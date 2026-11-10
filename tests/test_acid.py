"""ACID inside our database. Each money movement and each "use the approval, record the
attempt" step is one transaction: a crash half-way leaves nothing behind, a retry applies it
exactly once, concurrent workers can't both apply it, and a commit survives a restart.

(The bank and the provider are outside our database, so no transaction can span them; that
part is the saga with idempotency keys and status checks, tested elsewhere.)"""

import sqlite3
import threading

import pytest

from errand_agent.core import payments as payments_mod
from errand_agent.core.clock import FakeClock
from errand_agent.core.payments import Payments
from errand_agent.core.store import AgentStore
from errand_agent.mocks.payments import MockPaymentGateway
from evals import scripts as sc
from evals.harness import build
from evals.invariants import money_matches_bookings


class Crash(Exception):
    pass


def crash_on_status_write(monkeypatch, field):
    """Simulate the process dying after the ledger rows are written but before the payment
    row is updated: the worst moment for a non-atomic implementation."""
    real = Payments._set

    def boom(self, payment_id, **fields):
        if field in fields:
            raise Crash(field)
        return real(self, payment_id, **fields)
    monkeypatch.setattr(Payments, "_set", boom)


def confirmed_card():
    h = build(steps=sc.full_booking_steps(), mock={"forced_book_outcomes": ["success"]})
    h.say(sc.PLUMBER_REQUEST)
    h.say("HSR")
    h.say("Go with your recommendation")
    return h


def booked():
    h = confirmed_card()
    assert h.say("yes")[0].startswith("Booked and verified")
    return h


# ------------------------------------------------------------------ atomicity

def test_capture_is_all_or_nothing(monkeypatch):
    h = confirmed_card()
    crash_on_status_write(monkeypatch, "status")
    with pytest.raises(Crash):
        h.agent.payments._captured(h.agent.tracer("t"), {
            **_authorised(h), "booking_id": None})
    assert h.agent.payments.ledger() == []  # the debit and credit were rolled back too
    assert h.agent.payments.all()[0]["status"] == "authorized"
    monkeypatch.undo()
    h.agent.payments._captured(h.agent.tracer("t"), h.agent.payments.all()[0])
    assert len(h.agent.payments.ledger()) == 2  # applied exactly once on retry


def _authorised(h):
    """Authorise a payment for the open card without booking."""
    a = h.agent.parts(h.session_id).gate.pending_for(h.session_id)
    p, _ = h.agent.payments.authorize_checkout(h.agent.tracer("t"), "user_1", a, "upi")
    return p


def test_payout_is_all_or_nothing(monkeypatch):
    h = booked()
    (p,) = h.agent.payments.all()
    (b,) = h.committed()
    h.mock.config.forced_visit_outcomes[b.booking_id] = "completed"
    h.clock.advance(3 * 24 * 3600)
    crash_on_status_write(monkeypatch, "settled")
    h.agent.run_jobs()  # the job fails (and will retry): nothing may have moved
    assert not [e for e in h.agent.payments.ledger() if e["kind"] == "payout"]
    assert h.agent.payments.get(p["payment_id"])["settled"] == 0
    monkeypatch.undo()
    h.agent.payments.settle(h.agent.tracer("t"), lambda pay: "completed")
    h.agent.payments.settle(h.agent.tracer("t"), lambda pay: "completed")  # a second run
    payouts = [e for e in h.agent.payments.ledger() if e["kind"] == "payout"]
    assert len(payouts) == 2 and sum(e["amount"] for e in payouts) == 0  # one posting
    assert money_matches_bookings(h).ok


def test_using_the_approval_and_recording_the_attempt_is_one_step(monkeypatch):
    h = confirmed_card()
    card = h.agent.parts(h.session_id).gate.pending_for(h.session_id)

    def crash(*a, **k):
        raise Crash("attempt")
    monkeypatch.setattr(h.store, "insert_attempt", crash)
    reply = h.say("yes")[0]  # the runtime catches it and tells the user honestly
    assert "went wrong" in reply
    # rolled back: the approval is not spent (so it isn't stuck), no attempt, nothing booked
    assert h.store.get_approval(card.approval_id).status == "approved"
    assert not h.store.attempts() and not h.calls("book")
    monkeypatch.undo()
    s = h.agent.open_session(h.session_id)
    res = h.agent.parts(h.session_id).executor.execute(s, card.approval_id)
    assert res.status == "confirmed" and len(h.committed()) == 1


# ------------------------------------------------------------------ consistency

def test_the_database_refuses_a_second_capture_posting():
    h = booked()
    (p,) = h.agent.payments.all()
    with pytest.raises(sqlite3.IntegrityError):
        h.agent.payments._post(p["payment_id"], "capture", "again", "customer:user_1",
                               "escrow", p["amount"])
    assert len(h.agent.payments.ledger(p["payment_id"])) == 2
    with pytest.raises(sqlite3.IntegrityError):  # CHECK constraints
        h.store.conn.execute("UPDATE payments SET status='paid-ish'")


# ------------------------------------------------------------------ isolation

def test_two_workers_paying_out_at_once_pay_once(tmp_path):
    path = tmp_path / "agent.db"
    clock = FakeClock()
    gw = MockPaymentGateway(clock)
    a, b = Payments(AgentStore(path), gw, clock), Payments(AgentStore(path), gw, clock)
    tr = build().agent.tracer("t")
    with a.store.tx():
        a.store.conn.execute(
            "INSERT INTO payments (payment_id, attempt_id, approval_id, session_id, user_id, "
            "provider_id, slot, amount, status, booking_id, created_at, updated_at) VALUES "
            "('pay_1','AT-1','AP-1','S-1','user_1','PLB-01','2026-11-01T10:00',900,'captured',"
            "'BK-1','x','x')")
        a._post("pay_1", "capture", "captured", "customer:user_1", "escrow", 900)
    start, errors = threading.Barrier(2), []

    def worker(p):
        start.wait()
        try:
            p.settle(tr, lambda pay: "completed")
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=worker, args=(x,)) for x in (a, b)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    # the second worker waited for the write lock, re-read "settled" and skipped: no clash
    assert errors == []
    payouts = [e for e in a.ledger() if e["kind"] == "payout"]
    assert len(payouts) == 2  # one posting (two rows), not two
    assert a.balances() == {"customer:user_1": -900, "escrow": 0, "provider:PLB-01": 900}


# ------------------------------------------------------------------ durability

def test_committed_survives_a_restart_and_a_rolled_back_one_does_not(tmp_path):
    path = tmp_path / "agent.db"
    s1 = AgentStore(path)
    assert s1.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with s1.tx():
        s1.conn.execute("INSERT INTO counters (name, value) VALUES ('kept', 1)")
    with pytest.raises(Crash), s1.tx():
        s1.conn.execute("INSERT INTO counters (name, value) VALUES ('lost', 1)")
        raise Crash("power cut")
    s2 = AgentStore(path)  # a fresh process opening the same file
    names = {r[0] for r in s2.conn.execute("SELECT name FROM counters")}
    assert "kept" in names and "lost" not in names


def test_every_ledger_posting_balances():
    h = booked()
    for pid in {e["payment_id"] for e in h.agent.payments.ledger()}:
        for kind in ("capture", "refund", "payout"):
            rows = [e for e in h.agent.payments.ledger(pid) if e["kind"] == kind]
            assert sum(e["amount"] for e in rows) == 0
    assert payments_mod.LEDGER_ONCE.startswith("CREATE UNIQUE INDEX")


# ------------------------------------------------------------------ the rest of the writes

def test_a_new_memory_value_never_leaves_the_key_empty(monkeypatch):
    h = build()
    h.agent.memory.add("user_1", "area", "Whitefield", "explicit", evidence="Whitefield")
    monkeypatch.setattr(h.agent.memory, "_insert", _crash_after(h.agent.memory._insert, 1))
    with pytest.raises(Crash):  # dies after superseding Whitefield, before saving Indiranagar
        h.agent.memory.add("user_1", "area", "Indiranagar", "explicit", evidence="Indiranagar")
    active = [m.value for m in h.agent.memory.all("user_1", ("active",)) if m.key == "area"]
    assert active == ["Whitefield"]  # rolled back, not empty


def _crash_after(fn, n_ok):
    calls = {"n": 0}

    def wrapped(*a, **k):
        calls["n"] += 1
        if calls["n"] > n_ok:
            raise Crash("mid-transaction")
        return fn(*a, **k)
    return wrapped


def test_a_new_card_never_leaves_the_chat_with_none(monkeypatch):
    h = confirmed_card()
    gate = h.agent.parts(h.session_id).gate
    old = gate.pending_for(h.session_id)
    monkeypatch.setattr(h.store, "save_approval", _crash_after(h.store.save_approval, 0))
    with pytest.raises(Crash):
        gate.create(h.session(), "book", dict(old.details))
    assert gate.pending_for(h.session_id).approval_id == old.approval_id  # still live


def test_a_provider_cancellation_is_never_lost_in_a_crash(monkeypatch):
    h = booked()
    (b,) = h.committed()
    h.mock.simulate_provider_cancellation(b.booking_id, "Technician unavailable")
    real_say = h.agent._say
    monkeypatch.setattr(h.agent, "_say", _crash_after(real_say, 0))
    with pytest.raises(Crash):
        h.agent.check_proactive()
    assert h.agent.payments.all()[0]["status"] == "captured"  # refund rolled back with it
    monkeypatch.setattr(h.agent, "_say", real_say)
    out = h.agent.check_proactive()  # not marked handled, so it is handled now
    assert "Heads-up" in out[h.session_id][0] and "refunded" in out[h.session_id][0]
    assert h.agent.payments.all()[0]["status"] == "refunded"
    assert money_matches_bookings(h).ok


def test_a_job_that_crashes_midway_reruns_cleanly_and_reminds_once(monkeypatch):
    h = build()
    h.store.add_message("S-test", "user_1", "user", "hi", h.clock.now().isoformat())
    sent = []

    def handler(payload):
        sent.append(h.agent._say(h.agent.open_session("S-test"), "Reminder: x", proactive=True))
        if len(sent) == 1:
            raise Crash("after writing the message, before 'done'")
        return "sent"
    h.agent.jobs.register("remind_once", handler)
    h.agent.jobs.schedule("remind_once", h.clock.now())
    h.agent.jobs.run_due()
    h.clock.advance(60)
    h.agent.jobs.run_due()
    reminders = [m for m in h.store.messages("user_1") if m["content"] == "Reminder: x"]
    assert len(reminders) == 1  # the first message was rolled back with the crashed run


def test_a_crash_between_the_bank_and_our_payment_row_still_charges_exactly_once(monkeypatch):
    h = confirmed_card()
    real = Payments._insert
    monkeypatch.setattr(Payments, "_insert", _crash_after(real, 0))
    assert "went wrong" in h.say("yes")[0]  # the bank authorised; our row was never written
    monkeypatch.setattr(Payments, "_insert", real)
    h2 = h.restart()
    h2.agent.recover()  # finishes the booking and re-links the same authorisation
    (p,) = h2.agent.payments.all()
    assert p["status"] == "captured" and len(h2.committed()) == 1
    assert len(h2.agent.gateway.payments) == 1  # one authorisation at the bank, not two
    assert money_matches_bookings(h2).ok
