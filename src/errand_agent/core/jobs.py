"""A durable job scheduler on the agent database.

Jobs survive restarts (they are rows, not timers). A worker claims a due job with a
compare-and-set UPDATE and a lease, so two workers can never run the same job, and a job
whose worker died is picked up again once its lease runs out. Failures retry with
exponential backoff; after max_attempts the job is parked as 'dead' for a human. A
dedupe_key makes scheduling idempotent ("one reminder per booking").
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from errand_agent.core.clock import Clock
from errand_agent.core.store import AgentStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                  -- visit_reminder | reconcile_payment | settle_payouts
    run_at TEXT NOT NULL,
    status TEXT NOT NULL,                -- queued | running | done | dead
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    lease_until TEXT,                    -- a running job past this is reclaimable
    worker TEXT,
    dedupe_key TEXT UNIQUE,
    payload TEXT NOT NULL,
    result TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
LEASE = timedelta(minutes=2)
BACKOFF_SECONDS = 30

Handler = Callable[[dict[str, Any]], Any]


class JobQueue:
    def __init__(self, store: AgentStore, clock: Clock) -> None:
        self.store, self.clock = store, clock
        self.handlers: dict[str, Handler] = {}
        with store.lock:
            store.conn.executescript(SCHEMA)

    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    def schedule(self, kind: str, run_at: datetime, payload: dict[str, Any] | None = None,
                 dedupe_key: str | None = None, max_attempts: int = 5) -> int | None:
        """Queue a job. Returns its id, or None if one with this dedupe_key already exists."""
        now = self.clock.now().isoformat()
        with self.store.lock:
            cur = self.store.conn.execute(
                "INSERT OR IGNORE INTO jobs (kind, run_at, status, max_attempts, dedupe_key, "
                "payload, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (kind, run_at.isoformat(), "queued", max_attempts, dedupe_key,
                 json.dumps(payload or {}), now, now))
            return cur.lastrowid if cur.rowcount else None

    def jobs(self, kind: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM jobs WHERE 1=1", []
        if kind:
            q, args = q + " AND kind=?", [*args, kind]
        if status:
            q, args = q + " AND status=?", [*args, status]
        with self.store.lock:
            rows = self.store.conn.execute(q + " ORDER BY job_id", args).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def _claim(self, job_id: int, worker: str, now: datetime) -> bool:
        """Compare-and-set: only one worker's UPDATE can match."""
        with self.store.lock:
            cur = self.store.conn.execute(
                "UPDATE jobs SET status='running', worker=?, lease_until=?, "
                "attempts=attempts+1, updated_at=? WHERE job_id=? AND run_at<=? AND "
                "(status='queued' OR (status='running' AND lease_until<?))",
                (worker, (now + LEASE).isoformat(), now.isoformat(), job_id, now.isoformat(),
                 now.isoformat()))
            return cur.rowcount == 1

    def _finish(self, job_id: int, **fields: Any) -> None:
        fields["updated_at"] = self.clock.now().isoformat()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.store.lock:
            self.store.conn.execute(f"UPDATE jobs SET {cols} WHERE job_id=?",
                                    (*fields.values(), job_id))

    def run_due(self, worker: str = "worker-1", limit: int = 20) -> list[dict[str, Any]]:
        """Run every due job once. Returns [{job_id, kind, status, result|error}]."""
        now = self.clock.now()
        with self.store.lock:
            due = self.store.conn.execute(
                "SELECT job_id FROM jobs WHERE run_at<=? AND (status='queued' OR "
                "(status='running' AND lease_until<?)) ORDER BY run_at, job_id LIMIT ?",
                (now.isoformat(), now.isoformat(), limit)).fetchall()
        ran = []
        for (job_id,) in due:
            if not self._claim(job_id, worker, now):
                continue  # another worker got it
            job = next(j for j in self.jobs() if j["job_id"] == job_id)
            handler = self.handlers.get(job["kind"])
            try:
                if handler is None:
                    raise LookupError(f"no handler for {job['kind']}")
                result = handler(job["payload"])
            except Exception as e:  # noqa: BLE001 - a failing job must not stop the others
                err = f"{type(e).__name__}: {e}"
                if job["attempts"] >= job["max_attempts"]:
                    self._finish(job_id, status="dead", last_error=err, lease_until=None)
                    ran.append({"job_id": job_id, "kind": job["kind"], "status": "dead",
                                "error": err})
                else:
                    retry = now + timedelta(seconds=BACKOFF_SECONDS * 2 ** (job["attempts"] - 1))
                    self._finish(job_id, status="queued", run_at=retry.isoformat(),
                                 last_error=err, lease_until=None)
                    ran.append({"job_id": job_id, "kind": job["kind"], "status": "retry",
                                "error": err})
                continue
            self._finish(job_id, status="done", result=json.dumps(result, default=str),
                         lease_until=None)
            ran.append({"job_id": job_id, "kind": job["kind"], "status": "done",
                        "result": result})
        return ran
