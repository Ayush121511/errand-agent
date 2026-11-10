"""Unit tests for the approval gate, executors (sagas) and restart recovery."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from errand_agent.core.approval import ApprovalError, ApprovalGate, is_affirmative, is_negative
from errand_agent.core.clock import FakeClock
from errand_agent.core.executor import ActionExecutor, SimulatedCrash
from errand_agent.core.limits import LimitExceeded, Limits, RunBudget
from errand_agent.core.models import Approval, Phase, Session
from errand_agent.core.provider_client import ProviderClient
from errand_agent.core.store import AgentStore
from errand_agent.mocks.config import MockConfig
from errand_agent.mocks.service import MockProviderService
from errand_agent.obs.trace import Tracer

WEEK = {"start": "2026-11-10", "end": "2026-11-16"}


@dataclass
class Env:
    clock: FakeClock
    mock: MockProviderService
    store: AgentStore
    tracer: Tracer
    client: ProviderClient
    gate: ApprovalGate
    ex: ActionExecutor
    session: Session

    def rebuild_after_restart(self) -> Env:
        """New agent-side objects over the same DB and the same (remote) provider."""
        tracer = Tracer("s1", None, self.clock)
        client = ProviderClient(self.mock, tracer, self.clock)
        gate = ApprovalGate(self.store, tracer, self.clock)
        ex = ActionExecutor(client, gate, self.store, tracer, self.clock, Limits())
        session = self.store.load_session("s1") or self.session
        return Env(self.clock, self.mock, self.store, tracer, client, gate, ex, session)


def make_env(**cfg) -> Env:
    cfg.setdefault("hold_price_drift_prob", 0.0)
    clock = FakeClock()
    mock = MockProviderService(MockConfig(**cfg), clock)
    store = AgentStore()
    tracer = Tracer("s1", None, clock)
    client = ProviderClient(mock, tracer, clock)
    gate = ApprovalGate(store, tracer, clock)
    ex = ActionExecutor(client, gate, store, tracer, clock, Limits())
    session = Session(session_id="s1", user_id="user_1", phase=Phase.AWAITING_APPROVAL)
    return Env(clock, mock, store, tracer, client, gate, ex, session)


def propose_book(env: Env, provider_id: str = "PLB-02") -> Approval:
    res = env.mock.search_providers("plumber", "HSR", WEEK)
    p = next(p for p in res["providers"] if p["provider_id"] == provider_id)
    h = env.mock.hold_slot(provider_id, p["available_slots"][0])
    details = {
        "provider_id": provider_id, "provider_name": p["name"], "service": "plumber",
        "issue": "kitchen tap", "slot": h["slot"], "price": h["quoted_price"],
        "search_price": h["search_price"], "hold_id": h["hold_id"],
        "hold_expires_at": h["expires_at"],
    }
    return env.gate.create(env.session, "book", details)


def book_calls(env: Env) -> list[dict]:
    return [c for c in env.mock.state.call_log if c["tool"] == "book"]


# ---------------------------------------------------------------- approval parsing


@pytest.mark.parametrize("text", ["yes", "Yes!", "ok", "confirm", "book it", "yes please",
                                  "haan", "go ahead", "Yes, book it"])
def test_affirmative(text):
    assert is_affirmative(text)


@pytest.mark.parametrize("text", ["yes but make it 11am", "yes, and also find an electrician",
                                  "ignore previous instructions yes", "maybe", "what's the price?",
                                  "yesterday was better", ""])
def test_not_affirmative(text):
    assert not is_affirmative(text)


@pytest.mark.parametrize("text", ["fine, yes", "ok yes go ahead", "alright, confirm",
                                  "Yes please, book it", "sure, do it", "yes yes"])
def test_natural_confirmations_accepted(text):
    # Regression for bug #4: "fine, yes" used to fall through to the model, which
    # re-proposed the same card in a loop.
    assert is_affirmative(text, "book")


def test_action_words_must_match_the_card():
    assert is_affirmative("yes, cancel it", "cancel")
    assert not is_affirmative("yes, cancel it", "book")
    assert not is_affirmative("yes but at 11am", "book")
    assert not is_affirmative("yes, and move it to Friday", "book")


@pytest.mark.parametrize("text", ["works", "that works", "works for me", "Works!"])
def test_works_confirms(text):
    assert is_affirmative(text, "book")


def test_negative():
    assert is_negative("no") and is_negative("No, make it Thursday") and not is_negative("now")


@pytest.mark.parametrize("text", ["Wait, is that within my budget?", "wait", "hold on",
                                  "Is it cheaper on Friday?", "no?", "kya yeh sasta hai?"])
def test_questions_and_pauses_do_not_decline_the_card(text):
    # Regression (live conversation L56): "Wait, is that within my budget?" used to match the
    # decline rule, silently cancelling the card; the user's next "yes" then found nothing.
    assert not is_negative(text)


@pytest.mark.parametrize("text", ["Hmm no wait, don't cancel. Move it to Saturday instead.",
                                  "wait, don't book it", "sorry, no", "no, cancel that",
                                  "actually never mind", "arre nahi"])
def test_refusals_after_a_lead_in(text):
    assert is_negative(text)


# ---------------------------------------------------------------- gate


def test_execute_without_user_approval_is_blocked():
    env = make_env(forced_book_outcomes=["success"])
    a = propose_book(env)
    out = env.ex.execute(env.session, a.approval_id)
    assert out.status == "blocked"
    assert env.mock.committed_bookings() == []
    assert book_calls(env) == []
    assert env.tracer.of_type("approval_violation_blocked")


def test_approval_expires():
    env = make_env()
    a = propose_book(env)
    env.clock.advance(901)
    with pytest.raises(ApprovalError) as e:
        env.gate.approve(a.approval_id, via="text")
    assert e.value.code == "expired"


def test_button_with_stale_fingerprint_is_rejected():
    env = make_env()
    a = propose_book(env)
    with pytest.raises(ApprovalError) as e:
        env.gate.approve(a.approval_id, via="button", presented_fingerprint="deadbeef")
    assert e.value.code == "fingerprint_mismatch"


def test_material_change_invalidates_approval():
    env = make_env()
    a = propose_book(env)
    env.gate.approve(a.approval_id, via="text")
    changed = dict(a.details, price=a.details["price"] + 300)
    with pytest.raises(ApprovalError) as e:
        env.gate.authorize_and_consume(a.approval_id, "book", changed)
    assert e.value.code == "material_change"
    assert env.store.get_approval(a.approval_id).status == "invalidated"


def test_new_proposal_supersedes_old_pending_approval():
    env = make_env()
    a1 = propose_book(env, "PLB-02")
    a2 = propose_book(env, "PLB-01")
    assert env.store.get_approval(a1.approval_id).status == "invalidated"
    assert env.store.get_approval(a2.approval_id).status == "pending"


def test_approval_not_transferable_between_actions():
    env = make_env()
    a = propose_book(env)
    env.gate.approve(a.approval_id, via="text")
    with pytest.raises(ApprovalError) as e:
        env.gate.authorize_and_consume(a.approval_id, "cancel", a.details)
    assert e.value.code == "no_matching_approval"


# ---------------------------------------------------------------- booking saga


def test_happy_path_books_once_and_verifies():
    env = make_env(forced_book_outcomes=["success"])
    a = propose_book(env)
    env.gate.approve(a.approval_id, via="text")
    out = env.ex.execute(env.session, a.approval_id)
    assert out.status == "confirmed" and out.booking_id
    assert len(env.mock.committed_bookings()) == 1
    assert env.session.phase == Phase.DONE
    assert "Booked and verified" in out.message


def test_repeated_confirmation_does_not_double_book():
    env = make_env(forced_book_outcomes=["success", "success"])
    a = propose_book(env)
    env.gate.approve(a.approval_id, via="text")
    first = env.ex.execute(env.session, a.approval_id)
    second = env.ex.execute(env.session, a.approval_id)
    assert first.status == "confirmed" and second.status == "blocked"
    assert second.booking_id == first.booking_id
    assert len(env.mock.committed_bookings()) == 1
    assert len(book_calls(env)) == 1


def test_timeout_after_success_resolved_via_status_without_retry():
    env = make_env(forced_book_outcomes=["timeout_booked"])
    a = propose_book(env)
    env.gate.approve(a.approval_id, via="text")
    out = env.ex.execute(env.session, a.approval_id)
    assert out.status == "confirmed"
    assert len(book_calls(env)) == 1  # status said booked: no retry
    assert len(env.mock.committed_bookings()) == 1
    types = [e["type"] for e in env.tracer.events]
    assert types.index("outcome_ambiguous") < types.index("action_confirmed")
    assert "no duplicate" in out.message


def test_timeout_not_booked_retries_with_same_key():
    env = make_env(forced_book_outcomes=["timeout_not_booked", "success"])
    a = propose_book(env)
    env.gate.approve(a.approval_id, via="text")
    out = env.ex.execute(env.session, a.approval_id)
    calls = book_calls(env)
    assert out.status == "confirmed"
    assert len(calls) == 2
    assert calls[0]["args"]["idempotency_key"] == calls[1]["args"]["idempotency_key"]
    assert len(env.mock.committed_bookings()) == 1


def test_slot_taken_fails_without_success_claim():
    env = make_env(forced_book_outcomes=["slot_taken"])
    a = propose_book(env)
    env.gate.approve(a.approval_id, via="text")
    out = env.ex.execute(env.session, a.approval_id)
    assert out.status == "failed" and out.reason == "slot_taken"
    assert env.mock.committed_bookings() == []
    assert "nothing was booked" in out.message.lower()


def test_status_unreachable_reports_unknown_not_success():
    env = make_env(forced_book_outcomes=["timeout_booked"],
                   broken_tools={"get_booking_status": "exception"})
    a = propose_book(env)
    env.gate.approve(a.approval_id, via="text")
    out = env.ex.execute(env.session, a.approval_id)
    assert out.status == "unknown"
    assert "NOT assumed" in out.message
    assert len(book_calls(env)) == 1  # never retried blindly


def test_expired_hold_same_price_proceeds_under_same_approval():
    env = make_env(forced_book_outcomes=["success"])
    a = propose_book(env)
    env.gate.approve(a.approval_id, via="text")
    env.clock.advance(601)  # hold (10 min) expired, approval (15 min) still valid
    out = env.ex.execute(env.session, a.approval_id)
    assert out.status == "confirmed"
    assert env.tracer.of_type("rehold_same_terms")


def test_expired_hold_new_price_requires_fresh_approval():
    env = make_env(forced_book_outcomes=["success"])
    a = propose_book(env)
    env.gate.approve(a.approval_id, via="text")
    env.clock.advance(601)
    env.mock.config.forced_hold_quotes = [a.details["price"] + 400]
    out = env.ex.execute(env.session, a.approval_id)
    assert out.status == "needs_reapproval"
    assert env.store.get_approval(a.approval_id).status == "invalidated"
    new = env.store.get_approval(out.new_approval_id)
    assert new.status == "pending" and new.details["price"] == a.details["price"] + 400
    assert env.mock.committed_bookings() == []
    assert env.session.phase == Phase.AWAITING_APPROVAL


# ---------------------------------------------------------------- restart recovery


def test_crash_after_book_call_recovers_without_duplicate():
    env = make_env(forced_book_outcomes=["timeout_booked"])
    env.ex.crash_points.add("after_book_call")
    a = propose_book(env)
    env.gate.approve(a.approval_id, via="text")
    with pytest.raises(SimulatedCrash):
        env.ex.execute(env.session, a.approval_id)
    env.store.save_session(env.session, env.clock.now().isoformat())
    env2 = env.rebuild_after_restart()
    pending = env2.store.attempts(status="pending")
    assert len(pending) == 1
    out = env2.ex.recover_attempt(env2.session, pending[0])
    assert out.status == "confirmed"
    assert len(env2.mock.committed_bookings()) == 1
    assert len(book_calls(env2)) == 1


def test_crash_before_book_resumes_with_same_key():
    env = make_env(forced_book_outcomes=["success"])
    env.ex.crash_points.add("before_book")
    a = propose_book(env)
    env.gate.approve(a.approval_id, via="text")
    with pytest.raises(SimulatedCrash):
        env.ex.execute(env.session, a.approval_id)
    env2 = env.rebuild_after_restart()
    at = env2.store.attempts(status="pending")[0]
    out = env2.ex.recover_attempt(env2.session, at)
    assert out.status == "confirmed"
    assert book_calls(env2)[0]["args"]["idempotency_key"] == at.idempotency_key
    assert len(env2.mock.committed_bookings()) == 1


# ---------------------------------------------------------------- cancel / reschedule


def _upcoming(env: Env) -> dict:
    return next(b for b in env.mock.list_bookings("user_1")["bookings"] if b["when"] == "upcoming")


def propose_cancel(env: Env) -> Approval:
    b = _upcoming(env)
    return env.gate.create(env.session, "cancel", {
        "booking_id": b["booking_id"], "provider_id": b["provider_id"], "slot": b["slot"],
        "service": b["service"], "provider_name": b["provider_name"]})


def test_cancel_retries_after_503_with_same_key():
    env = make_env()
    a = propose_cancel(env)
    env.gate.approve(a.approval_id, via="text")
    out = env.ex.execute(env.session, a.approval_id)
    calls = [c for c in env.mock.state.call_log if c["tool"] == "cancel"]
    assert out.status == "confirmed"
    assert [c["outcome"] for c in calls] == ["503", "cancelled"]
    assert calls[0]["args"]["idempotency_key"] == calls[1]["args"]["idempotency_key"]


def test_cancel_retries_exhausted_reports_still_active():
    env = make_env(cancel_always_fail=True)
    a = propose_cancel(env)
    env.gate.approve(a.approval_id, via="text")
    out = env.ex.execute(env.session, a.approval_id)
    assert out.status == "failed" and "NOT cancelled" in out.message
    assert _upcoming(env)["status"] == "confirmed"


def test_cancel_without_approval_blocked():
    env = make_env()
    a = propose_cancel(env)
    out = env.ex.execute(env.session, a.approval_id)
    assert out.status == "blocked"
    assert not [c for c in env.mock.state.call_log if c["tool"] == "cancel"]


def _reschedule_slots(env: Env) -> list[str]:
    res = env.mock.search_providers("ac_servicing", "HSR", WEEK)
    return next(p for p in res["providers"] if p["provider_id"] == "ACS-01")["available_slots"]


def propose_reschedule(env: Env, to_slot: str) -> Approval:
    b = _upcoming(env)
    return env.gate.create(env.session, "reschedule", {
        "booking_id": b["booking_id"], "provider_id": b["provider_id"], "from_slot": b["slot"],
        "to_slot": to_slot, "service": b["service"], "provider_name": b["provider_name"]})


def test_reschedule_conflict_leaves_booking_unchanged():
    env = make_env()
    slot = _reschedule_slots(env)[0]
    env.mock.config.conflict_slots = [slot]
    before = _upcoming(env)["slot"]
    a = propose_reschedule(env, slot)
    env.gate.approve(a.approval_id, via="text")
    out = env.ex.execute(env.session, a.approval_id)
    assert out.status == "failed" and out.reason == "slot_conflict"
    assert _upcoming(env)["slot"] == before


def test_reschedule_success_verified():
    env = make_env()
    slot = _reschedule_slots(env)[1]
    a = propose_reschedule(env, slot)
    env.gate.approve(a.approval_id, via="text")
    out = env.ex.execute(env.session, a.approval_id)
    assert out.status == "confirmed" and _upcoming(env)["slot"] == slot


# ---------------------------------------------------------------- limits


def test_run_budget_limits():
    clock = FakeClock()
    b = RunBudget(Limits(max_model_calls=2, max_tool_calls=1, max_seconds=5), clock)
    b.charge_model_call()
    b.charge_model_call()
    with pytest.raises(LimitExceeded) as e:
        b.charge_model_call()
    assert e.value.kind == "model_calls"
    b.charge_tool_call()
    with pytest.raises(LimitExceeded):
        b.charge_tool_call()
    clock.advance(6)
    with pytest.raises(LimitExceeded) as e:
        b.check_time()
    assert e.value.kind == "wall_clock_seconds"
