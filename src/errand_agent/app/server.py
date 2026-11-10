"""Web UI for the demo: chat on the left, live trace on the right.

    uv run uvicorn errand_agent.app.server:app --port 8000

The agent is synchronous and not built for concurrent turns, so one lock serialises all
agent work. A background thread polls provider events so proactive messages appear
without a user prompt.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from errand_agent.app.factory import build_live

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Errand Agent")
_lock = threading.RLock()
_live = None
_subscribers: dict[str, list[queue.Queue]] = {}


def live():
    global _live
    if _live is None:
        _live = build_live()
        threading.Thread(target=_poll_events, daemon=True).start()
    return _live


def _publish(session_id: str, item: dict[str, Any]) -> None:
    for q in list(_subscribers.get(session_id, [])):
        q.put(item)


def _poll_events() -> None:
    while True:
        time.sleep(3)
        try:
            with _lock:
                out = live().agent.check_proactive()
            for sid, msgs in out.items():
                for m in msgs:
                    _publish(sid, {"kind": "chat", "role": "assistant", "text": m,
                                   "proactive": True, "pending": _pending(sid)})
        except Exception as e:  # noqa: BLE001 - keep the poller alive
            print("proactive poll error:", e)


def _pending(session_id: str) -> dict[str, Any] | None:
    a = live().agent.parts(session_id).gate.pending_for(session_id)
    return {"approval_id": a.approval_id, "fingerprint": a.fingerprint, "action": a.action} \
        if a else None


class ChatIn(BaseModel):
    session_id: str
    text: str


class ApproveIn(BaseModel):
    session_id: str
    approval_id: str
    fingerprint: str
    approve: bool = True


class ForceIn(BaseModel):
    outcome: str


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.post("/api/session")
def new_session() -> dict[str, Any]:
    with _lock:
        s = live().agent.open_session()
        recovered = live().recovered
    return {"session_id": s.session_id, "model": live().llm.model,
            "recovered": recovered}


@app.get("/api/session/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    with _lock:
        agent = live().agent
        s = agent.open_session(session_id)
        msgs = [{"role": m["role"], "text": m["content"]}
                for m in agent.store.messages(s.user_id, s.session_id)]
        return {"session_id": s.session_id, "messages": msgs, "pending": _pending(session_id),
                "phase": s.phase.value, "model": live().llm.model}


@app.post("/api/chat")
def chat(body: ChatIn) -> dict[str, Any]:
    with _lock:
        replies = live().agent.handle_message(body.session_id, body.text)
        s = live().store.load_session(body.session_id)
        return {"replies": replies, "pending": _pending(body.session_id),
                "phase": s.phase.value if s else None}


@app.post("/api/approve")
def approve(body: ApproveIn) -> dict[str, Any]:
    with _lock:
        replies = live().agent.handle_approval(body.session_id, body.approval_id,
                                               body.fingerprint, body.approve)
        return {"replies": replies, "pending": _pending(body.session_id)}


@app.get("/api/bookings")
def bookings() -> dict[str, Any]:
    with _lock:
        return live().provider.list_bookings(live().agent.config.user_id)


@app.post("/api/debug/force_book")
def force_book(body: ForceIn) -> dict[str, Any]:
    with _lock:
        live().provider.config.forced_book_outcomes = [body.outcome]  # type: ignore[list-item]
    return {"forced": body.outcome}


@app.post("/api/debug/provider_cancel")
def provider_cancel() -> dict[str, Any]:
    with _lock:
        p = live().provider
        up = [b for b in p.list_bookings(live().agent.config.user_id)["bookings"]
              if b["when"] == "upcoming" and b["status"] == "confirmed"]
        if not up:
            return {"cancelled": None}
        ev = p.simulate_provider_cancellation(up[-1]["booking_id"], "Technician unavailable")
    return {"cancelled": up[-1]["booking_id"], "event": ev["event_id"]}


@app.get("/api/admin/flagged")
def flagged() -> dict[str, Any]:
    """Users who tried to pull restricted data, with every attempt (for support/audit)."""
    with _lock:
        a = live().agent
        return {"block_after": a.config.block_after_flags,
                "users": a.store.flagged_users(a.config.block_after_flags),
                "attempts": a.store.flags()}


@app.post("/api/admin/unblock")
def unblock() -> dict[str, Any]:
    with _lock:
        a = live().agent
        return {"user_id": a.config.user_id, "cleared": a.store.clear_flags(a.config.user_id)}


@app.get("/api/trace/{session_id}/stream")
async def trace_stream(session_id: str) -> StreamingResponse:
    q: queue.Queue = queue.Queue()
    with _lock:
        tracer = live().agent.tracer(session_id)
        backlog = list(tracer.events)

        def listener(ev: dict[str, Any]) -> None:
            q.put({"kind": "trace", "event": ev})

        tracer.subscribe(listener)
        _subscribers.setdefault(session_id, []).append(q)

    async def gen():
        try:
            for ev in backlog:
                yield f"data: {json.dumps({'kind': 'trace', 'event': ev}, default=str)}\n\n"
            idle = 0
            while True:
                try:
                    item = q.get_nowait()
                    idle = 0
                    yield f"data: {json.dumps(item, default=str)}\n\n"
                except queue.Empty:
                    await asyncio.sleep(0.15)
                    idle += 1
                    if idle % 40 == 0:
                        yield ": keep-alive\n\n"
        finally:
            tracer.unsubscribe(listener)
            _subscribers.get(session_id, []).remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")
