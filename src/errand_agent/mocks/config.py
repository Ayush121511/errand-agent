"""Knobs for the mock provider environment.

Default behaviour follows the assignment's stated distributions, seeded so runs are
reproducible. Scenario tests override individual knobs (or queue forced outcomes) to
hit an exact failure path.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

BookOutcome = Literal["success", "slot_taken", "timeout_booked", "timeout_not_booked"]


class MockConfig(BaseModel):
    seed: int = 42
    hold_ttl_seconds: int = 600

    # hold_slot: quoted price may differ from the search price.
    hold_price_drift_prob: float = 0.25
    hold_price_drift_range: tuple[float, float] = (0.05, 0.25)
    # FIFO of forced quotes; None in the queue means "use the normal rule".
    forced_hold_quotes: list[int | None] = Field(default_factory=list)

    # book: 70% success, 15% slot_taken, 15% timeout (half of which actually booked).
    book_success_prob: float = 0.70
    book_slot_taken_prob: float = 0.15
    book_timeout_booked_fraction: float = 0.5
    forced_book_outcomes: list[BookOutcome] = Field(default_factory=list)

    # cancel: the first call per booking fails with a transient 503.
    cancel_fail_first: bool = True
    cancel_always_fail: bool = False

    # reschedule: slot_conflict on these slot strings ("YYYY-MM-DDTHH:MM").
    conflict_slots: list[str] = Field(default_factory=list)

    # Fault injection for limit / robustness scenarios.
    slow_tools: dict[str, float] = Field(default_factory=dict)  # tool name -> seconds
    broken_tools: dict[str, Literal["exception", "malformed"]] = Field(default_factory=dict)

    # Fully booked: "service" or "service@Area" entries whose providers have no free slots
    # (the service is offered, but nobody is available right now).
    no_availability: list[str] = Field(default_factory=list)

    # How many days of slots to generate per provider when searching.
    max_slots_per_provider: int = 10
