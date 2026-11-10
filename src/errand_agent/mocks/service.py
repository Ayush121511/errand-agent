"""In-process mock of the home-services provider platform.

It implements the eight tool contracts from the assignment with their failure modes.
State is persisted to its own SQLite file (separate from the agent's), so it behaves
like a remote service that survives an agent process restart.

Design notes:
- Mutating endpoints honour idempotency keys server-side: a repeated key returns the
  stored result instead of acting twice (as real booking/payment APIs do).
- `book` timeouts are raised *after* the side effect may have been committed; only
  `get_booking_status` tells the truth.
- All randomness is seeded per call identity (seed + hold id + attempt), so outcomes do
  not depend on call order in other parts of a test.
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from errand_agent.core.clock import IST, Clock, RealClock
from errand_agent.mocks.catalog import (
    Provider,
    build_catalog,
    normalize_area,
    normalize_service,
)
from errand_agent.mocks.config import BookOutcome, MockConfig
from errand_agent.mocks.errors import ServiceUnavailable, ToolInternalError, ToolTimeout

SLOT_FMT = "%Y-%m-%dT%H:%M"
MAX_RANGE_DAYS = 14


def parse_slot(slot: str) -> datetime:
    return datetime.strptime(slot, SLOT_FMT).replace(tzinfo=IST)


def fmt_slot(dt: datetime) -> str:
    return dt.astimezone(IST).strftime(SLOT_FMT)


class Hold(BaseModel):
    hold_id: str
    provider_id: str
    slot: str
    search_price: int
    quoted_price: int
    created_at: str
    expires_at: str
    status: Literal["active", "used", "lost"] = "active"
    user_id: str | None = None  # who placed it (the authenticated caller)


class Booking(BaseModel):
    booking_id: str
    user_id: str
    provider_id: str
    provider_name: str
    service: str
    slot: str
    price: int
    technician: str
    status: Literal["confirmed", "cancelled", "cancelled_by_provider"] = "confirmed"
    hold_id: str | None = None
    idempotency_key: str | None = None
    created_at: str
    history: list[dict[str, Any]] = Field(default_factory=list)


class MockState(BaseModel):
    counters: dict[str, int] = Field(default_factory=lambda: {"hold": 1000, "booking": 0})
    holds: dict[str, Hold] = Field(default_factory=dict)
    bookings: dict[str, Booking] = Field(default_factory=dict)
    # scope ("book"|"cancel"|"reschedule") -> idempotency key -> stored response
    idempotency: dict[str, dict[str, dict[str, Any]]] = Field(
        default_factory=lambda: {"book": {}, "cancel": {}, "reschedule": {}}
    )
    # provider_id -> slots taken by other customers (simulated competition)
    taken_by_others: dict[str, list[str]] = Field(default_factory=dict)
    book_attempts: dict[str, int] = Field(default_factory=dict)  # hold_id -> attempts
    cancel_attempts: dict[str, int] = Field(default_factory=dict)  # booking_id -> attempts
    events: list[dict[str, Any]] = Field(default_factory=list)  # provider-side events
    call_log: list[dict[str, Any]] = Field(default_factory=list)
    seeded_users: list[str] = Field(default_factory=list)


class _Store:
    """One-row JSON snapshot in SQLite. Small state, so whole-snapshot writes are fine."""

    def __init__(self, path: str | Path | None) -> None:
        self._conn = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False)
        self._conn.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT)")
        self._conn.commit()

    def load(self) -> MockState | None:
        row = self._conn.execute("SELECT body FROM state WHERE id = 1").fetchone()
        return MockState.model_validate_json(row[0]) if row else None

    def save(self, state: MockState) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO state (id, body) VALUES (1, ?)", (state.model_dump_json(),)
        )
        self._conn.commit()


class MockProviderService:
    def __init__(
        self,
        config: MockConfig | None = None,
        clock: Clock | None = None,
        db_path: str | Path | None = None,
        default_user_id: str = "user_1",
    ) -> None:
        self.config = config or MockConfig()
        self.clock = clock or RealClock()
        self.catalog: dict[str, Provider] = build_catalog()
        self.default_user_id = default_user_id
        self._lock = threading.RLock()
        self._caller = threading.local()  # who is calling, like an API's auth token
        self._store = _Store(db_path)
        self.state = self._store.load() or MockState()
        if default_user_id not in self.state.seeded_users:
            self._seed_history(default_user_id)
            self._save()

    # ------------------------------------------------------------------ caller identity
    # The tool contracts are fixed (book(hold_id, key) has no user), so, like a real provider
    # API, the caller's identity travels with the connection, not in the tool arguments.
    # ProviderClient opens each chat's calls with `acting_as(user)`. Calls without a caller
    # (tests, the demo's debug buttons) act as staff and see everything.

    @contextmanager
    def acting_as(self, user_id: str | None):
        previous = getattr(self._caller, "user_id", None)
        self._caller.user_id = user_id
        try:
            yield self
        finally:
            self._caller.user_id = previous

    def _acting(self) -> str | None:
        return getattr(self._caller, "user_id", None)

    def _visible(self, b: Booking) -> bool:
        caller = self._acting()
        return caller is None or b.user_id == caller

    # ------------------------------------------------------------------ helpers

    def _save(self) -> None:
        self._store.save(self.state)

    def _now(self) -> datetime:
        return self.clock.now()

    def _log(self, tool: str, args: dict[str, Any], outcome: str, **extra: Any) -> None:
        self.state.call_log.append(
            {
                "seq": len(self.state.call_log) + 1,
                "ts": self._now().isoformat(),
                "tool": tool,
                "args": args,
                "outcome": outcome,
                **extra,
            }
        )

    def _faults(self, tool: str) -> dict[str, Any] | None:
        """Apply configured fault injection. Returns a malformed payload if configured."""
        delay = self.config.slow_tools.get(tool)
        if delay:
            self.clock.sleep(delay)
        mode = self.config.broken_tools.get(tool)
        if mode == "exception":
            raise ToolInternalError(f"{tool}: upstream returned 500 Internal Server Error")
        if mode == "malformed":
            return {"unexpected": "<html><body>502 Bad Gateway</body></html>"}
        return None

    def _next_id(self, kind: str) -> str:
        self.state.counters[kind] += 1
        n = self.state.counters[kind]
        return f"H-{n}" if kind == "hold" else f"BK-{n:04d}"

    def _rng(self, *parts: Any) -> random.Random:
        return random.Random(":".join(str(p) for p in (self.config.seed, *parts)))

    def _generated_slots(self, p: Provider, day: date) -> list[str]:
        rng = self._rng("avail", p.id, day.isoformat())
        out = []
        for hour in range(p.open_hour, p.close_hour):
            if rng.random() < 0.6:
                out.append(f"{day.isoformat()}T{hour:02d}:00")
        return out

    def _slot_free(self, provider_id: str, slot: str, ignore_hold: str | None = None) -> bool:
        p = self.catalog[provider_id]
        dt = parse_slot(slot)
        if dt <= self._now():
            return False
        if slot not in self._generated_slots(p, dt.date()):
            return False
        if slot in self.state.taken_by_others.get(provider_id, []):
            return False
        for b in self.state.bookings.values():
            if b.provider_id == provider_id and b.slot == slot and b.status == "confirmed":
                return False
        now = self._now()
        for h in self.state.holds.values():
            if (
                h.provider_id == provider_id
                and h.slot == slot
                and h.status == "active"
                and h.hold_id != ignore_hold
                and datetime.fromisoformat(h.expires_at) > now
            ):
                return False
        return True

    def seed_history(self, user_id: str,
                     seeds: list[tuple[str, str, int, int, str]] | None = None) -> None:
        """Test/demo data: past and upcoming bookings for a user (not a tool)."""
        with self._lock:
            if user_id not in self.state.seeded_users:
                self._seed_history(user_id, seeds)
                self._save()

    def _seed_history(self, user_id: str,
                      seeds: list[tuple[str, str, int, int, str]] | None = None) -> None:
        now = self._now()

        def at(days: int, hour: int) -> str:
            d = (now + timedelta(days=days)).date()
            return f"{d.isoformat()}T{hour:02d}:00"

        # (provider, technician, days from now, hour, status)
        seeds = seeds or [
            ("ELE-01", "Ramesh", -75, 12, "confirmed"),
            ("PLB-03", "Kumar", -41, 9, "confirmed"),
            ("ELE-01", "Ramesh", -20, 11, "confirmed"),
            ("CLN-04", "Team Meena", -10, 10, "cancelled"),
            ("ACS-01", "Salim", 2, 15, "confirmed"),
        ]
        for pid, tech, days, hour, status in seeds:
            slot = at(days, hour)
            p = self.catalog[pid]
            bid = self._next_id("booking")
            self.state.bookings[bid] = Booking(
                booking_id=bid,
                user_id=user_id,
                provider_id=pid,
                provider_name=p.name,
                service=p.service,
                slot=slot,
                price=p.price,
                technician=tech,
                status=status,  # type: ignore[arg-type]
                created_at=(parse_slot(slot) - timedelta(days=3)).isoformat(),
            )
        self.state.seeded_users.append(user_id)

    def _visit_outcome(self, b: Booking) -> str:
        """Did the visit happen? Seeded history is always completed; a booking made through
        `book` is a no-show with probability no_show_prob, fixed per booking."""
        forced = self.config.forced_visit_outcomes.get(b.booking_id)
        if forced:
            return forced
        if b.hold_id is None:
            return "completed"
        h = hashlib.sha256(f"{self.config.seed}:{b.booking_id}".encode()).hexdigest()
        return "no_show" if int(h[:8], 16) / 0xFFFFFFFF < self.config.no_show_prob \
            else "completed"

    def _booking_view(self, b: Booking) -> dict[str, Any]:
        when = "past" if parse_slot(b.slot) < self._now() else "upcoming"
        status = self._visit_outcome(b) if (b.status == "confirmed" and when == "past") \
            else b.status
        return {
            "booking_id": b.booking_id,
            "provider_id": b.provider_id,
            "provider_name": b.provider_name,
            "service": b.service,
            "slot": b.slot,
            "price": b.price,
            "technician": b.technician,
            "status": status,
            "when": when,
        }

    # ------------------------------------------------------------------ tools

    def search_providers(
        self, service: str, area: str, date_range: dict[str, str]
    ) -> dict[str, Any]:
        with self._lock:
            args = {"service": service, "area": area, "date_range": date_range}
            bad = self._faults("search_providers")
            if bad is not None:
                self._log("search_providers", args, "malformed")
                return bad
            svc = normalize_service(service)
            ar = normalize_area(area)
            if svc is None:
                self._log("search_providers", args, "unknown_service")
                return {"status": "error", "error": "unknown_service", "service": service}
            if ar is None:
                self._log("search_providers", args, "unknown_area")
                return {"status": "error", "error": "unknown_area", "area": area}
            try:
                start = date.fromisoformat(date_range["start"])
                end = date.fromisoformat(date_range["end"])
            except (KeyError, ValueError, TypeError):
                self._log("search_providers", args, "bad_date_range")
                return {"status": "error", "error": "bad_date_range",
                        "hint": "date_range must be {'start': 'YYYY-MM-DD', 'end': 'YYYY-MM-DD'}"}
            if end < start:
                start, end = end, start
            end = min(end, start + timedelta(days=MAX_RANGE_DAYS - 1))

            results = []
            full = svc in self.config.no_availability or f"{svc}@{ar}" in \
                self.config.no_availability
            for p in self.catalog.values():
                if p.service != svc or ar not in p.areas:
                    continue
                slots: list[str] = []
                day = start
                while (not full and day <= end
                       and len(slots) < self.config.max_slots_per_provider):
                    for s in self._generated_slots(p, day):
                        if self._slot_free(p.id, s):
                            slots.append(s)
                    day += timedelta(days=1)
                results.append(
                    {
                        "provider_id": p.id,
                        "name": p.name,
                        "price": p.price,
                        "rating": p.rating,
                        "review_count": p.review_count,
                        "available_slots": slots[: self.config.max_slots_per_provider],
                        "attributes": {
                            "verified": p.verified,
                            "years_experience": p.years_experience,
                            "warranty_days": p.warranty_days,
                            "languages": p.languages,
                            "specialties": p.specialties,
                            "working_hours": f"{p.open_hour:02d}:00-{p.close_hour:02d}:00",
                        },
                    }
                )
            results = results[:8]
            self._log("search_providers", args, "ok", count=len(results))
            self._save()
            return {
                "status": "ok",
                "service": svc,
                "area": ar,
                "date_range": {"start": start.isoformat(), "end": end.isoformat()},
                "providers": results,
            }

    def get_provider(self, provider_id: str) -> dict[str, Any]:
        with self._lock:
            args = {"provider_id": provider_id}
            bad = self._faults("get_provider")
            if bad is not None:
                self._log("get_provider", args, "malformed")
                return bad
            p = self.catalog.get(provider_id)
            if p is None:
                self._log("get_provider", args, "not_found")
                return {"status": "error", "error": "provider_not_found"}
            self._log("get_provider", args, "ok")
            self._save()
            return {
                "status": "ok",
                "provider_id": p.id,
                "name": p.name,
                "service": p.service,
                "areas": p.areas,
                "price": p.price,
                "rating": p.rating,
                "review_count": p.review_count,
                "years_experience": p.years_experience,
                "verified": p.verified,
                "warranty_days": p.warranty_days,
                "languages": p.languages,
                "specialties": p.specialties,
                "technicians": p.technicians,
                "phone": p.phone,
                "about": p.about,
                "reviews": [r.model_dump() for r in p.reviews],
            }

    def hold_slot(self, provider_id: str, slot: str) -> dict[str, Any]:
        with self._lock:
            args = {"provider_id": provider_id, "slot": slot}
            bad = self._faults("hold_slot")
            if bad is not None:
                self._log("hold_slot", args, "malformed")
                return bad
            p = self.catalog.get(provider_id)
            if p is None:
                self._log("hold_slot", args, "provider_not_found")
                return {"status": "error", "error": "provider_not_found"}
            try:
                parse_slot(slot)
            except ValueError:
                self._log("hold_slot", args, "bad_slot")
                return {"status": "error", "error": "bad_slot_format", "hint": "YYYY-MM-DDTHH:MM"}
            if not self._slot_free(provider_id, slot):
                self._log("hold_slot", args, "slot_unavailable")
                return {"status": "error", "error": "slot_unavailable"}

            hold_id = self._next_id("hold")
            quote = self._quote(p, hold_id)
            now = self._now()
            hold = Hold(
                hold_id=hold_id,
                provider_id=provider_id,
                slot=slot,
                search_price=p.price,
                quoted_price=quote,
                created_at=now.isoformat(),
                expires_at=(now + timedelta(seconds=self.config.hold_ttl_seconds)).isoformat(),
                user_id=self._acting(),
            )
            self.state.holds[hold_id] = hold
            self._log("hold_slot", args, "ok", hold_id=hold_id, quoted_price=quote)
            self._save()
            return {
                "status": "ok",
                "hold_id": hold_id,
                "provider_id": provider_id,
                "slot": slot,
                "quoted_price": quote,
                "search_price": p.price,
                "expires_at": hold.expires_at,
            }

    def _quote(self, p: Provider, hold_id: str) -> int:
        if self.config.forced_hold_quotes:
            forced = self.config.forced_hold_quotes.pop(0)
            if forced is not None:
                return forced
        rng = self._rng("quote", hold_id)
        if rng.random() >= self.config.hold_price_drift_prob:
            return p.price
        lo, hi = self.config.hold_price_drift_range
        pct = rng.uniform(lo, hi) * (1 if rng.random() < 0.8 else -1)
        return int(round(p.price * (1 + pct) / 10.0) * 10)

    def _book_outcome(self, hold_id: str, attempt: int) -> BookOutcome:
        if self.config.forced_book_outcomes:
            return self.config.forced_book_outcomes.pop(0)
        r = self._rng("book", hold_id, attempt).random()
        c = self.config
        if r < c.book_success_prob:
            return "success"
        if r < c.book_success_prob + c.book_slot_taken_prob:
            return "slot_taken"
        rest = (r - c.book_success_prob - c.book_slot_taken_prob) / max(
            1e-9, 1 - c.book_success_prob - c.book_slot_taken_prob
        )
        return "timeout_booked" if rest < c.book_timeout_booked_fraction else "timeout_not_booked"

    def book(self, hold_id: str, idempotency_key: str) -> dict[str, Any]:
        with self._lock:
            args = {"hold_id": hold_id, "idempotency_key": idempotency_key}
            bad = self._faults("book")
            if bad is not None:
                self._log("book", args, "malformed")
                return bad
            stored = self.state.idempotency["book"].get(idempotency_key)
            if stored is not None:
                self._log("book", args, "idempotent_replay", committed=False)
                self._save()
                return dict(stored)

            hold = self.state.holds.get(hold_id)
            if hold is not None and self._acting() and hold.user_id not in (None,
                                                                           self._acting()):
                hold = None  # someone else's hold: as if it didn't exist
            if hold is None:
                self._log("book", args, "hold_not_found")
                self._save()
                return {"status": "error", "error": "hold_not_found"}
            if hold.status == "used":
                self._log("book", args, "hold_already_used")
                self._save()
                return {"status": "slot_taken", "reason": "hold_already_used"}
            if hold.status == "lost":
                self._log("book", args, "slot_taken")
                self._save()
                return {"status": "slot_taken"}
            if self._now() > datetime.fromisoformat(hold.expires_at):
                self._log("book", args, "hold_expired")
                self._save()
                return {"status": "hold_expired", "hold_id": hold_id,
                        "expired_at": hold.expires_at}

            attempt = self.state.book_attempts.get(hold_id, 0) + 1
            self.state.book_attempts[hold_id] = attempt
            outcome = self._book_outcome(hold_id, attempt)

            if outcome == "slot_taken":
                hold.status = "lost"
                self.state.taken_by_others.setdefault(hold.provider_id, []).append(hold.slot)
                result = {"status": "slot_taken", "hold_id": hold_id}
                self.state.idempotency["book"][idempotency_key] = result
                self._log("book", args, "slot_taken", committed=False)
                self._save()
                return dict(result)

            if outcome == "timeout_not_booked":
                self._log("book", args, "timeout_not_booked", committed=False)
                self._save()
                raise ToolTimeout("book: upstream timed out after 30s")

            # success or timeout_booked: the booking is committed either way.
            p = self.catalog[hold.provider_id]
            bid = self._next_id("booking")
            booking = Booking(
                booking_id=bid,
                user_id=hold.user_id or self._acting() or self.default_user_id,
                provider_id=p.id,
                provider_name=p.name,
                service=p.service,
                slot=hold.slot,
                price=hold.quoted_price,
                technician=p.technicians[0],
                hold_id=hold_id,
                idempotency_key=idempotency_key,
                created_at=self._now().isoformat(),
            )
            self.state.bookings[bid] = booking
            hold.status = "used"
            result = {
                "status": "success",
                "booking_id": bid,
                "provider_id": p.id,
                "slot": hold.slot,
                "price": hold.quoted_price,
            }
            self.state.idempotency["book"][idempotency_key] = result
            if outcome == "timeout_booked":
                self._log("book", args, "timeout_booked", committed=True, booking_id=bid)
                self._save()
                raise ToolTimeout("book: upstream timed out after 30s")
            self._log("book", args, "success", committed=True, booking_id=bid)
            self._save()
            return dict(result)

    def get_booking_status(
        self, hold_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            args = {"hold_id": hold_id, "idempotency_key": idempotency_key}
            bad = self._faults("get_booking_status")
            if bad is not None:
                self._log("get_booking_status", args, "malformed")
                return bad
            if not hold_id and not idempotency_key:
                return {"status": "error", "error": "hold_id_or_idempotency_key_required"}

            booking: Booking | None = None
            if idempotency_key:
                stored = self.state.idempotency["book"].get(idempotency_key)
                if stored and stored.get("status") == "slot_taken":
                    self._log("get_booking_status", args, "failed_slot_taken")
                    self._save()
                    return {"status": "failed", "reason": "slot_taken"}
                if stored and stored.get("booking_id"):
                    booking = self.state.bookings[stored["booking_id"]]
            if booking is None and hold_id:
                booking = next(
                    (b for b in self.state.bookings.values() if b.hold_id == hold_id), None
                )
            if booking is not None and not self._visible(booking):
                booking = None
            if booking is not None:
                self._log("get_booking_status", args, "booked")
                self._save()
                # The booking view has its own "status" (confirmed/cancelled/...); keep it
                # under a separate key so it cannot overwrite the lookup result.
                view = self._booking_view(booking)
                view["booking_status"] = view.pop("status")
                return {**view, "status": "booked"}

            if hold_id and hold_id in self.state.holds:
                h = self.state.holds[hold_id]
                hold_state = h.status
                if h.status == "active" and self._now() > datetime.fromisoformat(h.expires_at):
                    hold_state = "expired"
                self._log("get_booking_status", args, "not_booked")
                self._save()
                return {"status": "not_booked", "hold_status": hold_state}
            self._log("get_booking_status", args, "not_found")
            self._save()
            return {"status": "not_found"}

    def cancel(self, booking_id: str, idempotency_key: str) -> dict[str, Any]:
        with self._lock:
            args = {"booking_id": booking_id, "idempotency_key": idempotency_key}
            bad = self._faults("cancel")
            if bad is not None:
                self._log("cancel", args, "malformed")
                return bad
            stored = self.state.idempotency["cancel"].get(idempotency_key)
            if stored is not None:
                self._log("cancel", args, "idempotent_replay", committed=False)
                self._save()
                return dict(stored)
            b = self.state.bookings.get(booking_id)
            if b is not None and not self._visible(b):
                b = None  # another user's booking: don't reveal it exists
            if b is None:
                self._log("cancel", args, "not_found")
                self._save()
                return {"status": "error", "error": "booking_not_found"}
            attempts = self.state.cancel_attempts.get(booking_id, 0) + 1
            self.state.cancel_attempts[booking_id] = attempts
            if self.config.cancel_always_fail or (self.config.cancel_fail_first and attempts == 1):
                self._log("cancel", args, "503", committed=False)
                self._save()
                raise ServiceUnavailable("cancel: 503 Service Unavailable, retry later")
            if b.status != "confirmed" or parse_slot(b.slot) < self._now():
                self._log("cancel", args, "not_cancellable")
                self._save()
                return {"status": "error", "error": "not_cancellable",
                        "booking_status": self._booking_view(b)["status"]}
            b.status = "cancelled"
            b.history.append({"ts": self._now().isoformat(), "event": "cancelled_by_user"})
            result = {"status": "cancelled", "booking_id": booking_id}
            self.state.idempotency["cancel"][idempotency_key] = result
            self._log("cancel", args, "cancelled", committed=True)
            self._save()
            return dict(result)

    def reschedule(self, booking_id: str, slot: str, idempotency_key: str) -> dict[str, Any]:
        with self._lock:
            args = {"booking_id": booking_id, "slot": slot, "idempotency_key": idempotency_key}
            bad = self._faults("reschedule")
            if bad is not None:
                self._log("reschedule", args, "malformed")
                return bad
            stored = self.state.idempotency["reschedule"].get(idempotency_key)
            if stored is not None:
                self._log("reschedule", args, "idempotent_replay", committed=False)
                self._save()
                return dict(stored)
            b = self.state.bookings.get(booking_id)
            if b is not None and not self._visible(b):
                b = None
            if b is None:
                self._log("reschedule", args, "not_found")
                self._save()
                return {"status": "error", "error": "booking_not_found"}
            if b.status != "confirmed" or parse_slot(b.slot) < self._now():
                self._log("reschedule", args, "not_reschedulable")
                self._save()
                return {"status": "error", "error": "not_reschedulable",
                        "booking_status": self._booking_view(b)["status"]}
            try:
                parse_slot(slot)
            except ValueError:
                return {"status": "error", "error": "bad_slot_format", "hint": "YYYY-MM-DDTHH:MM"}
            if slot in self.config.conflict_slots or not self._slot_free(b.provider_id, slot):
                result = {"status": "slot_conflict", "booking_id": booking_id,
                          "requested_slot": slot}
                self.state.idempotency["reschedule"][idempotency_key] = result
                self._log("reschedule", args, "slot_conflict", committed=False)
                self._save()
                return dict(result)
            old = b.slot
            b.slot = slot
            b.history.append({"ts": self._now().isoformat(), "event": "rescheduled",
                              "from": old, "to": slot})
            result = {"status": "rescheduled", "booking_id": booking_id, "old_slot": old,
                      "new_slot": slot}
            self.state.idempotency["reschedule"][idempotency_key] = result
            self._log("reschedule", args, "rescheduled", committed=True)
            self._save()
            return dict(result)

    def list_bookings(self, user_id: str) -> dict[str, Any]:
        with self._lock:
            args = {"user_id": user_id}
            bad = self._faults("list_bookings")
            if bad is not None:
                self._log("list_bookings", args, "malformed")
                return bad
            caller = self._acting()
            if caller is not None and user_id != caller:
                self._log("list_bookings", args, "forbidden")
                self._save()
                return {"status": "error", "error": "forbidden"}
            rows = [
                self._booking_view(b)
                for b in sorted(self.state.bookings.values(), key=lambda b: b.slot)
                if b.user_id == user_id
            ]
            self._log("list_bookings", args, "ok", count=len(rows))
            self._save()
            return {"status": "ok", "user_id": user_id, "bookings": rows}

    # ------------------------------------------------------------------ simulation admin
    # Not tools: used by tests, the demo debug panel and the proactive-event simulator.

    def simulate_provider_cancellation(self, booking_id: str, reason: str = "") -> dict[str, Any]:
        with self._lock:
            b = self.state.bookings[booking_id]
            b.status = "cancelled_by_provider"
            b.history.append({"ts": self._now().isoformat(), "event": "cancelled_by_provider",
                              "reason": reason})
            event = {
                "event_id": f"EV-{len(self.state.events) + 1}",
                "type": "booking_cancelled_by_provider",
                "booking_id": booking_id,
                "user_id": b.user_id,
                "reason": reason or "Technician unavailable",
                "ts": self._now().isoformat(),
                "delivered": False,
            }
            self.state.events.append(event)
            self._save()
            return dict(event)

    def poll_events(self, user_id: str) -> list[dict[str, Any]]:
        """Webhook-style feed of provider events not yet delivered."""
        with self._lock:
            out = []
            for ev in self.state.events:
                if ev["user_id"] == user_id and not ev["delivered"]:
                    ev["delivered"] = True
                    out.append(dict(ev))
            if out:
                self._save()
            return out

    def committed_bookings(self) -> list[Booking]:
        """All bookings created through `book` (excludes seeded history)."""
        return [b for b in self.state.bookings.values() if b.hold_id is not None]

    def export_state(self) -> str:
        return json.dumps(self.state.model_dump(), indent=2, default=str)
