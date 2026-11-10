"""The agent runtime: routes inputs, runs the bounded model loop, persists, traces.

Inputs and who handles them:
  user message that is a bare confirmation of the pending card ─► ApprovalGate + executor (code)
  approval button click ─────────────────────────────────────────► ApprovalGate + executor (code)
  restricted-data request / gibberish / blocked user ────────────► screened by code, no model
  any other user message ────────────────────────────────────────► model loop (bounded)
  provider event (cancellation) ─────────────────────────────────► proactive handler (code)
  process start ─────────────────────────────────────────────────► recovery of pending attempts
  new chat ──────────────────────────────────────────────────────► greeting with next free slots

Every path ends with a user-facing message, including limit exhaustion and internal errors.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from errand_agent.agent.context import ContextBuilder
from errand_agent.agent.greeting import build_greeting
from errand_agent.agent.proactive import handle_provider_cancellation
from errand_agent.agent.review_reader import DeterministicReviewReader, ReviewReader
from errand_agent.agent.tools import TERMINAL_TOOLS, TOOL_SPECS, ToolDispatcher, tool_result_json
from errand_agent.core.approval import ApprovalError, ApprovalGate, is_affirmative, is_negative
from errand_agent.core.clock import Clock
from errand_agent.core.executor import ActionExecutor, Outcome, SimulatedCrash
from errand_agent.core.fmt import inr, when
from errand_agent.core.guard import Screen, looks_hinglish, screen
from errand_agent.core.limits import LimitExceeded, Limits, RunBudget
from errand_agent.core.models import Phase, Session
from errand_agent.core.provider_client import ProviderClient
from errand_agent.core.store import AgentStore
from errand_agent.llm.base import ChatMessage, LLMClient, default_token_ratio, estimate_tokens
from errand_agent.memory.store import MemoryStore
from errand_agent.mocks.service import MockProviderService
from errand_agent.obs.pricing import estimate_cost
from errand_agent.obs.trace import Tracer

# Claims that an action just happened. Only code-rendered executor messages may say this,
# so in a model reply they are always blocked.
_ACTION_CLAIM = re.compile(
    r"\b(i'?ve|i have|i)\s+(just\s+|now\s+|successfully\s+)?(booked|cancel+ed|rescheduled)\b"
    r"|\b(has|have) (now )?been (successfully )?(booked|confirmed|cancel+ed|rescheduled)\b"
    r"|\bsuccessfully (booked|cancel+ed|rescheduled)\b|\ball set\b|\bbooking (is )?confirmed\b",
    re.I,
)
# Statements about booking state ("your AC service is booked for Thursday"): fine only when
# grounded in a list_bookings result from this same run.
_STATE_CLAIM = re.compile(r"\bis (now )?(booked|confirmed|cancel+ed|rescheduled)\b", re.I)
TOOL_SPEC_TOKENS = estimate_tokens(json.dumps([s.model_dump() for s in TOOL_SPECS]))
_NEGATION = re.compile(
    r"\b(nothing|not|no|never|nor|cannot|can'?t|isn'?t|hasn'?t|haven'?t|wasn'?t|won'?t|"
    r"didn'?t|couldn'?t)\b", re.I)


def _affirmed(pattern: re.Pattern[str], text: str) -> bool:
    """True if `pattern` matches in a clause that is not negated.

    "Nothing has been booked" or "I haven't cancelled anything" are honest statements and
    must not be treated as success claims.
    """
    for m in pattern.finditer(text):
        clause_start = max(text.rfind(c, 0, m.start()) for c in ".!?\n;") + 1
        if not _NEGATION.search(text[clause_start:m.start()]):
            return True
    return False


def claims_action(text: str) -> bool:
    return _affirmed(_ACTION_CLAIM, text)


def claims_state(text: str) -> bool:
    return _affirmed(_STATE_CLAIM, text)


@dataclass
class AgentConfig:
    user_id: str = "user_1"
    limits: Limits = field(default_factory=Limits)
    context_budget: int = 6000
    memory_budget: int = 600
    trace_dir: str | Path | None = None
    block_after_flags: int = 3   # restricted-data attempts before the user is blocked


@dataclass
class Parts:
    tracer: Tracer
    client: ProviderClient
    gate: ApprovalGate
    executor: ActionExecutor


class ErrandAgent:
    def __init__(self, llm: LLMClient, provider: MockProviderService, store: AgentStore,
                 clock: Clock, config: AgentConfig | None = None,
                 reader: ReviewReader | None = None,
                 crash_points: set[str] | None = None) -> None:
        self.llm = llm
        self.provider = provider
        self.store = store
        self.clock = clock
        self.config = config or AgentConfig()
        self.reader = reader or DeterministicReviewReader()
        self.memory = MemoryStore(store, clock)
        self.crash_points = crash_points if crash_points is not None else set()
        self._tracers: dict[str, Tracer] = {}
        self.token_ratio = default_token_ratio(getattr(llm, "model", ""))

    # ------------------------------------------------------------------ plumbing
    def tracer(self, session_id: str) -> Tracer:
        if session_id not in self._tracers:
            self._tracers[session_id] = Tracer(session_id, self.config.trace_dir, self.clock)
        return self._tracers[session_id]

    def parts(self, session_id: str) -> Parts:
        tr = self.tracer(session_id)
        client = ProviderClient(self.provider, tr, self.clock)
        gate = ApprovalGate(self.store, tr, self.clock)
        ex = ActionExecutor(client, gate, self.store, tr, self.clock, self.config.limits,
                            self.crash_points)
        return Parts(tr, client, gate, ex)

    def open_session(self, session_id: str | None = None) -> Session:
        if session_id:
            s = self.store.load_session(session_id)
            if s:
                return s
        sid = session_id or self.store.next_id("session", "S-")
        s = Session(session_id=sid, user_id=self.config.user_id)
        self._save(s)
        self.tracer(sid).event("session_opened", user_id=s.user_id)
        return s

    def _save(self, s: Session) -> None:
        self.store.save_session(s, self.clock.now().isoformat())

    def _say(self, s: Session, text: str, **meta: Any) -> str:
        self.store.add_message(s.session_id, s.user_id, "assistant", text,
                               self.clock.now().isoformat(), meta)
        self.tracer(s.session_id).event("assistant_message", text=text, **meta)
        return text

    # ------------------------------------------------------------------ chat start
    def greet(self, session_id: str) -> str:
        """Opening message of a new chat: welcome (back) and the next free slot for each
        service. Code only, no model call."""
        s = self.open_session(session_id)
        p = self.parts(s.session_id)
        p.tracer.start_run("greeting")
        if self.is_blocked(s.user_id):
            text = self._blocked_text()
        else:
            text, rows = build_greeting(s, p.client, self.memory, self.store, self.clock)
            p.tracer.event("greeting_built", rows=rows)
        out = self._say(s, text, greeting=True)
        p.tracer.end_run("greeted", phase=s.phase.value)
        return out

    # ------------------------------------------------------------------ user input
    def is_blocked(self, user_id: str) -> bool:
        return self.store.flag_count(user_id) >= self.config.block_after_flags

    def handle_message(self, session_id: str, text: str) -> list[str]:
        s = self.open_session(session_id)
        p = self.parts(s.session_id)
        p.tracer.start_run("user_message")
        blocked = self.is_blocked(s.user_id)
        sc = Screen("blocked") if blocked else screen(text)
        # Screened messages are kept for audit but marked, so they never enter a model context
        # and can never serve as evidence for a task field or memory.
        msg_id = self.store.add_message(s.session_id, s.user_id, "user", text,
                                        self.clock.now().isoformat(),
                                        {"screened": sc.kind} if sc.kind else None)
        p.tracer.event("user_message", message_id=msg_id, text=text)
        budget = RunBudget(self.config.limits, self.clock)
        outcome = "replied"
        if sc.kind:
            replies, outcome = self._screened(s, p, sc, text, msg_id)
            if sc.kind == "gibberish":
                replies = self._resurface_card(s, p, replies)
            self._save(s)
            out = [self._say(s, r, screened_reply=True) for r in replies]
            p.tracer.end_run(outcome, phase=s.phase.value, budget=budget.snapshot())
            return out
        try:
            replies, outcome = self._route(s, p, text, msg_id, budget)
        except LimitExceeded as e:
            p.tracer.event("limit_hit", kind=e.kind, used=e.used, limit=e.limit)
            replies, outcome = [self._limit_message(s, p, e)], f"limit_hit:{e.kind}"
        except SimulatedCrash:
            self._save(s)
            raise
        except Exception as e:  # noqa: BLE001 - every exit path must answer the user
            p.tracer.event("unexpected_error", error=f"{type(e).__name__}: {e}")
            replies = ["Something went wrong on my side while handling that. "
                       + self._where_things_stand(s, p)]
            outcome = "error"
        self._save(s)
        replies = self._resurface_card(s, p, replies)
        out = [self._say(s, r) for r in replies]
        p.tracer.end_run(outcome, phase=s.phase.value, budget=budget.snapshot())
        return out

    def _resurface_card(self, s: Session, p: Parts, replies: list[str]) -> list[str]:
        """Keep an open confirmation card next to the latest message.

        If the user asks a question while a card is open, the model's answer would otherwise
        push the card out of sight ("tap the card on your screen" pointing at a message far
        up). The same card, same approval id and fingerprint, is appended to the reply; the
        UI disables the older copy.
        """
        pending = p.gate.pending_for(s.session_id)
        if pending is None or not replies or not pending.card or pending.card in replies[-1]:
            return replies
        p.tracer.event("approval_card_resurfaced", approval_id=pending.approval_id)
        return [*replies[:-1], replies[-1].rstrip() + "\n\n" + pending.card]

    def handle_approval(self, session_id: str, approval_id: str, fingerprint: str | None,
                        approve: bool = True) -> list[str]:
        """Approval button in the UI: carries the exact approval id + fingerprint shown."""
        s = self.open_session(session_id)
        p = self.parts(s.session_id)
        p.tracer.start_run("approval_click", approval_id=approval_id, approve=approve)
        label = "Confirm" if approve else "Decline"
        msg_id = self.store.add_message(s.session_id, s.user_id, "user", f"[{label} button]",
                                        self.clock.now().isoformat(),
                                        {"approval_id": approval_id})
        try:
            if approve and self.is_blocked(s.user_id):
                p.tracer.event("blocked_user_input", channel="button", approval_id=approval_id)
                text, outcome = self._blocked_text(), "blocked"  # button: no text to read
            elif not approve:
                a = self.store.get_approval(approval_id)
                if a and a.status == "pending":
                    p.gate.reject(a, "user_declined")
                text, outcome = "Okay, I won't go ahead with that. Nothing was changed.", "declined"
            else:
                try:
                    p.gate.approve(approval_id, via="button", message_id=msg_id,
                                   presented_fingerprint=fingerprint)
                    res = p.executor.execute(s, approval_id)
                    text, outcome = res.message, f"executed:{res.status}"
                except ApprovalError as e:
                    text, outcome = self._approval_error_text(p, e), f"approval_error:{e.code}"
        except SimulatedCrash:
            self._save(s)
            raise
        except Exception as e:  # noqa: BLE001
            p.tracer.event("unexpected_error", error=f"{type(e).__name__}: {e}")
            text, outcome = ("Something went wrong on my side. "
                             + self._where_things_stand(s, p)), "error"
        self._save(s)
        out = [self._say(s, text)]
        p.tracer.end_run(outcome, phase=s.phase.value)
        return out

    # ------------------------------------------------------------------ screened input
    def _blocked_text(self, hi: bool = False) -> str:
        if hi:
            return ("Baar-baar restricted jaankari maangne ki wajah se yeh account pause hai, "
                    "isliye abhi main messages par kaam nahi kar sakta. Access wapas paane ke "
                    "liye support se contact karein.")
        return ("This account is paused after repeated requests for restricted information, "
                "so I can't act on messages right now. Please contact support to restore "
                "access.")

    def _screened(self, s: Session, p: Parts, sc: Screen, text: str,
                  msg_id: int) -> tuple[list[str], str]:
        """Input handled entirely by code: no model call, no tool call. Replies follow the
        user's language (English or Hinglish), since a template in the wrong language reads
        as a canned brush-off (found by the reply-quality judge)."""
        hi = looks_hinglish(text)
        if sc.kind == "blocked":
            p.tracer.event("blocked_user_input", channel="text", message_id=msg_id)
            return [self._blocked_text(hi)], "blocked"
        if sc.kind == "restricted":
            n = self.store.add_flag(s.user_id, s.session_id, msg_id,
                                    self.clock.now().isoformat(), sc.categories, text)
            limit = self.config.block_after_flags
            p.tracer.event("restricted_request_flagged", message_id=msg_id,
                           categories=sc.categories, flag_count=n, block_after=limit,
                           blocked=n >= limit)
            if n >= limit:
                if hi:
                    return [("Main internal ya doosre customers ki jaankari share nahi kar "
                             "sakta. Baar-baar aisi requests ki wajah se maine yeh account "
                             "pause kar diya hai. Access wapas paane ke liye support se contact "
                             "karein.")], "flagged_blocked"
                return [("I can't share internal or other customers' information. After "
                         "repeated requests like this, I've paused this account. Please "
                         "contact support to restore access.")], "flagged_blocked"
            left = limit - n
            if hi:
                return [("Maaf kijiye, main internal system details, keys ya doosre customers "
                         "ki jaankari share nahi karta. Main aapke liye home services book, "
                         "cancel ya reschedule kar sakta hoon (plumber, electrician, AC "
                         "servicing, house cleaning, carpenter, pest control). Note: yeh "
                         "request log ho gayi hai"
                         + (f"; {left} aur aisi requests par yeh account pause ho jayega."
                            if left else "."))], "flagged"
            return [("I can't help with that: I don't share internal system details, keys or "
                     "other customers' information. I can book, cancel or reschedule home "
                     "services for you (plumber, electrician, AC servicing, house cleaning, "
                     "carpenter, pest control). Note: this request was logged"
                     + (f"; {left} more will pause this account." if left else "."))], "flagged"
        # gibberish
        p.tracer.event("gibberish_input", message_id=msg_id)
        tail = ""
        pending = p.gate.pending_for(s.session_id)
        t = s.task
        if pending or (t and t.hold):
            tail = " " + self._where_things_stand(s, p)
        if hi:
            return [("Maaf kijiye, mujhe samajh nahi aaya. Kya aap dobara likh sakte hain? Main "
                     "home services book, cancel ya reschedule kar sakta hoon, jaise: \"Kal "
                     "subah HSR Layout mein plumber chahiye.\"" + tail)], "gibberish"
        return [("Sorry, I couldn't make sense of that. Could you rephrase? I can book, "
                 "cancel or reschedule home services, for example: \"Find a plumber in HSR "
                 "Layout tomorrow morning.\"" + tail)], "gibberish"

    def _approval_error_text(self, p: Parts, e: ApprovalError) -> str:
        if e.code.startswith("not_pending:consumed") and e.approval:
            return p.executor._replay(e.approval).message
        reason = {"expired": "it expired", "fingerprint_mismatch": "the details changed",
                  "not_found": "I can't find it"}.get(e.code, e.code.replace("_", " "))
        return (f"I didn't act on that confirmation because {reason}. Nothing was done. "
                "Tell me if you'd like me to set it up again.")

    def _route(self, s: Session, p: Parts, text: str, msg_id: int,
               budget: RunBudget) -> tuple[list[str], str]:
        pending = p.gate.pending_for(s.session_id)
        if pending and is_affirmative(text, pending.action):
            p.gate.approve(pending.approval_id, via="text", message_id=msg_id)
            res: Outcome = p.executor.execute(s, pending.approval_id)
            return [res.message], f"executed:{res.status}"
        if not pending and is_affirmative(text):
            last = max(self.store.approvals(s.session_id), key=lambda a: a.created_at,
                       default=None)
            if last is not None and last.status == "consumed":
                return [p.executor._replay(last).message], "duplicate_confirmation"
            if last is not None and last.status in ("invalidated", "expired", "rejected"):
                p.tracer.event("stale_confirmation", approval_id=last.approval_id,
                               status=last.status, reason=last.status_reason)
                return [f"There's nothing waiting for your confirmation right now — the last "
                        f"request is no longer valid ({last.status_reason or last.status}). "
                        "Nothing was done. Tell me if you'd like me to set it up again."], \
                    "stale_confirmation"
        if pending and is_negative(text):
            p.gate.reject(pending, "user_declined")
        return self._model_loop(s, p, budget)

    # ------------------------------------------------------------------ model loop
    def _model_loop(self, s: Session, p: Parts, budget: RunBudget) -> tuple[list[str], str]:
        ctx = ContextBuilder(self.store, self.memory, p.gate, self.clock,
                             self.config.context_budget, self.config.memory_budget,
                             tool_tokens=TOOL_SPEC_TOKENS, token_ratio=self.token_ratio)
        d = ToolDispatcher(s, self.store, self.memory, p.client, p.gate, p.tracer, self.clock,
                           self.reader)
        scratch: list[ChatMessage] = []
        while True:
            budget.charge_model_call()
            messages, manifest = ctx.build(s, scratch)
            p.tracer.event("context_built", **manifest)
            t0 = time.perf_counter()
            resp = self.llm.complete(messages, TOOL_SPECS)
            latency = round((time.perf_counter() - t0) * 1000, 1)
            cost = estimate_cost(resp.model, resp.usage.input_tokens, resp.usage.output_tokens)
            p.tracer.event("model_call", model=resp.model, latency_ms=latency,
                           input_tokens=resp.usage.input_tokens,
                           output_tokens=resp.usage.output_tokens,
                           est_context_tokens=manifest["total_tokens"], cost_usd=cost,
                           replayed=bool((resp.raw or {}).get("replayed")),
                           tool_calls=[{"name": tc.name, "args": tc.args}
                                       for tc in resp.tool_calls],
                           text=resp.text)
            if not (resp.raw or {}).get("replayed"):
                self._calibrate(p, resp.usage.input_tokens, manifest["total_tokens"], ctx)
            budget.check_time()
            if not resp.tool_calls:
                return [self._guard_claims(s, p, d, resp.text or "")], "replied"
            scratch.append(ChatMessage(role="assistant", content=resp.text or "",
                                       tool_calls=resp.tool_calls))
            for tc in resp.tool_calls:
                budget.charge_tool_call()
                out = d.dispatch(tc)
                p.tracer.event("tool_result_to_model", tool=tc.name, result=out.result)
                scratch.append(ChatMessage(role="tool", name=tc.name, tool_call_id=tc.id,
                                           content=tool_result_json(out)))
                if out.reply is not None and (tc.name in TERMINAL_TOOLS or out.terminal):
                    self._save(s)
                    kind = "approval_requested" if tc.name.startswith("propose") else "replied"
                    return [out.reply], kind
            self._save(s)

    def _calibrate(self, p: Parts, reported: int, estimated: int, ctx: ContextBuilder) -> None:
        """Raise the real/estimated token ratio when the provider reports more than we
        estimated. Only ever goes up, capped at 2x, so the cap stays conservative."""
        if reported <= 0 or estimated <= 0:
            return
        observed = reported / estimated
        if observed > self.token_ratio:
            new = min(2.0, round(observed * 1.05, 3))  # small margin over what we saw
            p.tracer.event("token_ratio_calibrated", previous=self.token_ratio, new=new,
                           observed=round(observed, 3), reported=reported, estimated=estimated)
            self.token_ratio = new
            ctx.token_ratio = new

    def _guard_claims(self, s: Session, p: Parts, d: ToolDispatcher, text: str) -> str:
        """The model may not announce a booking/cancellation/reschedule that code did not
        verify in this run (statements about existing bookings it just listed are fine)."""
        if not text.strip():
            return "Sorry, I didn't get that. Could you rephrase?"
        if claims_action(text) or (claims_state(text) and not d.listed_bookings):
            p.tracer.event("unverified_claim_blocked", text=text)
            return ("I can't confirm that anything was booked or changed. "
                    + self._where_things_stand(s, p))
        return text

    # ------------------------------------------------------------------ messages
    def _where_things_stand(self, s: Session, p: Parts) -> str:
        pending = p.gate.pending_for(s.session_id)
        if pending:
            return ("Your confirmation request is still open; reply \"yes\" to go ahead or tell "
                    "me what to change.")
        t = s.task
        if t and t.hold and t.hold.expires_at > self.clock.now().isoformat():
            return (f"I'm holding {t.hold.provider_name} on {when(t.hold.slot)} for "
                    f"{inr(t.hold.quoted_price)}; nothing is booked yet.")
        return "Nothing has been booked or changed."

    def _limit_message(self, s: Session, p: Parts, e: LimitExceeded) -> str:
        what = {"model_calls": "reasoning steps", "tool_calls": "tool calls",
                "wall_clock_seconds": "time"}.get(e.kind, e.kind)
        return (f"I stopped because this request hit my safety limit on {what} "
                f"({e.limit:g}). {self._where_things_stand(s, p)} You can try again or "
                "rephrase.")

    # ------------------------------------------------------------------ background work
    def recover(self) -> dict[str, list[str]]:
        """Run at process start: resolve attempts left pending/unknown by a crash."""
        out: dict[str, list[str]] = {}
        todo = self.store.attempts(status="pending") + self.store.attempts(status="unknown")
        for at in todo:
            s = self.open_session(at.session_id)
            p = self.parts(s.session_id)
            p.tracer.start_run("startup_recovery", attempt_id=at.attempt_id)
            try:
                res = p.executor.recover_attempt(s, at)
            except Exception as e:  # noqa: BLE001
                p.tracer.event("unexpected_error", error=f"{type(e).__name__}: {e}")
                p.tracer.end_run("error")
                continue
            self._save(s)
            if res.status != "unknown":
                out.setdefault(s.session_id, []).append(
                    self._say(s, "Update: " + res.message, recovery=True))
            p.tracer.end_run(f"recovered:{res.status}", phase=s.phase.value)
        return out

    def check_proactive(self) -> dict[str, list[str]]:
        """Poll provider events and act on them without a user prompt."""
        out: dict[str, list[str]] = {}
        for ev in self.provider.poll_events(self.config.user_id):
            if not self.store.mark_event_processed(ev["event_id"], self.clock.now().isoformat()):
                continue
            sessions = self.store.sessions_for_user(self.config.user_id)
            s = sessions[-1] if sessions else self.open_session()
            if s.phase in (Phase.EXECUTING, Phase.VERIFYING):
                s = self.open_session()  # don't disturb an action in flight
            p = self.parts(s.session_id)
            p.tracer.start_run("provider_event", event=ev)
            if self.is_blocked(s.user_id):
                p.tracer.event("proactive_decision", event_id=ev["event_id"], notify=True,
                               reason="user_blocked_no_replacement")
                text = ("Heads-up: one of your bookings was cancelled by the provider. This "
                        "account is paused, so I haven't arranged a replacement. Please contact "
                        "support to restore access.")
                out.setdefault(s.session_id, []).append(self._say(s, text, proactive=True))
                p.tracer.end_run("notified", phase=s.phase.value)
                continue
            try:
                text = handle_provider_cancellation(s, ev, p.client, p.gate, self.memory,
                                                    p.tracer, self.clock)
            except Exception as e:  # noqa: BLE001
                p.tracer.event("unexpected_error", error=f"{type(e).__name__}: {e}")
                text = ("Heads-up: one of your bookings was cancelled by the provider, but I hit "
                        "an error looking for a replacement. Nothing was rebooked.")
            self._save(s)
            if text:
                out.setdefault(s.session_id, []).append(self._say(s, text, proactive=True))
            p.tracer.end_run("notified" if text else "no_action", phase=s.phase.value)
        return out
