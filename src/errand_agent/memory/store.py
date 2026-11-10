"""Persistent user memory with explicit vs inferred facts.

- explicit: the user said it (evidence quote from a user message). Confirmed by definition.
- inferred: derived from behaviour (e.g. booking history). Stored unconfirmed and NEVER
  sent to the model until the user confirms it.
- status: active | superseded (replaced by a correction) | forgotten (user asked).

`select_for_context` is the single gate deciding which memories may reach the model; it
returns both the included memories and every excluded one with a reason, which the
context builder logs on every model call.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel

from errand_agent.core.clock import Clock
from errand_agent.core.store import AgentStore
from errand_agent.llm.base import estimate_tokens

Kind = Literal["explicit", "inferred"]
Status = Literal["active", "superseded", "forgotten"]

# Keys that matter for arranging a home-service visit. Anything else (favourite cuisine,
# cricket team, ...) is stored if the user said it but is never sent to the model.
RELEVANT_KEYS = {
    "area",
    "earliest_time",
    "latest_time",
    "preferred_time_of_day",
    "budget_max",
    "preferred_provider",
    "avoid_provider",
    "language",
    "access_notes",
}

KEY_LABELS = {
    "area": "Area",
    "earliest_time": "Earliest acceptable time",
    "latest_time": "Latest acceptable time",
    "preferred_time_of_day": "Preferred time of day",
    "budget_max": "Budget limit",
    "preferred_provider": "Preferred provider/technician",
    "avoid_provider": "Provider to avoid",
    "language": "Language",
    "access_notes": "Access notes",
}


class Memory(BaseModel):
    memory_id: str
    user_id: str
    key: str
    value: str
    kind: Kind
    status: Status
    confirmed: bool
    evidence: str | None = None
    scope: str | None = None  # service the fact applies to; None = global
    created_at: str
    updated_at: str

    def line(self) -> str:
        scope = f" [{self.scope}]" if self.scope else ""
        tag = "explicit" if self.kind == "explicit" else "inferred, confirmed by user"
        return f"{self.memory_id}: {self.key}{scope} = {self.value} ({tag})"


class MemoryStore:
    def __init__(self, store: AgentStore, clock: Clock) -> None:
        self.db = store
        self.clock = clock

    # ------------------------------------------------------------------ queries
    def _rows(self, sql: str, args: tuple) -> list[Memory]:
        with self.db.lock:
            rows = self.db.conn.execute(sql, args).fetchall()
        return [Memory(**{**dict(r), "confirmed": bool(r["confirmed"])}) for r in rows]

    def get(self, memory_id: str) -> Memory | None:
        rows = self._rows("SELECT * FROM memories WHERE memory_id=?", (memory_id,))
        return rows[0] if rows else None

    def all(self, user_id: str, statuses: tuple[str, ...] | None = None) -> list[Memory]:
        rows = self._rows("SELECT * FROM memories WHERE user_id=? ORDER BY memory_id", (user_id,))
        return [m for m in rows if statuses is None or m.status in statuses]

    def active(self, user_id: str, key: str, scope: str | None) -> list[Memory]:
        return [m for m in self.all(user_id, ("active",)) if m.key == key and m.scope == scope]

    # ------------------------------------------------------------------ writes
    def _insert(self, m: Memory) -> None:
        with self.db.lock:
            self.db.conn.execute(
                "INSERT OR REPLACE INTO memories (memory_id, user_id, key, value, kind, status, "
                "confirmed, evidence, scope, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (m.memory_id, m.user_id, m.key, m.value, m.kind, m.status, int(m.confirmed),
                 m.evidence, m.scope, m.created_at, m.updated_at),
            )

    def _set(self, m: Memory, **changes: Any) -> Memory:
        new = m.model_copy(update={**changes, "updated_at": self.clock.now().isoformat()})
        self._insert(new)
        return new

    def add(self, user_id: str, key: str, value: str, kind: Kind, evidence: str | None = None,
            scope: str | None = None, status: Status = "active",
            created_at: str | None = None) -> Memory:
        """Supersede the old value and insert the new one in ONE transaction: a crash can't
        leave the user with no active value for the key."""
        with self.db.tx():
            return self._add(user_id, key, value, kind, evidence, scope, status, created_at)

    def _add(self, user_id: str, key: str, value: str, kind: Kind, evidence: str | None,
             scope: str | None, status: Status, created_at: str | None) -> Memory:
        existing = self.active(user_id, key, scope)
        if status == "active":
            if kind == "inferred":
                for m in existing:
                    if m.kind == "explicit" or m.value == value:
                        return m  # never let an inference override what the user said
            for m in existing:
                self._set(m, status="superseded")
        now = created_at or self.clock.now().isoformat()
        m = Memory(
            memory_id=self.db.next_id("memory", "M-"), user_id=user_id, key=key,
            value=str(value), kind=kind, status=status, confirmed=(kind == "explicit"),
            evidence=evidence, scope=scope, created_at=now, updated_at=now,
        )
        self._insert(m)
        return m

    def forget(self, user_id: str, memory_id: str | None = None, key: str | None = None,
               scope: str | None = None) -> list[Memory]:
        targets = [m for m in self.all(user_id, ("active",))
                   if (memory_id and m.memory_id == memory_id)
                   or (key and m.key == key and (scope is None or m.scope == scope))]
        with self.db.tx():  # all of them or none
            return [self._set(m, status="forgotten") for m in targets]

    def confirm(self, user_id: str, memory_id: str) -> Memory | None:
        m = self.get(memory_id)
        if m is None or m.user_id != user_id or m.status != "active":
            return None
        return self._set(m, confirmed=True)

    # ------------------------------------------------------------------ context gate
    def select_for_context(
        self, user_id: str, service: str | None, budget_tokens: int
    ) -> tuple[list[Memory], list[dict[str, str]]]:
        included: list[Memory] = []
        excluded: list[dict[str, str]] = []
        candidates: list[Memory] = []
        for m in self.all(user_id):
            reason = None
            if m.status == "forgotten":
                reason = "forgotten"
            elif m.status == "superseded":
                reason = "superseded"
            elif m.kind == "inferred" and not m.confirmed:
                reason = "unconfirmed_inferred"
            elif m.key not in RELEVANT_KEYS:
                reason = "irrelevant_key"
            elif m.scope is not None and m.scope != service:
                reason = "irrelevant_scope"
            if reason:
                excluded.append({"memory_id": m.memory_id, "reason": reason})
            else:
                candidates.append(m)
        # Service-specific facts first, then most recently updated.
        candidates.sort(key=lambda m: m.updated_at, reverse=True)
        candidates.sort(key=lambda m: m.scope is None)  # stable: scoped facts first
        used = 0
        for m in candidates:
            cost = estimate_tokens(m.line()) + 1
            if used + cost > budget_tokens:
                excluded.append({"memory_id": m.memory_id, "reason": "over_budget"})
                continue
            used += cost
            included.append(m)
        return included, excluded

    # ------------------------------------------------------------------ inference
    def infer_from_bookings(self, user_id: str, bookings: list[dict[str, Any]]) -> list[Memory]:
        """Derive unconfirmed preferences from booking history."""
        out: list[Memory] = []
        done = [b for b in bookings if b.get("status") in ("completed", "confirmed")]
        by_service: dict[str, Counter] = {}
        for b in done:
            by_service.setdefault(b["service"], Counter())[b["provider_id"]] += 1
        for service, counts in by_service.items():
            pid, n = counts.most_common(1)[0]
            if n >= 2:
                out.append(self.add(user_id, "preferred_provider", pid, "inferred",
                                    evidence=f"booked {pid} {n} times", scope=service))
        hours = [int(b["slot"][11:13]) for b in done]
        if len(hours) >= 3 and all(h < 12 for h in hours):
            out.append(self.add(user_id, "preferred_time_of_day", "morning", "inferred",
                                evidence=f"{len(hours)} past bookings all before noon"))
        return out
