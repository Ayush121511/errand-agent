"""JSONL tracer.

One file per session (`traces/<session_id>.jsonl`). Every input the agent handles
(user message, approval click, proactive event, startup recovery) is a *run*, bracketed
by `run_start` / `run_end` events that share a `run_id`.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from errand_agent.core.clock import Clock, RealClock

_PHONE = re.compile(r"\+?91[\s-]?\d{2}[\s-]?\d{4}[\s-]?\d{4}|\+91\s?\d{10}")
_MAX_STR = 400


def redact(value: Any) -> Any:
    """Redact phone numbers and truncate long strings so traces stay readable and safe."""
    if isinstance(value, str):
        s = _PHONE.sub("[REDACTED_PHONE]", value)
        return s if len(s) <= _MAX_STR else s[:_MAX_STR] + f"...[+{len(s) - _MAX_STR} chars]"
    if isinstance(value, dict):
        return {k: ("[REDACTED_PHONE]" if k == "phone" else redact(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


class Tracer:
    def __init__(
        self,
        session_id: str,
        trace_dir: str | Path | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.session_id = session_id
        self.clock = clock or RealClock()
        self.events: list[dict[str, Any]] = []
        self.run_id: str | None = None
        self._run_counter = 0
        self._run_t0 = 0.0
        self._lock = threading.Lock()
        self._path: Path | None = None
        self._listeners: list = []
        if trace_dir is not None:
            Path(trace_dir).mkdir(parents=True, exist_ok=True)
            self._path = Path(trace_dir) / f"{session_id}.jsonl"

    @property
    def path(self) -> Path | None:
        return self._path

    def subscribe(self, fn) -> None:
        """Register a callback (e.g. the web UI's live trace stream)."""
        self._listeners.append(fn)

    def start_run(self, kind: str, **data: Any) -> str:
        self._run_counter += 1
        self.run_id = f"{self.session_id}-r{self._run_counter:03d}"
        self._run_t0 = self.clock.monotonic()
        self.event("run_start", kind=kind, **data)
        return self.run_id

    def end_run(self, outcome: str, **data: Any) -> None:
        self.event("run_end", outcome=outcome, **data)
        self.run_id = None

    def event(self, type_: str, **data: Any) -> dict[str, Any]:
        with self._lock:
            ev = {
                "seq": len(self.events) + 1,
                "ts": self.clock.now().isoformat(),
                "session_id": self.session_id,
                "run_id": self.run_id,
                "elapsed_ms": round((self.clock.monotonic() - self._run_t0) * 1000, 1)
                if self.run_id
                else None,
                "type": type_,
                **redact(data),
            }
            self.events.append(ev)
            if self._path is not None:
                with self._path.open("a") as f:
                    f.write(json.dumps(ev, default=str) + "\n")
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:  # a broken UI listener must never break the agent
                pass
        return ev

    def of_type(self, type_: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["type"] == type_]
