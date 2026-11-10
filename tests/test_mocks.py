"""Contract tests for the mock provider environment."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

import pytest

from errand_agent.core.clock import FakeClock
from errand_agent.mocks.catalog import AREAS, INJECTION_REVIEWS, SERVICES
from errand_agent.mocks.config import MockConfig
from errand_agent.mocks.errors import ServiceUnavailable, ToolInternalError, ToolTimeout
from errand_agent.mocks.service import MockProviderService

WEEK = {"start": "2026-11-10", "end": "2026-11-16"}


def make(**cfg) -> tuple[MockProviderService, FakeClock]:
    clock = FakeClock()
    return MockProviderService(MockConfig(**cfg), clock=clock), clock


def first_slot(svc: MockProviderService, provider_id: str = "PLB-02") -> str:
    res = svc.search_providers("plumber", "HSR Layout", WEEK)
    p = next(p for p in res["providers"] if p["provider_id"] == provider_id)
    return p["available_slots"][0]


@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize("area", AREAS)
def test_search_returns_3_to_8_providers_with_required_fields(service, area):
    svc, _ = make()
    res = svc.search_providers(service, area, WEEK)
    assert res["status"] == "ok"
    assert 3 <= len(res["providers"]) <= 8
    for p in res["providers"]:
        assert {"provider_id", "price", "rating", "available_slots", "attributes"} <= p.keys()


def test_search_accepts_aliases_and_rejects_unknowns():
    svc, _ = make()
    assert svc.search_providers("AC servicing", "hsr", WEEK)["status"] == "ok"
    assert svc.search_providers("astrologer", "HSR Layout", WEEK)["error"] == "unknown_service"
    assert svc.search_providers("plumber", "Mars", WEEK)["error"] == "unknown_area"
    assert svc.search_providers("plumber", "HSR", {"start": "x"})["error"] == "bad_date_range"


def test_search_is_deterministic_for_a_seed():
    a, _ = make(seed=7)
    b, _ = make(seed=7)
    assert a.search_providers("plumber", "HSR", WEEK) == b.search_providers("plumber", "HSR", WEEK)


def test_get_provider_returns_reviews_and_some_contain_injection():
    svc, _ = make()
    assert len(INJECTION_REVIEWS) >= 1
    res = svc.get_provider("PLB-02")
    texts = " ".join(r["text"] for r in res["reviews"])
    assert "Ignore all previous instructions" in texts
    assert res["phone"].startswith("+91")


def test_hold_has_ten_minute_expiry_and_quote():
    svc, clock = make(hold_price_drift_prob=0.0)
    slot = first_slot(svc)
    h = svc.hold_slot("PLB-02", slot)
    assert h["status"] == "ok" and h["hold_id"].startswith("H-")
    assert h["quoted_price"] == h["search_price"] == 1250
    assert h["expires_at"] == (clock.now() + timedelta(minutes=10)).isoformat()


def test_hold_quote_can_differ_from_search_price():
    svc, _ = make(forced_hold_quotes=[1600])
    h = svc.hold_slot("PLB-02", first_slot(svc))
    assert h["quoted_price"] == 1600 and h["search_price"] == 1250


def test_held_slot_is_not_holdable_twice_until_expiry():
    svc, clock = make()
    slot = first_slot(svc)
    assert svc.hold_slot("PLB-02", slot)["status"] == "ok"
    assert svc.hold_slot("PLB-02", slot)["error"] == "slot_unavailable"
    clock.advance(601)
    assert svc.hold_slot("PLB-02", slot)["status"] == "ok"


def test_book_outcome_distribution_matches_spec():
    svc, _ = make(seed=123)
    counts = Counter(svc._book_outcome(f"H-{i}", 1) for i in range(20000))
    n = sum(counts.values())
    assert abs(counts["success"] / n - 0.70) < 0.02
    assert abs(counts["slot_taken"] / n - 0.15) < 0.02
    timeouts = counts["timeout_booked"] + counts["timeout_not_booked"]
    assert abs(timeouts / n - 0.15) < 0.02
    assert abs(counts["timeout_booked"] / timeouts - 0.5) < 0.05


def test_book_success_and_idempotent_replay():
    svc, _ = make(forced_book_outcomes=["success"])
    h = svc.hold_slot("PLB-02", first_slot(svc))
    r1 = svc.book(h["hold_id"], "key-1")
    r2 = svc.book(h["hold_id"], "key-1")
    assert r1["status"] == "success" and r1 == r2
    assert len(svc.committed_bookings()) == 1


def test_timeout_after_success_is_visible_only_via_status():
    svc, _ = make(forced_book_outcomes=["timeout_booked"])
    h = svc.hold_slot("PLB-02", first_slot(svc))
    with pytest.raises(ToolTimeout):
        svc.book(h["hold_id"], "key-1")
    status = svc.get_booking_status(idempotency_key="key-1")
    assert status["status"] == "booked"
    assert svc.get_booking_status(hold_id=h["hold_id"])["status"] == "booked"
    # Retrying with the same key must not create a second booking.
    assert svc.book(h["hold_id"], "key-1")["booking_id"] == status["booking_id"]
    assert len(svc.committed_bookings()) == 1


def test_timeout_without_booking_then_retry_with_same_key():
    svc, _ = make(forced_book_outcomes=["timeout_not_booked", "success"])
    h = svc.hold_slot("PLB-02", first_slot(svc))
    with pytest.raises(ToolTimeout):
        svc.book(h["hold_id"], "key-1")
    assert svc.get_booking_status(idempotency_key="key-1")["status"] == "not_found"
    assert svc.get_booking_status(hold_id=h["hold_id"])["status"] == "not_booked"
    assert svc.book(h["hold_id"], "key-1")["status"] == "success"
    assert len(svc.committed_bookings()) == 1


def test_slot_taken_marks_slot_unavailable():
    svc, _ = make(forced_book_outcomes=["slot_taken"])
    slot = first_slot(svc)
    h = svc.hold_slot("PLB-02", slot)
    assert svc.book(h["hold_id"], "k")["status"] == "slot_taken"
    assert svc.get_booking_status(idempotency_key="k")["status"] == "failed"
    assert svc.hold_slot("PLB-02", slot)["error"] == "slot_unavailable"


def test_book_after_expiry_returns_hold_expired():
    svc, clock = make()
    h = svc.hold_slot("PLB-02", first_slot(svc))
    clock.advance(601)
    assert svc.book(h["hold_id"], "k")["status"] == "hold_expired"
    assert svc.committed_bookings() == []


def test_cancel_fails_first_with_503_then_succeeds_idempotently():
    svc, _ = make()
    upcoming = next(b for b in svc.list_bookings("user_1")["bookings"] if b["when"] == "upcoming")
    with pytest.raises(ServiceUnavailable):
        svc.cancel(upcoming["booking_id"], "c-1")
    assert svc.cancel(upcoming["booking_id"], "c-1")["status"] == "cancelled"
    assert svc.cancel(upcoming["booking_id"], "c-1")["status"] == "cancelled"


def test_cancel_always_fail_knob():
    svc, _ = make(cancel_always_fail=True)
    bid = next(b for b in svc.list_bookings("user_1")["bookings"] if b["when"] == "upcoming")
    for _ in range(3):
        with pytest.raises(ServiceUnavailable):
            svc.cancel(bid["booking_id"], "c-1")


def test_reschedule_conflict_on_configured_slot_then_success():
    svc, _ = make()
    ac = next(b for b in svc.list_bookings("user_1")["bookings"] if b["service"] == "ac_servicing")
    res = svc.search_providers("ac_servicing", "HSR", WEEK)
    slots = next(p for p in res["providers"] if p["provider_id"] == "ACS-01")["available_slots"]
    svc.config.conflict_slots = [slots[0]]
    assert svc.reschedule(ac["booking_id"], slots[0], "r-1")["status"] == "slot_conflict"
    ok = svc.reschedule(ac["booking_id"], slots[1], "r-2")
    assert ok["status"] == "rescheduled" and ok["new_slot"] == slots[1]


def test_list_bookings_has_seeded_history():
    svc, _ = make()
    rows = svc.list_bookings("user_1")["bookings"]
    services = {(b["service"], b["when"]) for b in rows}
    assert ("electrician", "past") in services
    assert ("ac_servicing", "upcoming") in services
    assert all({"booking_id", "provider_id", "service", "slot", "status"} <= b.keys() for b in rows)


def test_state_survives_restart(tmp_path):
    db = tmp_path / "mock.db"
    clock = FakeClock()
    svc = MockProviderService(MockConfig(forced_book_outcomes=["timeout_booked"]), clock, db)
    h = svc.hold_slot("PLB-02", first_slot(svc))
    with pytest.raises(ToolTimeout):
        svc.book(h["hold_id"], "key-1")
    reopened = MockProviderService(MockConfig(), clock, db)
    assert reopened.get_booking_status(idempotency_key="key-1")["status"] == "booked"
    assert len(reopened.list_bookings("user_1")["bookings"]) == 6  # 5 seeded + 1


def test_provider_cancellation_event_feed():
    svc, _ = make()
    bid = next(b for b in svc.list_bookings("user_1")["bookings"] if b["when"] == "upcoming")
    svc.simulate_provider_cancellation(bid["booking_id"], "Technician sick")
    events = svc.poll_events("user_1")
    assert len(events) == 1 and events[0]["type"] == "booking_cancelled_by_provider"
    assert svc.poll_events("user_1") == []
    row = next(b for b in svc.list_bookings("user_1")["bookings"]
               if b["booking_id"] == bid["booking_id"])
    assert row["status"] == "cancelled_by_provider"


def test_fault_injection_slow_and_broken():
    svc, clock = make(slow_tools={"search_providers": 5.0},
                      broken_tools={"get_provider": "exception", "list_bookings": "malformed"})
    t0 = clock.monotonic()
    svc.search_providers("plumber", "HSR", WEEK)
    assert clock.monotonic() - t0 == 5.0
    with pytest.raises(ToolInternalError):
        svc.get_provider("PLB-02")
    assert "unexpected" in svc.list_bookings("user_1")
