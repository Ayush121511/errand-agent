"""Code-enforced approvals for consequential actions (book / cancel / reschedule).

Rules:
- Only `ApprovalGate.create` makes an approval request, and the confirmation card the user
  sees is rendered here from structured details, never written by the model.
- Only the user channel can approve (`approve`), via a UI button carrying the approval id
  and fingerprint, or a strict affirmative reply matched in code (see `is_affirmative`).
- Right before the side effect, `authorize_and_consume` recomputes the fingerprint from the
  *current* details. Any change to provider, slot or price (the material fields) fails
  the check and invalidates the approval. Consumption is a compare-and-set, so an approval
  can authorise at most one execution.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any

from errand_agent.core.clock import Clock
from errand_agent.core.fmt import clock_time, inr, service_label, when
from errand_agent.core.models import Approval, ApprovalAction, Session
from errand_agent.core.store import AgentStore
from errand_agent.obs.trace import Tracer

MATERIAL_FIELDS: dict[str, list[str]] = {
    "book": ["provider_id", "slot", "price", "service"],
    "cancel": ["booking_id", "provider_id", "slot"],
    "reschedule": ["booking_id", "provider_id", "from_slot", "to_slot"],
}

# A confirmation is a short reply made ONLY of confirming words and filler. Any other word
# ("but", "11am", "instead", a provider name...) makes it a change request, not an approval.
_STRONG = {"yes", "yeah", "yep", "yup", "y", "ok", "okay", "sure", "confirm", "confirmed",
           "proceed", "approve", "approved", "absolutely", "definitely", "works",
           # Hinglish
           "haan", "haa", "han", "ha", "ji", "haanji", "hanji", "theek", "thik", "chalega",
           "bilkul", "karo", "kardo", "kijiye", "karein"}
_STRONG_PHRASES = ("go ahead", "do it", "book it", "go for it", "sounds good",
                   "kar do", "kar dijiye", "kar dena", "kar dein")
_FILLER = {"fine", "alright", "please", "it", "go", "ahead", "book", "do", "that", "sounds",
           "good", "great", "perfect", "thanks", "thank", "you", "now", "just", "the", "booking",
           "and", "for", "lets", "let's", "kar", "this", "one", "same", "right", "away", "me",
           # Hinglish filler
           "hai", "na", "bhai", "chalo", "toh", "to", "yeh", "ye", "wala", "wali", "acha",
           "accha", "achha", "pls", "plz", "isko", "usko", "abhi", "jaldi", "dijiye", "dena",
           "dein", "sir", "shukriya", "dhanyavaad"}
_ACTION_WORDS = {"cancel": "cancel", "cancellation": "cancel", "reschedule": "reschedule",
                 "move": "reschedule"}
# A refusal starts with a refusal word, optionally after a lead-in ("hmm no", "sorry, don't").
# "wait" and "hold on" alone, and any question, are NOT refusals: "Wait, is that within my
# budget?" must leave the card open (found in a long live conversation).
_LEAD_IN = re.compile(r"^\s*((wait|hold on|hmm+|actually|oh|sorry|um+|uh+|well|arre|acha)"
                      r"[\s,.!]+)*", re.I)
_REFUSAL = re.compile(
    r"^(no|nope|nah|n|don'?t|do not|stop|cancel that|never ?mind|not now|"
    r"nahi|nahin|nai|mat|ruko|ruk jao|rehne do|abhi nahi)\b",
    re.I,
)


def is_affirmative(text: str, action: str | None = None) -> bool:
    """Strict: a bare confirmation only. "fine, yes" approves; "yes but at 11" does not.

    "cancel"/"reschedule" words only count when they match the card's action, so
    "yes, cancel it" can confirm a cancellation card but never a booking card.
    """
    words = re.findall(r"[a-z']+", text.lower())
    if not words or len(words) > 8 or re.search(r"\d", text):
        return False
    norm = " ".join(words)
    strong = any(w in _STRONG for w in words) or any(p in norm for p in _STRONG_PHRASES)
    for w in words:
        if w in _STRONG or w in _FILLER:
            continue
        if w in _ACTION_WORDS and (action is None or _ACTION_WORDS[w] == action):
            continue
        return False
    return strong


def is_negative(text: str) -> bool:
    t = text.strip()
    if not t or t.endswith("?"):
        return False
    rest = _LEAD_IN.sub("", t, count=1)
    return bool(_REFUSAL.match(rest)) and not is_affirmative(text)


def fingerprint(action: str, user_id: str, details: dict[str, Any]) -> str:
    material = {k: details.get(k) for k in MATERIAL_FIELDS[action]}
    canonical = json.dumps({"action": action, "user_id": user_id, **material}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def material_diff(action: str, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    return {
        k: {"approved": old.get(k), "now": new.get(k)}
        for k in MATERIAL_FIELDS[action]
        if old.get(k) != new.get(k)
    }


class ApprovalError(Exception):
    def __init__(self, code: str, approval: Approval | None = None, **info: Any) -> None:
        super().__init__(code)
        self.code = code
        self.approval = approval
        self.info = info


def render_card(action: ApprovalAction, d: dict[str, Any]) -> str:
    warnings = d.get("warnings") or []
    warn = "".join(f"\n⚠ {w}" for w in warnings)
    if action == "book":
        price_note = ""
        if d.get("search_price") and d["search_price"] != d["price"]:
            price_note = (f" (search showed {inr(d['search_price'])}; "
                          "this is the provider's held quote)")
        return (
            "Please confirm this booking:\n"
            f"• Provider: {d['provider_name']} ({d['provider_id']})\n"
            f"• Service: {service_label(d['service'])}"
            f"{' — ' + d['issue'] if d.get('issue') else ''}\n"
            f"• When: {when(d['slot'])}\n"
            f"• Price: {inr(d['price'])}{price_note}, charged only once the booking is verified\n"
            f"• Slot held until {clock_time(d['hold_expires_at'])}"
            f"{warn}\n"
            "Reply \"yes\" to confirm, or tell me what to change."
        )
    if action == "cancel":
        return (
            "Please confirm this cancellation:\n"
            f"• Booking: {d['booking_id']} — {service_label(d['service'])} with "
            f"{d['provider_name']}\n"
            f"• Currently scheduled: {when(d['slot'])}"
            f"{warn}\n"
            "Reply \"yes\" to cancel it, or tell me what to change."
        )
    return (
        "Please confirm this reschedule:\n"
        f"• Booking: {d['booking_id']} — {service_label(d['service'])} with "
        f"{d['provider_name']}\n"
        f"• From: {when(d['from_slot'])}\n"
        f"• To: {when(d['to_slot'])}"
        f"{warn}\n"
        "Reply \"yes\" to confirm, or tell me what to change."
    )


class ApprovalGate:
    def __init__(self, store: AgentStore, tracer: Tracer, clock: Clock,
                 ttl_seconds: int = 900) -> None:
        self.store = store
        self.tracer = tracer
        self.clock = clock
        self.ttl = ttl_seconds

    def create(self, session: Session, action: ApprovalAction, details: dict[str, Any]) -> Approval:
        for old in self.store.approvals(session.session_id, status="pending"):
            self.invalidate(old, "superseded_by_new_proposal")
        now = self.clock.now()
        a = Approval(
            approval_id=self.store.next_id("approval", "AP-"),
            session_id=session.session_id,
            user_id=session.user_id,
            action=action,
            details=details,
            fingerprint=fingerprint(action, session.user_id, details),
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=self.ttl)).isoformat(),
            card=render_card(action, details),
        )
        self.store.save_approval(a)
        self.tracer.event("approval_requested", approval_id=a.approval_id, action=action,
                          fingerprint=a.fingerprint,
                          material={k: details.get(k) for k in MATERIAL_FIELDS[action]})
        return a

    def pending_for(self, session_id: str) -> Approval | None:
        pending = self.store.approvals(session_id, status="pending")
        now = self.clock.now()
        live = []
        for a in pending:
            if datetime.fromisoformat(a.expires_at) <= now:
                self._set_status(a, "pending", "expired", "ttl_elapsed")
            else:
                live.append(a)
        return max(live, key=lambda a: a.created_at) if live else None

    def approve(self, approval_id: str, via: str, message_id: int | None = None,
                presented_fingerprint: str | None = None) -> Approval:
        a = self.store.get_approval(approval_id)
        if a is None:
            raise ApprovalError("not_found")
        if a.status != "pending":
            raise ApprovalError(f"not_pending:{a.status}", a)
        if datetime.fromisoformat(a.expires_at) <= self.clock.now():
            self._set_status(a, "pending", "expired", "ttl_elapsed")
            raise ApprovalError("expired", a)
        if presented_fingerprint is not None and presented_fingerprint != a.fingerprint:
            self.tracer.event("approval_rejected_by_gate", approval_id=a.approval_id,
                              reason="fingerprint_mismatch")
            raise ApprovalError("fingerprint_mismatch", a)
        new = a.model_copy(update={
            "status": "approved",
            "approved_at": self.clock.now().isoformat(),
            "approved_via": via,
            "approved_message_id": message_id,
        })
        if not self.store.cas_approval_status(a, "pending", new):
            raise ApprovalError("race", a)
        self.tracer.event("approval_granted", approval_id=a.approval_id, via=via,
                          message_id=message_id, fingerprint=a.fingerprint)
        return new

    def reject(self, approval: Approval, reason: str) -> None:
        self._set_status(approval, "pending", "rejected", reason)

    def invalidate(self, approval: Approval, reason: str, **info: Any) -> None:
        for expected in ("pending", "approved"):
            if self._set_status(approval, expected, "invalidated", reason, **info):
                return

    def authorize_and_consume(self, approval_id: str, action: ApprovalAction,
                              current: dict[str, Any]) -> Approval:
        """The one gate every side effect passes through."""
        a = self.store.get_approval(approval_id)
        if a is None or a.action != action:
            self.tracer.event("approval_violation_blocked", approval_id=approval_id,
                              action=action, reason="no_matching_approval")
            raise ApprovalError("no_matching_approval", a)
        if a.status != "approved":
            self.tracer.event("approval_violation_blocked", approval_id=approval_id,
                              action=action, reason=f"status_{a.status}")
            raise ApprovalError(f"not_approved:{a.status}", a)
        fp = fingerprint(action, a.user_id, current)
        if fp != a.fingerprint:
            diff = material_diff(action, a.details, current)
            self.invalidate(a, "material_change", diff=diff)
            raise ApprovalError("material_change", a, diff=diff)
        consumed = a.model_copy(update={"status": "consumed"})
        if not self.store.cas_approval_status(a, "approved", consumed):
            self.tracer.event("approval_violation_blocked", approval_id=approval_id,
                              action=action, reason="already_consumed")
            raise ApprovalError("already_consumed", a)
        self.tracer.event("approval_consumed", approval_id=approval_id, action=action,
                          fingerprint=fp)
        return consumed

    def _set_status(self, a: Approval, expected: str, status: str, reason: str,
                    **info: Any) -> bool:
        current = self.store.get_approval(a.approval_id)
        if current is None or current.status != expected:
            return False
        new = current.model_copy(update={"status": status, "status_reason": reason})
        ok = self.store.cas_approval_status(current, expected, new)
        if ok:
            self.tracer.event(f"approval_{status}", approval_id=a.approval_id, reason=reason,
                              **info)
        return ok
