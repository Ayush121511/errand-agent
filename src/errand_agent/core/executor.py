"""Deterministic executors (sagas) for consequential actions, plus restart recovery.

The model never calls `book`, `cancel` or `reschedule`. Once the user approves, code runs
the action here:

  approved ─► ensure hold is fresh ─► authorize_and_consume (fingerprint check, single use)
          ─► write attempt row (idempotency key) ─► call tool
          ─► success ............................► verify ─► CONFIRMED
          ─► slot_taken / conflict ..............► FAILED (nothing booked)
          ─► timeout / 503 / malformed ..........► get_booking_status (source of truth)
                 booked ─► CONFIRMED   not booked ─► retry with the SAME key (bounded)
                 can't tell ─► UNKNOWN (never reported as success; recovered later)

User-facing messages for outcomes are rendered from verified state by code, never by the
model, so the agent cannot claim a success that did not happen.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel

from errand_agent.core.approval import ApprovalError, ApprovalGate
from errand_agent.core.clock import Clock
from errand_agent.core.fmt import clock_time, inr, service_label, when
from errand_agent.core.jobs import JobQueue
from errand_agent.core.limits import Limits
from errand_agent.core.models import Approval, Attempt, HoldInfo, Phase, Session
from errand_agent.core.payments import Payments
from errand_agent.core.provider_client import ProviderClient
from errand_agent.core.store import AgentStore
from errand_agent.core.workflow import transition
from errand_agent.obs.trace import Tracer

OutcomeStatus = Literal["confirmed", "failed", "unknown", "needs_reapproval", "blocked",
                        "payment_required"]


class Outcome(BaseModel):
    status: OutcomeStatus
    action: str
    message: str
    reason: str | None = None
    booking_id: str | None = None
    approval_id: str | None = None
    new_approval_id: str | None = None
    data: dict[str, Any] = {}


class SimulatedCrash(Exception):
    """Raised at a configured crash point to simulate the process dying mid-action."""


def idempotency_key(action: str, approval_id: str, hold_id: str | None = None) -> str:
    raw = f"{action}:{approval_id}:{hold_id or ''}"
    return f"{action[:2]}-{hashlib.sha256(raw.encode()).hexdigest()[:20]}"


class ActionExecutor:
    def __init__(
        self,
        client: ProviderClient,
        gate: ApprovalGate,
        store: AgentStore,
        tracer: Tracer,
        clock: Clock,
        limits: Limits,
        crash_points: set[str] | None = None,
        payments: Payments | None = None,
        jobs: JobQueue | None = None,
        checkout: bool = False,
    ) -> None:
        self.checkout = checkout  # True: the user pays in a checkout before we book
        self.payments = payments
        self.jobs = jobs
        self.client = client
        self.gate = gate
        self.store = store
        self.tracer = tracer
        self.clock = clock
        self.limits = limits
        self.crash_points = crash_points if crash_points is not None else set()

    # ------------------------------------------------------------------ entry points

    def execute(self, session: Session, approval_id: str) -> Outcome:
        a = self.store.get_approval(approval_id)
        if a is None:
            return Outcome(status="blocked", action="unknown", reason="no_approval",
                           message="I couldn't find that confirmation, so I did nothing.")
        if a.status == "consumed":
            return self._replay(a)
        if (a.action == "book" and self.checkout and self.payments
                and not self.payments.paid_for(a.approval_id)):
            d = a.details
            self.tracer.event("payment_required", approval_id=a.approval_id, amount=d["price"])
            return Outcome(status="payment_required", action="book", approval_id=a.approval_id,
                           data={"amount": d["price"]},
                           message=f"Almost done: pay {inr(d['price'])} to book "
                                   f"{d['provider_name']} for {when(d['slot'])}. It's only "
                                   "authorised now and charged once the booking is verified; "
                                   "if the booking fails, nothing is charged.")
        if session.phase != Phase.AWAITING_APPROVAL:
            transition(session, Phase.AWAITING_APPROVAL, self.tracer, "approval_pending")
        transition(session, Phase.EXECUTING, self.tracer, f"approved:{a.approval_id}")
        if a.action == "book":
            return self._execute_book(session, a)
        if a.action == "cancel":
            return self._execute_cancel(session, a)
        return self._execute_reschedule(session, a)

    def recover_attempt(self, session: Session, at: Attempt) -> Outcome:
        """Resolve an attempt left pending/unknown by a crash or an unanswered timeout."""
        self.tracer.event("recovery_check", attempt_id=at.attempt_id, action=at.action,
                          idempotency_key=at.idempotency_key, prior_status=at.status)
        a = self.store.get_approval(at.approval_id)
        assert a is not None
        if session.phase not in (Phase.EXECUTING, Phase.VERIFYING):
            session.phase = Phase.VERIFYING  # resuming a saga that was interrupted
            self.tracer.event("state_transition", from_="(restart)", to="VERIFYING",
                              reason="recovery")
        if at.action == "book":
            status, data = self._booking_status(at.idempotency_key, at.hold_id)
            if status == "booked":
                return self._confirm_book(session, a, at, data, note="recovered")
            if status == "failed":
                return self._fail(session, a, at, "slot_taken")
            if status == "not_booked":
                hold_live = data.get("hold_status") == "active"
                if hold_live:
                    transition(session, Phase.EXECUTING, self.tracer, "recovery_resume_same_key")
                    return self._book_loop(session, a, dict(a.details), at)
                return self._fail(session, a, at, "not_booked_hold_expired")
            return self._unknown(session, a, at)
        if at.action == "cancel":
            row = self._find_booking(session, a.details["booking_id"])
            if row and row["status"] == "cancelled":
                return self._confirm_simple(session, a, at, row, "cancel", note="recovered")
            transition(session, Phase.EXECUTING, self.tracer, "recovery_resume_same_key")
            return self._cancel_loop(session, a, at)
        row = self._find_booking(session, a.details["booking_id"])
        if row and row["slot"] == a.details["to_slot"]:
            return self._confirm_simple(session, a, at, row, "reschedule", note="recovered")
        transition(session, Phase.EXECUTING, self.tracer, "recovery_resume_same_key")
        return self._reschedule_loop(session, a, at)

    # ------------------------------------------------------------------ book

    def _execute_book(self, session: Session, a: Approval) -> Outcome:
        d = dict(a.details)
        fresh = self._ensure_fresh_hold(session, a, d)
        if isinstance(fresh, Outcome):
            return fresh
        d = fresh
        try:
            a = self.gate.authorize_and_consume(a.approval_id, "book", d)
        except ApprovalError as e:
            return self._approval_failure(session, a, e, current=d)
        key = idempotency_key("book", a.approval_id, d["hold_id"])
        now = self.clock.now().isoformat()
        at = self.store.insert_attempt(Attempt(
            attempt_id=self.store.next_id("attempt", "AT-"),
            approval_id=a.approval_id, session_id=session.session_id, action="book",
            idempotency_key=key, hold_id=d["hold_id"], created_at=now, updated_at=now,
        ))
        if at.status != "pending":
            return self._replay(a)
        self.tracer.event("action_attempt_started", attempt_id=at.attempt_id, action="book",
                          idempotency_key=key, hold_id=d["hold_id"])
        if self.payments:  # money is authorised first and only captured once verified
            pay, why = self.payments.authorize(self.tracer, session.user_id, at, a)
            if pay is None:
                return self._fail(session, a, at, why or "payment_declined")
        self._crash("before_book")
        return self._book_loop(session, a, d, at)

    def _book_loop(self, session: Session, a: Approval, d: dict[str, Any],
                   at: Attempt) -> Outcome:
        hold_id, key = at.hold_id, at.idempotency_key
        for n in range(1, self.limits.max_book_attempts + 1):
            r = self.client.call("book", {"hold_id": hold_id, "idempotency_key": key},
                                 origin="executor", attempt=n)
            self._crash("after_book_call")
            if r.ok:
                st = r.data.get("status")
                if st == "success":
                    return self._confirm_book(session, a, at, r.data)
                if st == "slot_taken":
                    return self._fail(session, a, at, "slot_taken")
                if st == "hold_expired":
                    return self._fail(session, a, at, "hold_expired")
                return self._fail(session, a, at, f"unexpected_status:{st}")

            # Ambiguous: the side effect may or may not have happened.
            transition(session, Phase.VERIFYING, self.tracer, f"book_{r.error}")
            self.tracer.event("outcome_ambiguous", action="book", error=r.error, attempt=n,
                              idempotency_key=key)
            status, data = self._booking_status(key, hold_id)
            if status == "booked":
                return self._confirm_book(session, a, at, data, note=f"after_{r.error}")
            if status == "failed":
                return self._fail(session, a, at, "slot_taken")
            if status == "unknown":
                return self._unknown(session, a, at)
            if data.get("hold_status") == "expired":
                return self._fail(session, a, at, "hold_expired")
            # Verified not booked: safe to retry with the SAME key.
            transition(session, Phase.EXECUTING, self.tracer, "verified_not_booked_retry")
            self.tracer.event("retry_same_key", action="book", attempt=n + 1,
                              idempotency_key=key)
            self._backoff(n)
        status, data = self._booking_status(key, hold_id)
        transition(session, Phase.VERIFYING, self.tracer, "attempts_exhausted")
        if status == "booked":
            return self._confirm_book(session, a, at, data, note="after_retries")
        if status == "not_booked":
            return self._fail(session, a, at, "provider_unavailable")
        return self._unknown(session, a, at)

    def _ensure_fresh_hold(self, session: Session, a: Approval,
                           d: dict[str, Any]) -> dict[str, Any] | Outcome:
        """If the hold expired, re-hold the same slot. Same price ⇒ continue under the same
        approval (hold id is not material). Different price or slot gone ⇒ new approval."""
        if datetime.fromisoformat(d["hold_expires_at"]) > self.clock.now():
            return d
        self.tracer.event("hold_expired_before_booking", hold_id=d["hold_id"])
        r = self.client.call("hold_slot", {"provider_id": d["provider_id"], "slot": d["slot"]},
                             origin="executor")
        if not r.ok or r.data.get("status") != "ok":
            self.gate.invalidate(a, "slot_unavailable_on_rehold")
            transition(session, Phase.FAILED, self.tracer, "rehold_failed")
            msg = (f"The hold on {when(d['slot'])} expired before you confirmed, and the slot "
                   "is no longer available. Nothing was booked. Want me to find another slot?")
            return Outcome(status="failed", action="book", reason="rehold_failed", message=msg,
                           approval_id=a.approval_id)
        new = dict(d)
        new.update(hold_id=r.data["hold_id"], hold_expires_at=r.data["expires_at"],
                   price=r.data["quoted_price"], search_price=r.data["search_price"])
        if new["price"] != d["price"]:
            diff = {"price": {"approved": d["price"], "now": new["price"]}}
            self.gate.invalidate(a, "material_change", diff=diff)
            return self._reapprove(session, a, new,
                                   f"When I re-held the slot (the first hold expired), the price "
                                   f"changed from {inr(d['price'])} to {inr(new['price'])}. "
                                   "I have not booked anything.")
        a2 = a.model_copy(update={"details": new})
        self.store.save_approval(a2)
        if session.task and session.task.hold:
            session.task.hold.hold_id = new["hold_id"]
            session.task.hold.expires_at = new["hold_expires_at"]
        self.tracer.event("rehold_same_terms", old_hold=d["hold_id"], new_hold=new["hold_id"],
                          price=new["price"])
        return new

    def _reapprove(self, session: Session, old: Approval, details: dict[str, Any],
                   why: str) -> Outcome:
        new = self.gate.create(session, old.action, details)
        if session.task:
            session.task.pending_approval_id = new.approval_id
            if old.action == "book":
                session.task.hold = HoldInfo(
                    hold_id=details["hold_id"], provider_id=details["provider_id"],
                    provider_name=details["provider_name"], service=details["service"],
                    slot=details["slot"], quoted_price=details["price"],
                    search_price=details.get("search_price", details["price"]),
                    expires_at=details["hold_expires_at"],
                )
        transition(session, Phase.AWAITING_APPROVAL, self.tracer, "material_change_reapproval")
        return Outcome(status="needs_reapproval", action=old.action, reason="material_change",
                       message=f"{why}\n\n{new.card}", approval_id=old.approval_id,
                       new_approval_id=new.approval_id)

    def _booking_status(self, key: str, hold_id: str | None) -> tuple[str, dict[str, Any]]:
        """Ask the source of truth. Returns (booked|failed|not_booked|unknown, data)."""
        for n in range(1, self.limits.max_status_checks + 1):
            r = self.client.call("get_booking_status", {"idempotency_key": key},
                                 origin="executor", attempt=n)
            if r.ok:
                st = r.data.get("status")
                if st == "booked":
                    return "booked", r.data
                if st == "failed":
                    return "failed", r.data
                if hold_id:
                    r2 = self.client.call("get_booking_status", {"hold_id": hold_id},
                                          origin="executor", attempt=n)
                    if r2.ok:
                        st2 = r2.data.get("status")
                        if st2 == "booked":
                            return "booked", r2.data
                        if st2 in ("not_booked", "not_found"):
                            return "not_booked", r2.data
                return "not_booked", r.data
            self._backoff(n)
        return "unknown", {}

    def _confirm_book(self, session: Session, a: Approval, at: Attempt, data: dict[str, Any],
                      note: str | None = None) -> Outcome:
        booking_id = data.get("booking_id")
        if note is None:  # direct success response: still verify against the source of truth
            status, vdata = self._booking_status(at.idempotency_key, at.hold_id)
            if status != "booked":
                self.tracer.event("verification_mismatch", booking_id=booking_id, status=status)
                return self._unknown(session, a, at)
            booking_id = vdata.get("booking_id", booking_id)
        at = at.model_copy(update={"status": "confirmed", "booking_id": booking_id,
                                   "result": data, "updated_at": self.clock.now().isoformat()})
        self.store.save_attempt(at)
        d = a.details
        transition(session, Phase.DONE, self.tracer, "booking_verified")
        self.tracer.event("action_confirmed", action="book", booking_id=booking_id,
                          attempt_id=at.attempt_id, verified_via=note or "book_response+status")
        if session.task:
            session.task.outcome = {"action": "book", "status": "confirmed",
                                    "booking_id": booking_id}
            session.task.pending_approval_id = None
        session.known_bookings[booking_id] = "confirmed"
        paid = self._capture(at, booking_id)
        self._schedule_reminder(session, booking_id, a.details["slot"])
        extra = ""
        if note and note.startswith("after_"):
            extra = ("\n(The provider's system didn't answer in time, so I checked the booking "
                     "status before telling you. It went through exactly once — no duplicate.)")
        elif note == "recovered":
            extra = "\n(I verified this after a restart; it went through exactly once.)"
        msg = (f"Booked and verified.\n• {d['provider_name']} — {service_label(d['service'])}\n"
               f"• {when(d['slot'])}\n• Price: {inr(d['price'])}\n• Booking ID: {booking_id}"
               f"{paid}{extra}")
        return Outcome(status="confirmed", action="book", booking_id=booking_id, message=msg,
                       approval_id=a.approval_id, reason=note)

    # ------------------------------------------------------------------ cancel

    def _execute_cancel(self, session: Session, a: Approval) -> Outcome:
        current = self._current_booking_details(session, a)
        if isinstance(current, Outcome):
            return current
        try:
            a = self.gate.authorize_and_consume(a.approval_id, "cancel", current)
        except ApprovalError as e:
            return self._approval_failure(session, a, e, current=current)
        at = self._new_attempt(session, a, "cancel", idempotency_key("cancel", a.approval_id))
        if at.status != "pending":
            return self._replay(a)
        return self._cancel_loop(session, a, at)

    def _cancel_loop(self, session: Session, a: Approval, at: Attempt) -> Outcome:
        bid = a.details["booking_id"]
        for n in range(1, self.limits.max_cancel_attempts + 1):
            r = self.client.call("cancel", {"booking_id": bid, "idempotency_key":
                                            at.idempotency_key}, origin="executor", attempt=n)
            if r.ok:
                if r.data.get("status") == "cancelled":
                    row = self._find_booking(session, bid)
                    if row and row["status"] == "cancelled":
                        return self._confirm_simple(session, a, at, row, "cancel")
                    return self._unknown(session, a, at)
                return self._fail(session, a, at, r.data.get("error", "cancel_rejected"))
            self.tracer.event("retry_same_key", action="cancel", error=r.error, attempt=n + 1,
                              idempotency_key=at.idempotency_key)
            self._backoff(n)
        transition(session, Phase.VERIFYING, self.tracer, "cancel_attempts_exhausted")
        row = self._find_booking(session, bid)
        if row and row["status"] == "cancelled":
            return self._confirm_simple(session, a, at, row, "cancel", note="after_retries")
        if row is None:
            return self._unknown(session, a, at)
        return self._fail(session, a, at, "cancel_retries_exhausted")

    # ------------------------------------------------------------------ reschedule

    def _execute_reschedule(self, session: Session, a: Approval) -> Outcome:
        current = self._current_booking_details(session, a)
        if isinstance(current, Outcome):
            return current
        try:
            a = self.gate.authorize_and_consume(a.approval_id, "reschedule", current)
        except ApprovalError as e:
            return self._approval_failure(session, a, e, current=current)
        at = self._new_attempt(session, a, "reschedule",
                               idempotency_key("reschedule", a.approval_id))
        if at.status != "pending":
            return self._replay(a)
        return self._reschedule_loop(session, a, at)

    def _reschedule_loop(self, session: Session, a: Approval, at: Attempt) -> Outcome:
        bid, to_slot = a.details["booking_id"], a.details["to_slot"]
        for n in range(1, self.limits.max_reschedule_attempts + 1):
            r = self.client.call("reschedule", {"booking_id": bid, "slot": to_slot,
                                                "idempotency_key": at.idempotency_key},
                                 origin="executor", attempt=n)
            if r.ok:
                st = r.data.get("status")
                if st == "rescheduled":
                    row = self._find_booking(session, bid)
                    if row and row["slot"] == to_slot:
                        return self._confirm_simple(session, a, at, row, "reschedule")
                    return self._unknown(session, a, at)
                if st == "slot_conflict":
                    return self._fail(session, a, at, "slot_conflict")
                return self._fail(session, a, at, r.data.get("error", "reschedule_rejected"))
            transition(session, Phase.VERIFYING, self.tracer, f"reschedule_{r.error}")
            row = self._find_booking(session, bid)
            if row and row["slot"] == to_slot:
                return self._confirm_simple(session, a, at, row, "reschedule",
                                            note=f"after_{r.error}")
            transition(session, Phase.EXECUTING, self.tracer, "verified_not_applied_retry")
            self.tracer.event("retry_same_key", action="reschedule", attempt=n + 1,
                              idempotency_key=at.idempotency_key)
            self._backoff(n)
        return self._fail(session, a, at, "reschedule_retries_exhausted")

    # ------------------------------------------------------------------ shared helpers

    def _current_booking_details(self, session: Session, a: Approval) -> dict[str, Any] | Outcome:
        """Re-read the booking right before acting so the fingerprint covers current state."""
        row = self._find_booking(session, a.details["booking_id"])
        if row is None or row["status"] not in ("confirmed",):
            self.gate.invalidate(a, "booking_not_active",
                                 booking_status=row["status"] if row else None)
            transition(session, Phase.FAILED, self.tracer, "booking_not_active")
            return Outcome(status="failed", action=a.action, reason="booking_not_active",
                           approval_id=a.approval_id,
                           message=f"Booking {a.details['booking_id']} is no longer active "
                                   f"({row['status'] if row else 'not found'}), so I did nothing.")
        current = dict(a.details)
        current["provider_id"] = row["provider_id"]
        if a.action == "cancel":
            current["slot"] = row["slot"]
        else:
            current["from_slot"] = row["slot"]
        return current

    def _find_booking(self, session: Session, booking_id: str) -> dict[str, Any] | None:
        r = self.client.call("list_bookings", {"user_id": session.user_id}, origin="executor")
        if not r.ok:
            return None
        return next((b for b in r.data.get("bookings", []) if b["booking_id"] == booking_id), None)

    def _new_attempt(self, session: Session, a: Approval, action: str, key: str) -> Attempt:
        now = self.clock.now().isoformat()
        at = self.store.insert_attempt(Attempt(
            attempt_id=self.store.next_id("attempt", "AT-"), approval_id=a.approval_id,
            session_id=session.session_id, action=action,  # type: ignore[arg-type]
            idempotency_key=key, booking_id=a.details.get("booking_id"),
            created_at=now, updated_at=now,
        ))
        self.tracer.event("action_attempt_started", attempt_id=at.attempt_id, action=action,
                          idempotency_key=key)
        return at

    def _confirm_simple(self, session: Session, a: Approval, at: Attempt, row: dict[str, Any],
                        action: str, note: str | None = None) -> Outcome:
        at = at.model_copy(update={"status": "confirmed", "result": row,
                                   "updated_at": self.clock.now().isoformat()})
        self.store.save_attempt(at)
        transition(session, Phase.DONE, self.tracer, f"{action}_verified")
        self.tracer.event("action_confirmed", action=action, booking_id=row["booking_id"],
                          attempt_id=at.attempt_id, verified_via=note or "list_bookings")
        if session.task:
            session.task.outcome = {"action": action, "status": "confirmed",
                                    "booking_id": row["booking_id"]}
            session.task.pending_approval_id = None
        session.known_bookings[row["booking_id"]] = row["status"]
        d = a.details
        if action == "cancel":
            msg = (f"Cancelled and verified: {service_label(d['service'])} with "
                   f"{d['provider_name']} on {when(d['slot'])} (booking {row['booking_id']}).")
            refund = self.payments.refund_booking(self.tracer, row["booking_id"], "cancelled "
                                                  "by the user") if self.payments else None
            if refund:
                msg += (f"\n• Refunded: {inr(refund['amount'])} to your original payment method "
                        f"(payment {refund['payment_id']}).")
            if note == "after_retries" or self._had_retry(at):
                msg += "\n(The provider's system returned a temporary error first; I retried " \
                       "safely with the same request key.)"
        else:
            msg = (f"Rescheduled and verified: {service_label(d['service'])} with "
                   f"{d['provider_name']} is now on {when(row['slot'])} "
                   f"(booking {row['booking_id']}).")
        return Outcome(status="confirmed", action=action, booking_id=row["booking_id"],
                       message=msg, approval_id=a.approval_id, reason=note)

    def _had_retry(self, at: Attempt) -> bool:
        return any(e["type"] == "retry_same_key" and e.get("idempotency_key") == at.idempotency_key
                   for e in self.tracer.events)

    _FAIL_MESSAGES = {
        "slot_taken": "Someone else booked that slot just before us, so nothing was booked and "
                      "nothing was charged. Want me to hold the next best option?",
        "hold_expired": "The hold on that slot expired before the booking went through. Nothing "
                        "was booked. Want me to try to hold it again?",
        "not_booked_hold_expired": "Your earlier booking did not go through before the restart, "
                                   "and the slot hold has expired. Nothing was booked. Want me "
                                   "to look again?",
        "provider_unavailable": "The provider's booking system kept failing. I checked, and "
                                "nothing was booked. Want me to try a different provider?",
        "slot_conflict": "That new time conflicts with the provider's schedule, so your booking "
                         "is unchanged. Want me to suggest other times?",
        "cancel_retries_exhausted": "The provider's system kept returning errors, and your "
                                    "booking is still active — it was NOT cancelled. I can try "
                                    "again in a few minutes.",
        "reschedule_retries_exhausted": "The provider's system kept failing, and your booking is "
                                        "unchanged. I can try again shortly.",
        "payment_declined": "Your payment was declined, so I didn't book anything and nothing "
                            "was charged. Want to try again?",
        "payment_unavailable": "The payment system didn't respond, so I didn't book anything "
                               "and nothing was charged. Want me to try again in a minute?",
    }

    def _fail(self, session: Session, a: Approval, at: Attempt, reason: str) -> Outcome:
        at = at.model_copy(update={"status": "failed", "result": {"reason": reason},
                                   "updated_at": self.clock.now().isoformat()})
        self.store.save_attempt(at)
        if a.action == "book" and self.payments:  # release the authorisation: nothing booked
            p = self.payments.void(self.tracer, at.attempt_id)
            if p and p["status"] == "authorized":
                self._schedule_reconcile(p["payment_id"])
        transition(session, Phase.FAILED, self.tracer, reason)
        self.tracer.event("action_failed", action=a.action, reason=reason,
                          attempt_id=at.attempt_id)
        if session.task:
            session.task.outcome = {"action": a.action, "status": "failed", "reason": reason}
            session.task.pending_approval_id = None
            if a.action == "book":
                session.task.hold = None
        msg = self._FAIL_MESSAGES.get(
            reason, f"That didn't go through ({reason}). Nothing was changed.")
        return Outcome(status="failed", action=a.action, reason=reason, message=msg,
                       approval_id=a.approval_id)

    def _unknown(self, session: Session, a: Approval, at: Attempt) -> Outcome:
        at = at.model_copy(update={"status": "unknown",
                                   "updated_at": self.clock.now().isoformat()})
        self.store.save_attempt(at)
        transition(session, Phase.FAILED, self.tracer, "outcome_unknown")
        self.tracer.event("action_outcome_unknown", action=a.action, attempt_id=at.attempt_id,
                          idempotency_key=at.idempotency_key)
        p = self.payments.for_attempt(at.attempt_id) if self.payments else None
        if p:  # money stays authorised, not captured, until the booking outcome is known
            self._schedule_reconcile(p["payment_id"])
        if session.task:
            session.task.outcome = {"action": a.action, "status": "unknown"}
        msg = ("I couldn't confirm whether that went through — the provider's system isn't "
               "responding. I have NOT assumed it succeeded, and I won't retry blindly. I'll "
               "check the booking status again and let you know.")
        return Outcome(status="unknown", action=a.action, reason="unverified", message=msg,
                       approval_id=a.approval_id)

    def _replay(self, a: Approval) -> Outcome:
        """Repeated confirmation of an already-executed approval: report, never re-execute."""
        attempts = self.store.attempts(approval_id=a.approval_id)
        self.tracer.event("duplicate_confirmation_ignored", approval_id=a.approval_id,
                          attempts=[x.attempt_id for x in attempts])
        last = attempts[-1] if attempts else None
        if last and last.status == "confirmed":
            msg = (f"That's already done (booking {last.booking_id}). I didn't repeat it, so "
                   "there's no duplicate.")
        elif last and last.status == "failed":
            msg = "That request already finished without going through; I didn't retry it. " \
                  "Want me to look for another option?"
        else:
            msg = "That request is already being processed; I won't send it twice."
        return Outcome(status="blocked", action=a.action, reason="already_consumed", message=msg,
                       approval_id=a.approval_id,
                       booking_id=last.booking_id if last else None)

    def _approval_failure(self, session: Session, a: Approval, e: ApprovalError,
                          current: dict[str, Any] | None = None) -> Outcome:
        if e.code == "already_consumed":
            return self._replay(a)
        if e.code == "material_change" and current is not None:
            return self._reapprove(session, a, current,
                                   "Something material changed since you confirmed "
                                   f"({', '.join(e.info.get('diff', {}))}).")
        transition(session, Phase.FAILED, self.tracer, f"approval_{e.code}")
        return Outcome(status="blocked", action=a.action, reason=e.code,
                       approval_id=a.approval_id,
                       message="I couldn't act on that confirmation because the details "
                               "changed or it is no longer valid. Nothing was done.")

    # ------------------------------------------------------------------ payments and jobs
    def _capture(self, at: Attempt, booking_id: str | None) -> str:
        if not self.payments:
            return ""
        p = self.payments.capture(self.tracer, at.attempt_id, booking_id)
        if p is None:
            return ""
        if p["status"] == "captured":
            return f"\n• Paid: {inr(p['amount'])} (payment {p['payment_id']})"
        self._schedule_reconcile(p["payment_id"])
        return ("\n• Payment: authorised; the bank hasn't confirmed the charge yet. I'll check "
                "it and you'll never be charged twice.")

    def _schedule_reconcile(self, payment_id: str) -> None:
        if self.jobs:
            self.jobs.schedule("reconcile_payment", self.clock.now() + timedelta(minutes=1),
                               {"payment_id": payment_id}, dedupe_key=f"reconcile:{payment_id}")

    def _schedule_reminder(self, session: Session, booking_id: str | None, slot: str) -> None:
        """A reminder at 18:00 the day before the visit (or soon, if that has passed)."""
        if not self.jobs or not booking_id:
            return
        now = self.clock.now()
        visit = datetime.fromisoformat(slot).replace(tzinfo=now.tzinfo)
        if visit - now < timedelta(hours=3):
            return  # too close for a reminder to help
        at = (visit - timedelta(days=1)).replace(hour=18, minute=0)
        self.jobs.schedule("visit_reminder", max(at, now + timedelta(minutes=1)),
                           {"session_id": session.session_id, "booking_id": booking_id},
                           dedupe_key=f"reminder:{booking_id}")
        self.tracer.event("reminder_scheduled", booking_id=booking_id,
                          run_at=max(at, now + timedelta(minutes=1)).isoformat())

    def _backoff(self, n: int) -> None:
        delays = self.limits.retry_backoff_seconds
        self.clock.sleep(delays[min(n - 1, len(delays) - 1)])

    def _crash(self, point: str) -> None:
        if point in self.crash_points:
            self.crash_points.discard(point)
            self.tracer.event("simulated_crash", point=point)
            raise SimulatedCrash(point)

    @staticmethod
    def hold_expiry_text(d: dict[str, Any]) -> str:
        return clock_time(d["hold_expires_at"])
