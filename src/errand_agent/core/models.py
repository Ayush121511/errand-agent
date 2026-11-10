"""Domain models for the agent side: session, task, workflow phase, approvals, attempts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Phase(StrEnum):
    IDLE = "IDLE"
    GATHERING = "GATHERING"
    SEARCHING = "SEARCHING"
    PRESENTING = "PRESENTING"
    HOLDING = "HOLDING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    DONE = "DONE"
    FAILED = "FAILED"


_P = Phase
_PLANNING = {_P.GATHERING, _P.SEARCHING, _P.PRESENTING, _P.HOLDING, _P.AWAITING_APPROVAL}

ALLOWED_TRANSITIONS: dict[Phase, set[Phase]] = {
    _P.IDLE: _PLANNING,
    _P.GATHERING: _PLANNING | {_P.IDLE},
    _P.SEARCHING: {_P.PRESENTING, _P.GATHERING, _P.FAILED, _P.IDLE},
    _P.PRESENTING: _PLANNING | {_P.IDLE},
    _P.HOLDING: {_P.AWAITING_APPROVAL, _P.PRESENTING, _P.SEARCHING, _P.GATHERING, _P.FAILED},
    _P.AWAITING_APPROVAL: _PLANNING | {_P.EXECUTING, _P.IDLE},
    _P.EXECUTING: {_P.VERIFYING, _P.DONE, _P.FAILED, _P.AWAITING_APPROVAL},
    _P.VERIFYING: {_P.EXECUTING, _P.DONE, _P.FAILED},
    _P.DONE: _PLANNING | {_P.IDLE},
    _P.FAILED: _PLANNING | {_P.IDLE},
}


class IllegalTransition(Exception):
    pass


Intent = Literal["book", "cancel", "reschedule", "info"]

TASK_FIELDS = [
    "service",
    "issue",
    "area",
    "date_start",
    "date_end",
    "time_window_start",
    "time_window_end",
    "earliest_time",
    "budget_max",
    "provider_pref",
    "target_booking_id",
    "new_date",
]

REQUIRED_FIELDS: dict[str, list[str]] = {
    "book": ["service", "area", "date_start", "date_end"],
    "reschedule": ["target_booking_id"],
    "cancel": ["target_booking_id"],
    "info": [],
}


class SlotValue(BaseModel):
    value: Any
    source: str  # "user:<message_id>" | "memory:<memory_id>" | "history:<booking_id>"
    evidence: str | None = None


class Candidate(BaseModel):
    provider_id: str
    name: str
    price: int
    rating: float
    fits: bool
    violations: list[str] = Field(default_factory=list)
    fitting_slots: list[str] = Field(default_factory=list)
    all_slots: list[str] = Field(default_factory=list)
    score: float = 0.0
    attributes: dict[str, Any] = Field(default_factory=dict)


class HoldInfo(BaseModel):
    hold_id: str
    provider_id: str
    provider_name: str
    service: str
    slot: str
    quoted_price: int
    search_price: int
    expires_at: str


class Task(BaseModel):
    task_id: str
    intent: Intent | None = None
    fields: dict[str, SlotValue] = Field(default_factory=dict)
    asked: list[str] = Field(default_factory=list)  # fields we already asked the user for
    candidates: list[Candidate] = Field(default_factory=list)
    hold: HoldInfo | None = None
    pending_approval_id: str | None = None
    outcome: dict[str, Any] | None = None
    options: dict[str, list[str]] = Field(default_factory=dict)  # booking_id -> reschedule slots

    def value(self, name: str) -> Any:
        sv = self.fields.get(name)
        return sv.value if sv else None

    def missing(self) -> list[str]:
        if self.intent is None:
            return ["intent"]
        return [f for f in REQUIRED_FIELDS[self.intent] if self.value(f) in (None, "")]


class User(BaseModel):
    """Who the user is. Preferences (area to search, budgets, times) live in memory, where
    they can be corrected and forgotten; the profile holds identity and the visit address."""
    user_id: str
    name: str
    phone: str | None = None
    home_area: str | None = None       # area at sign-up; the search area is a memory
    address: str | None = None         # flat / street, for the technician
    landmark: str | None = None
    created_at: str


class Session(BaseModel):
    session_id: str
    user_id: str
    phase: Phase = Phase.IDLE
    task: Task | None = None
    task_counter: int = 0
    # Other booking errands the user asked for in this chat, parked while `task` is worked on.
    errands: list[Task] = Field(default_factory=list)
    known_bookings: dict[str, str] = Field(default_factory=dict)  # booking_id -> status seen


ApprovalAction = Literal["book", "cancel", "reschedule"]
ApprovalStatus = Literal["pending", "approved", "consumed", "rejected", "invalidated", "expired"]


class Approval(BaseModel):
    approval_id: str
    session_id: str
    user_id: str
    action: ApprovalAction
    details: dict[str, Any]
    fingerprint: str
    status: ApprovalStatus = "pending"
    created_at: str
    expires_at: str
    approved_at: str | None = None
    approved_via: str | None = None
    approved_message_id: int | None = None
    status_reason: str | None = None
    card: str = ""


AttemptStatus = Literal["pending", "confirmed", "failed", "unknown"]


class Attempt(BaseModel):
    attempt_id: str
    approval_id: str
    session_id: str
    action: ApprovalAction
    idempotency_key: str
    hold_id: str | None = None
    booking_id: str | None = None
    status: AttemptStatus = "pending"
    result: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
