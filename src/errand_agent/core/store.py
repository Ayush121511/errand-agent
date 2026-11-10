"""SQLite persistence for the agent (separate file from the mock provider service)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from errand_agent.core.models import Approval, Attempt, Session, User

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    body TEXT NOT NULL               -- full profile (User) as JSON
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    body TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,              -- user | assistant | event
    content TEXT NOT NULL,
    ts TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    kind TEXT NOT NULL,              -- explicit | inferred
    status TEXT NOT NULL,            -- active | superseded | forgotten
    confirmed INTEGER NOT NULL,
    evidence TEXT,
    scope TEXT,                      -- service the fact applies to, or NULL for global
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_events (
    event_id TEXT PRIMARY KEY,
    handled_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS security_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    message_id INTEGER,
    ts TEXT NOT NULL,
    categories TEXT NOT NULL,        -- JSON list, e.g. ["secrets"]
    excerpt TEXT NOT NULL,           -- first 200 chars of the message
    cleared INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


class AgentStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = str(path) if path else ":memory:"
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    # ---------------------------------------------------------------- counters / ids
    def next_id(self, name: str, prefix: str) -> str:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
            n = (row["value"] if row else 0) + 1
            self.conn.execute(
                "INSERT OR REPLACE INTO counters (name, value) VALUES (?, ?)", (name, n)
            )
            self.conn.execute("COMMIT")
            return f"{prefix}{n:04d}"

    # ---------------------------------------------------------------- sessions
    def save_session(self, s: Session, ts: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO sessions (session_id, user_id, body, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (s.session_id, s.user_id, s.model_dump_json(), ts),
            )

    def load_session(self, session_id: str) -> Session | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT body FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        return Session.model_validate_json(row["body"]) if row else None

    def sessions_for_user(self, user_id: str) -> list[Session]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT body FROM sessions WHERE user_id=? ORDER BY updated_at", (user_id,)
            ).fetchall()
        return [Session.model_validate_json(r["body"]) for r in rows]

    # ---------------------------------------------------------------- messages
    def add_message(
        self, session_id: str, user_id: str, role: str, content: str, ts: str,
        meta: dict[str, Any] | None = None,
    ) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO messages (session_id, user_id, role, content, ts, meta) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, user_id, role, content, ts, json.dumps(meta or {})),
            )
            return int(cur.lastrowid)

    def messages(self, user_id: str, session_id: str | None = None,
                 limit: int | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM messages WHERE user_id=?"
        args: list[Any] = [user_id]
        if session_id:
            q += " AND session_id=?"
            args.append(session_id)
        q += " ORDER BY id"
        with self.lock:
            rows = [dict(r) for r in self.conn.execute(q, args).fetchall()]
        for r in rows:
            r["meta"] = json.loads(r["meta"])
        return rows[-limit:] if limit else rows

    def get_message(self, message_id: int) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- approvals
    def save_approval(self, a: Approval) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO approvals (approval_id, session_id, status, body) "
                "VALUES (?, ?, ?, ?)",
                (a.approval_id, a.session_id, a.status, a.model_dump_json()),
            )

    def get_approval(self, approval_id: str) -> Approval | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT body FROM approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()
        return Approval.model_validate_json(row["body"]) if row else None

    def approvals(self, session_id: str | None = None,
                  status: str | None = None) -> list[Approval]:
        q, args = "SELECT body FROM approvals WHERE 1=1", []
        if session_id:
            q += " AND session_id=?"
            args.append(session_id)
        if status:
            q += " AND status=?"
            args.append(status)
        with self.lock:
            rows = self.conn.execute(q, args).fetchall()
        return [Approval.model_validate_json(r["body"]) for r in rows]

    def cas_approval_status(self, a: Approval, expected: str, new: Approval) -> bool:
        """Compare-and-set: only one caller can move an approval out of `expected`."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE approvals SET status=?, body=? WHERE approval_id=? AND status=?",
                (new.status, new.model_dump_json(), a.approval_id, expected),
            )
            return cur.rowcount == 1

    # ---------------------------------------------------------------- attempts
    def insert_attempt(self, at: Attempt) -> Attempt:
        """Insert, or return the existing attempt with the same idempotency key."""
        with self.lock:
            try:
                self.conn.execute(
                    "INSERT INTO attempts (attempt_id, approval_id, idempotency_key, status, body) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (at.attempt_id, at.approval_id, at.idempotency_key, at.status,
                     at.model_dump_json()),
                )
                return at
            except sqlite3.IntegrityError:
                row = self.conn.execute(
                    "SELECT body FROM attempts WHERE idempotency_key=?", (at.idempotency_key,)
                ).fetchone()
                return Attempt.model_validate_json(row["body"])

    def save_attempt(self, at: Attempt) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE attempts SET status=?, body=? WHERE attempt_id=?",
                (at.status, at.model_dump_json(), at.attempt_id),
            )

    def attempts(self, status: str | None = None,
                 approval_id: str | None = None) -> list[Attempt]:
        q, args = "SELECT body FROM attempts WHERE 1=1", []
        if status:
            q += " AND status=?"
            args.append(status)
        if approval_id:
            q += " AND approval_id=?"
            args.append(approval_id)
        with self.lock:
            rows = self.conn.execute(q + " ORDER BY attempt_id", args).fetchall()
        return [Attempt.model_validate_json(r["body"]) for r in rows]

    # ---------------------------------------------------------------- events
    def mark_event_processed(self, event_id: str, ts: str) -> bool:
        with self.lock:
            try:
                self.conn.execute(
                    "INSERT INTO processed_events (event_id, handled_at) VALUES (?, ?)",
                    (event_id, ts),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    # ---------------------------------------------------------------- users
    def save_user(self, u: User) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO users (user_id, name, body) VALUES (?, ?, ?)",
                (u.user_id, u.name, u.model_dump_json()))

    def get_user(self, user_id: str) -> User | None:
        with self.lock:
            row = self.conn.execute("SELECT body FROM users WHERE user_id=?",
                                    (user_id,)).fetchone()
        return User.model_validate_json(row["body"]) if row else None

    def users(self) -> list[User]:
        with self.lock:
            rows = self.conn.execute("SELECT body FROM users ORDER BY user_id").fetchall()
        return [User.model_validate_json(r["body"]) for r in rows]

    # ---------------------------------------------------------------- security flags
    def add_flag(self, user_id: str, session_id: str, message_id: int | None, ts: str,
                 categories: list[str], excerpt: str) -> int:
        """Record a restricted-data attempt. Returns the user's active flag count."""
        with self.lock:
            self.conn.execute(
                "INSERT INTO security_flags (user_id, session_id, message_id, ts, categories, "
                "excerpt) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, session_id, message_id, ts, json.dumps(categories), excerpt[:200]))
        return self.flag_count(user_id)

    def flag_count(self, user_id: str) -> int:
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM security_flags WHERE user_id=? AND cleared=0",
                (user_id,)).fetchone()
        return int(row["n"])

    def flags(self, user_id: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM security_flags", []
        if user_id:
            q, args = q + " WHERE user_id=?", [user_id]
        with self.lock:
            rows = [dict(r) for r in self.conn.execute(q + " ORDER BY id", args).fetchall()]
        for r in rows:
            r["categories"] = json.loads(r["categories"])
        return rows

    def flagged_users(self, block_after: int) -> list[dict[str, Any]]:
        """One row per user with at least one flag: attempts, active flags, blocked."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT user_id, COUNT(*) AS attempts, SUM(cleared=0) AS active, "
                "MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM security_flags "
                "GROUP BY user_id ORDER BY last_ts DESC").fetchall()
        return [{**dict(r), "blocked": int(r["active"] or 0) >= block_after} for r in rows]

    def clear_flags(self, user_id: str) -> int:
        """Unblock a user (flags are kept for audit, marked cleared)."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE security_flags SET cleared=1 WHERE user_id=? AND cleared=0", (user_id,))
            return cur.rowcount
