"""Builds the model context under a fixed token budget and logs exactly what was sent.

Layout (priority order):
  1. system prompt                       (always)
  2. STATE block: task, missing fields, candidates, hold, pending approval   (always)
  3. MEMORY block: only memories that pass MemoryStore.select_for_context
  4. current-turn scratch (tool calls/results), older tool results compacted first
  5. conversation history, most recent first, until the budget is used up

The manifest returned with the messages lists every section's token count, which memory
ids were included, which were excluded and why, and which history messages made it in.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from errand_agent.agent.prompts import ERRANDS_PROMPT, system_prompt
from errand_agent.core.approval import ApprovalGate
from errand_agent.core.clock import Clock
from errand_agent.core.models import Approval, Session, Task
from errand_agent.core.store import AgentStore
from errand_agent.llm.base import ChatMessage, estimate_tokens, message_tokens
from errand_agent.memory.store import MemoryStore

TOOL_RESULT_CAP = 2400  # chars per tool result in the scratchpad
WITHHELD = {
    "restricted": "[message withheld by the system: a request for restricted information, "
                  "refused]",
    "gibberish": "[message withheld by the system: unreadable, the user was asked to rephrase]",
    "blocked": "[message withheld by the system: account paused]",
}


HISTORY_FLOOR = 250  # estimated tokens always left for the conversation itself


def state_snapshot(session: Session, gate: ApprovalGate, clock: Clock) -> dict[str, Any]:
    now = clock.now()
    t = session.task
    task = task_snapshot(t, gate.pending_for(session.session_id)) if t is not None else None
    out: dict[str, Any] = {
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "weekday": now.strftime("%A"),
        "user_id": session.user_id,
        "phase": session.phase.value,
        "task": task,
    }
    if session.errands:
        out["other_errands"] = [errand_summary(e) for e in session.errands]
    return out


def errand_summary(e: Task) -> dict[str, Any]:
    """What the model needs about a parked errand, and no more: its values (sources stay in
    the session record), what's missing, the user's chosen provider and the top 3 options
    with price, rating and one fitting slot each: everything a reply may quote (live:
    without rating, Gemini guessed the parked plumbers' ratings). Two errands in a chat
    are rare, so this stays small rather than repeating the current task's full shape."""
    out: dict[str, Any] = {"task_id": e.task_id,
                           "fields": {k: v.value for k, v in e.fields.items()}}
    if e.missing():
        out["missing"] = e.missing()
    if e.candidates:
        out["options"] = [
            {"provider_id": c.provider_id, "name": c.name, "price": c.price,
             "rating": c.rating, "fits": c.fits,
             "slot": c.fitting_slots[0] if c.fitting_slots else None}
            for c in e.candidates[:3]]
    return out


def task_snapshot(t: Task, pending: Approval | None) -> dict[str, Any]:
    """One shape for the current task and parked errands. Empty keys are left out (absent
    means none), which saves tokens on every call without changing what the model knows."""
    snap = {
        "task_id": t.task_id,
        "intent": t.intent,
        "fields": {k: {"value": v.value, "source": v.source} for k, v in t.fields.items()},
        "missing": t.missing(),
        "already_asked": t.asked,
        "candidates": [
            {"rank": i + 1, "provider_id": c.provider_id, "name": c.name, "price": c.price,
             "rating": c.rating, "fits": c.fits, "violations": c.violations,
             "fitting_slots": c.fitting_slots[:4]}
            for i, c in enumerate(t.candidates[:5])
        ],
        "hold": t.hold.model_dump() if t.hold else None,
        "pending_confirmation": (
            {"approval_id": pending.approval_id, "action": pending.action,
             "details": {k: pending.details.get(k) for k in
                         ("provider_name", "slot", "price", "booking_id", "to_slot")}}
            if pending else None
        ),
        "reschedule_options": t.options,
        "last_outcome": t.outcome,
    }
    keep = ("task_id", "intent", "fields", "missing")
    return {k: v for k, v in snap.items() if k in keep or v not in (None, [], {}, "")}


def compact_state(state: dict[str, Any]) -> dict[str, Any]:
    """Last resort when the fixed part of the context would not fit: keep each errand's top
    candidates and one slot each. Fields, holds and cards are never cut."""
    state = json.loads(json.dumps(state))
    t = state.get("task")
    if t:
        t.pop("already_asked", None)
        cands = t.get("candidates", [])[:2]
        for c in cands:
            c["fitting_slots"] = c.get("fitting_slots", [])[:1]
        if cands:
            t["candidates"] = cands
    for e in state.get("other_errands", []):
        if e.get("options"):
            e["options"] = e["options"][:1]
    return state


class ContextBuilder:
    def __init__(self, store: AgentStore, memory: MemoryStore, gate: ApprovalGate, clock: Clock,
                 budget_tokens: int = 6000, memory_budget_tokens: int = 600,
                 tool_tokens: int = 0, prior_history_cap: int = 6,
                 token_ratio: float = 1.0) -> None:
        self.tool_tokens = tool_tokens  # tool declarations are sent on every call: count them
        self.prior_history_cap = prior_history_cap
        self.store = store
        self.memory = memory
        self.gate = gate
        self.clock = clock
        self.budget = budget_tokens
        self.memory_budget = memory_budget_tokens
        # Real tokens per estimated token (>= 1). The cap applies to estimate x ratio, so the
        # budget holds against the provider's own count, not just our estimate.
        self.token_ratio = token_ratio

    @property
    def cap(self) -> int:
        """The budget in estimated tokens."""
        return int(self.budget / max(1.0, self.token_ratio))

    def build(self, session: Session,
              scratch: list[ChatMessage]) -> tuple[list[ChatMessage], dict[str, Any]]:
        service = session.task.value("service") if session.task else None
        included, excluded = self.memory.select_for_context(session.user_id, service,
                                                            self.memory_budget)
        state = state_snapshot(session, self.gate, self.clock)
        profile = self.store.get_user(session.user_id)
        if profile:  # name only: the model doesn't need the phone or street address
            state["user"] = {"name": profile.name}

        def compose(mems: list[Any], st: dict[str, Any]) -> ChatMessage:
            lines = [m.line() for m in mems]
            return ChatMessage(role="system", content=(
                system_prompt(self.clock.now())
                + ("\n\n" + ERRANDS_PROMPT if st.get("other_errands") else "")
                + "\n\n## STATE (authoritative)\n```json\n"
                + json.dumps(st, ensure_ascii=False)
                + "\n```\n\n## MEMORY (current, user-approved preferences relevant to this "
                "task)\n" + ("\n".join(f"- {x}" for x in lines) if lines else "- (none)")))

        scratch = [m.model_copy() for m in scratch]
        for m in scratch:
            if m.role == "tool" and len(m.content) > TOOL_RESULT_CAP:
                m.content = m.content[:TOOL_RESULT_CAP] + "...[truncated]"
        # The smallest the scratch can get (every tool result replaced by a short marker).
        scratch_floor = sum(message_tokens(m) if m.role != "tool" else 16 for m in scratch)

        # Enforce the cap on the fixed part too. Tool schemas and the prompt can't shrink, so
        # if they, STATE and MEMORY leave less than HISTORY_FLOOR for the conversation, drop
        # memory lines (least relevant last-in first), then compact STATE. Without this the
        # cap was only as safe as the fixed part was small (S27 reached 3548/3500).
        level = 0
        system_msg = compose(included, state)
        while (self.tool_tokens + message_tokens(system_msg) + scratch_floor + HISTORY_FLOOR
               > self.cap) and level < 3:
            level += 1
            if level == 1:
                keep = included[: len(included) // 2]
            elif level == 2:
                keep = []
            else:
                state = compact_state(state)
            if level < 3:
                excluded += [{"memory_id": m.memory_id, "reason": "over_budget_context"}
                             for m in included[len(keep):]]
                included = keep
            system_msg = compose(included, state)
        mem_lines = [m.line() for m in included]
        used = message_tokens(system_msg) + self.tool_tokens
        sections = {"tool_schemas": self.tool_tokens,
                    "system_state_memory": message_tokens(system_msg)}
        truncated = 0
        scratch_tokens = sum(message_tokens(m) for m in scratch)
        for m in scratch:
            if used + scratch_tokens <= self.cap:
                break
            if m.role == "tool" and not m.content.startswith("[older"):
                before = message_tokens(m)
                m.content = "[older tool result omitted to fit the context budget]"
                scratch_tokens -= before - message_tokens(m)
                truncated += 1
        used += scratch_tokens
        sections["scratch"] = scratch_tokens

        # Conversation history: the current session in full (budget permitting) plus only the
        # last few messages of earlier sessions. Durable facts from old chats reach the model
        # through MEMORY; replaying old small talk would only spend the budget on noise.
        # Screened input (restricted requests, gibberish, a blocked user's messages) never
        # enters a model context. It is replaced by a neutral placeholder, so a follow-up like
        # "sorry, ignore that" still makes sense (without it the model guessed "that" meant
        # the budget). The code's own reply to it is kept.
        all_msgs = []
        for r in self.store.messages(session.user_id):
            if r["role"] not in ("user", "assistant"):
                continue
            kind = r["meta"].get("screened")
            if kind and r["role"] == "user":
                r = dict(r, content=WITHHELD[kind], withheld=True)
            all_msgs.append(r)
        current = [r for r in all_msgs if r["session_id"] == session.session_id]
        prior = [r for r in all_msgs if r["session_id"] != session.session_id]
        history = prior[-self.prior_history_cap:] if self.prior_history_cap else []
        history += current
        not_considered = len(all_msgs) - len(history)
        picked: list[dict[str, Any]] = []
        def placeholder(n: int) -> ChatMessage:
            return ChatMessage(role="user",
                               content=f"[{n} earlier messages omitted to fit the context "
                                       "budget; durable facts are in STATE and MEMORY]")

        # Reserve room for the "omitted" placeholder up front: it is only known to be needed
        # after the loop, and adding it afterwards used to push contexts over the cap.
        reserve = message_tokens(placeholder(len(all_msgs)))
        hist_tokens = 0
        for r in reversed(history):
            cost = estimate_tokens(r["content"]) + 4
            if used + hist_tokens + cost + reserve > self.cap:
                if not picked:  # always keep the latest user message, trimmed if necessary
                    room_chars = max(40, (self.cap - used - reserve - 4) * 4)
                    r = dict(r, content=r["content"][:room_chars])
                    picked.append(r)
                    hist_tokens += estimate_tokens(r["content"]) + 4
                break
            picked.append(r)
            hist_tokens += cost
        picked.reverse()
        omitted = len(history) - len(picked) + not_considered
        conv = []
        if omitted:
            conv.append(placeholder(omitted))
            hist_tokens += message_tokens(conv[0])
        conv += [ChatMessage(role=r["role"], content=r["content"]) for r in picked]
        used += hist_tokens
        sections["history"] = hist_tokens

        messages = [system_msg, *conv, *scratch]
        digest = hashlib.sha256(
            "".join(m.role + m.content for m in messages).encode()).hexdigest()[:12]
        manifest = {
            "budget_tokens": self.budget,
            "total_tokens": used,
            "within_budget": used <= self.cap,
            "token_ratio": round(self.token_ratio, 3),
            "projected_tokens": int(used * max(1.0, self.token_ratio)),
            "sections": sections,
            "memory_included": [m.memory_id for m in included],
            "memory_lines": mem_lines,
            "memory_excluded": excluded,
            "history_included_ids": [r["id"] for r in picked if not r.get("withheld")],
            "history_withheld_ids": [r["id"] for r in picked if r.get("withheld")],
            "history_omitted": omitted,
            "scratch_messages": len(scratch),
            "scratch_truncated": truncated,
            "fixed_part_shrunk": level,
            "context_sha": digest,
        }
        return messages, manifest
