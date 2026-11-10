"""Payments (authorise → capture after verification → void / refund, double-entry ledger),
the durable job scheduler, and the jobs built on it (reminders, reconciliation, payouts)."""

from datetime import timedelta

from errand_agent.core.jobs import JobQueue
from errand_agent.llm.scripted import call
from evals import scripts as sc
from evals.harness import build
from evals.invariants import money_matches_bookings


def book(outcome="success", gateway=()):
    h = build(steps=sc.full_booking_steps(), mock={"forced_book_outcomes": [outcome]})
    h.agent.gateway.force(list(gateway))
    h.say(sc.PLUMBER_REQUEST)
    h.say("HSR")
    h.say("Go with your recommendation")
    return h, h.say("yes")[0]


def book_thursday():
    """A visit two days out, so a reminder the evening before is due."""
    h = build(steps=[sc.update(intent="book",
                               fields={"service": "plumber", "area": "HSR Layout",
                                       "date_start": "2026-11-12", "date_end": "2026-11-12"},
                               evidence={"service": "plumber", "area": "HSR",
                                         "date_start": "Thursday", "date_end": "Thursday"}),
                     sc.search(), sc.hold_top(), sc.propose()],
              mock={"forced_book_outcomes": ["success"]})
    assert h.say("plumber in HSR on Thursday")[0].startswith("Please confirm")
    assert h.say("yes")[0].startswith("Booked and verified")
    return h


def gateway_ops(h, op):
    return [c for c in h.agent.gateway.calls if c["op"] == op]


def test_paid_only_after_the_booking_is_verified():
    h, msg = book()
    (p,) = h.agent.payments.all()
    (b,) = h.committed()
    assert msg.startswith("Booked and verified") and f"Paid: ₹{p['amount']:,}" in msg
    assert p["status"] == "captured" and p["booking_id"] == b.booking_id
    order = [e["type"] for e in h.events if e["type"] in
             ("payment_authorized", "action_confirmed", "payment_captured")]
    assert order == ["payment_authorized", "action_confirmed", "payment_captured"]
    assert h.agent.payments.balances() == {"customer:user_1": -p["amount"],
                                           "escrow": p["amount"]}
    assert money_matches_bookings(h).ok


def test_slot_taken_voids_the_authorisation_and_charges_nothing():
    h, msg = book("slot_taken")
    (p,) = h.agent.payments.all()
    assert "nothing was charged" in msg and p["status"] == "voided"
    assert h.agent.payments.ledger() == [] and not h.committed()


def test_declined_card_books_nothing():
    h, msg = book(gateway=["decline"])
    assert "declined" in msg and not h.calls("book") and not h.agent.payments.all()


def test_capture_timeout_after_success_is_checked_not_repeated():
    # the bank took the money but the answer was lost: ask, don't capture twice
    h, msg = book(gateway=["success", "timeout_after_success"])
    (p,) = h.agent.payments.all()
    assert p["status"] == "captured" and "Paid:" in msg
    assert len(gateway_ops(h, "capture")) == 1 and gateway_ops(h, "get_payment")
    assert len(h.agent.payments.ledger(p["payment_id"])) == 2  # posted exactly once


def test_unanswered_capture_is_reconciled_by_a_job():
    h, msg = book(gateway=["success", "timeout", "timeout", "timeout"])
    (p,) = h.agent.payments.all()
    assert p["status"] == "capture_unknown" and "never be charged twice" in msg
    (job,) = h.agent.jobs.jobs("reconcile_payment")
    h.clock.advance(120)
    h.agent.run_jobs()
    assert h.agent.payments.get(p["payment_id"])["status"] == "captured"
    assert h.agent.jobs.jobs("reconcile_payment")[0]["status"] == "done"
    assert money_matches_bookings(h).ok


def test_user_cancellation_refunds_through_the_ledger():
    h, _ = book()
    (b,) = h.committed()
    h.llm.extend([call("list_bookings"), call("propose_cancel", {"booking_id": b.booking_id})])
    assert h.say("cancel my plumber booking")[0].startswith("Please confirm this cancellation")
    msg = h.say("yes")[0]
    (p,) = h.agent.payments.all()
    assert "Refunded: ₹" in msg and p["status"] == "refunded"
    assert all(v == 0 for v in h.agent.payments.balances().values())
    assert money_matches_bookings(h).ok


def test_provider_cancellation_refunds_and_says_so():
    h, _ = book()
    (b,) = h.committed()
    h.mock.simulate_provider_cancellation(b.booking_id, "Technician unavailable")
    out = h.proactive()
    assert "has been refunded" in out[h.session_id][0]
    assert h.agent.payments.all()[0]["status"] == "refunded"
    assert money_matches_bookings(h).ok


def test_reminder_the_evening_before_and_skipped_if_cancelled():
    h = book_thursday()
    (b,) = h.committed()
    (job,) = h.agent.jobs.jobs("visit_reminder")
    assert job["run_at"].startswith("2026-11-11T18:00")  # the evening before Thursday
    h.agent.run_jobs()
    assert not [m for m in h.store.messages("user_1") if m["content"].startswith("Reminder")]
    h.clock._now = h.clock._now.fromisoformat(job["run_at"]) + timedelta(seconds=1)
    out = h.agent.run_jobs()
    assert out[h.session_id][0].startswith("Reminder: ") and b.booking_id in out[h.session_id][0]
    assert h.agent.jobs.jobs("visit_reminder")[0]["status"] == "done"

    h2 = book_thursday()
    (b2,) = h2.committed()
    h2.mock.simulate_provider_cancellation(b2.booking_id, "x")
    (j2,) = h2.agent.jobs.jobs("visit_reminder")
    h2.clock._now = h2.clock._now.fromisoformat(j2["run_at"]) + timedelta(seconds=1)
    assert h2.agent.run_jobs() == {}
    assert h2.agent.jobs.jobs("visit_reminder")[0]["result"] == '"skipped (cancelled_by_provider)"'


def run_nightly(h, days):
    h.clock.advance(days * 24 * 3600)
    return h.agent.run_jobs()


def test_provider_paid_only_for_a_completed_visit_after_the_dispute_window():
    h, _ = book()
    (p,) = h.agent.payments.all()
    (b,) = h.committed()
    h.mock.config.forced_visit_outcomes[b.booking_id] = "completed"
    assert p["slot"].startswith("2026-11-10T")  # the visit is today
    h.clock._now = h.clock._now.replace(day=11, hour=2, minute=1)  # first night: 15 h later
    h.agent.run_jobs()
    assert h.agent.payments.get(p["payment_id"])["settled"] == 0  # inside the 24 h window
    run_nightly(h, 1)                                               # second night
    assert h.agent.payments.get(p["payment_id"])["settled"] == 1
    bal = h.agent.payments.balances()
    assert bal["escrow"] == 0 and bal[f"provider:{p['provider_id']}"] == p["amount"]
    assert len(h.agent.jobs.jobs("settle_payouts")) >= 2  # it re-schedules itself
    assert money_matches_bookings(h).ok


def test_no_show_is_refunded_never_paid_out_and_the_user_is_told():
    h, _ = book()
    (p,) = h.agent.payments.all()
    (b,) = h.committed()
    h.mock.config.forced_visit_outcomes[b.booking_id] = "no_show"
    out = run_nightly(h, 4)
    row = h.agent.payments.get(p["payment_id"])
    assert row["status"] == "refunded" and row["settled"] == 0
    assert "didn't happen" in out[h.session_id][0] and "refunded" in out[h.session_id][0]
    assert all(v == 0 for v in h.agent.payments.balances().values())
    assert money_matches_bookings(h).ok


def test_about_half_of_past_visits_are_no_shows_and_the_answer_is_stable():
    h, _ = book()
    (b,) = h.committed()
    h.clock.advance(4 * 24 * 3600)
    outcomes = []
    for i in range(400):
        clone = b.model_copy(update={"booking_id": f"BK-{i:04d}"})
        outcomes.append(h.mock._booking_view(clone)["status"])
    assert 0.4 < outcomes.count("no_show") / len(outcomes) < 0.6
    assert set(outcomes) == {"completed", "no_show"}
    assert h.mock._booking_view(b)["status"] == h.mock._booking_view(b)["status"]


def test_job_queue_claims_once_retries_with_backoff_and_survives_restarts():
    h = build()
    q = JobQueue(h.store, h.clock)
    runs = []
    q.register("flaky", lambda p: runs.append(p) or (_ for _ in ()).throw(ValueError("no")))
    q.register("ok", lambda p: runs.append(p) or "fine")
    assert q.schedule("ok", h.clock.now(), {"n": 1}, dedupe_key="once")
    assert q.schedule("ok", h.clock.now(), {"n": 2}, dedupe_key="once") is None  # deduped
    (job,) = q.jobs("ok")
    assert q._claim(job["job_id"], "w1", h.clock.now())
    assert not q._claim(job["job_id"], "w2", h.clock.now())  # compare-and-set: one winner
    h.clock.advance(3 * 60)  # w1 died; its lease ran out
    assert q.run_due("w2")[0]["status"] == "done"

    q.schedule("flaky", h.clock.now(), max_attempts=2)
    assert q.run_due()[0]["status"] == "retry"
    assert q.run_due() == []  # backing off
    h.clock.advance(31)
    assert q.run_due()[0]["status"] == "dead"
    restarted = JobQueue(h.store, h.clock)  # rows, not timers: a restart sees everything
    assert {j["status"] for j in restarted.jobs() if j["kind"] in ("ok", "flaky")} == \
        {"done", "dead"}
