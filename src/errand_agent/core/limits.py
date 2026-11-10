"""Hard per-run limits: model calls, tool calls, wall clock."""

from __future__ import annotations

from dataclasses import dataclass, field

from errand_agent.core.clock import Clock


@dataclass(frozen=True)
class Limits:
    max_model_calls: int = 8
    max_tool_calls: int = 12
    max_seconds: float = 45.0
    # Bounded retries inside the deterministic executors (not counted against the
    # model's tool budget: they are already bounded and must not be cut mid-saga).
    max_book_attempts: int = 3
    max_status_checks: int = 3
    max_cancel_attempts: int = 3
    max_reschedule_attempts: int = 3
    retry_backoff_seconds: tuple[float, ...] = (0.5, 1.0, 2.0)


class LimitExceeded(Exception):
    def __init__(self, kind: str, used: float, limit: float) -> None:
        super().__init__(f"{kind} limit exceeded: {used} > {limit}")
        self.kind = kind
        self.used = used
        self.limit = limit


@dataclass
class RunBudget:
    limits: Limits
    clock: Clock
    model_calls: int = 0
    tool_calls: int = 0
    started: float = field(default=0.0)

    def __post_init__(self) -> None:
        self.started = self.clock.monotonic()

    def elapsed(self) -> float:
        return self.clock.monotonic() - self.started

    def check_time(self) -> None:
        if self.elapsed() > self.limits.max_seconds:
            raise LimitExceeded("wall_clock_seconds", round(self.elapsed(), 2),
                                self.limits.max_seconds)

    def charge_model_call(self) -> None:
        self.check_time()
        if self.model_calls >= self.limits.max_model_calls:
            raise LimitExceeded("model_calls", self.model_calls + 1, self.limits.max_model_calls)
        self.model_calls += 1

    def charge_tool_call(self) -> None:
        self.check_time()
        if self.tool_calls >= self.limits.max_tool_calls:
            raise LimitExceeded("tool_calls", self.tool_calls + 1, self.limits.max_tool_calls)
        self.tool_calls += 1

    def snapshot(self) -> dict:
        return {
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "elapsed_s": round(self.elapsed(), 3),
            "limits": {
                "model_calls": self.limits.max_model_calls,
                "tool_calls": self.limits.max_tool_calls,
                "seconds": self.limits.max_seconds,
            },
        }
