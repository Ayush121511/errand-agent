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

from errand_agent.agent.prompts import system_prompt
from errand_agent.core.approval import ApprovalGate
from errand_agent.core.clock import Clock
from errand_agent.core.models import Session
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


def state_snapshot(session: Session, gate: ApprovalGate, clock: Clock) -> dict[str, Any]:
    now = clock.now()
    t = session.task
    task: dict[str, Any] | None = None
    if t is not None:
        pending = gate.pending_for(session.session_id)
        task = {
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
    return {
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "weekday": now.strftime("%A"),
        "user_id": session.user_id,
        "phase": session.phase.value,
        "task": task,
    }


class ContextBuilder:
    def __init__(self, store: AgentStore, memory: MemoryStore, gate: ApprovalGate, clock: Clock,
                 budget_tokens: int = 6000, memory_budget_tokens: int = 600,
                 tool_tokens: int = 0, prior_history_cap: int = 6) -> None:
        self.tool_tokens = tool_tokens  # tool declarations are sent on every call: count them
        self.prior_history_cap = prior_history_cap
        self.store = store
        self.memory = memory
        self.gate = gate
        self.clock = clock
        self.budget = budget_tokens
        self.memory_budget = memory_budget_tokens

    def build(self, session: Session,
              scratch: list[ChatMessage]) -> tuple[list[ChatMessage], dict[str, Any]]:
        service = session.task.value("service") if session.task else None
        included, excluded = self.memory.select_for_context(session.user_id, service,
                                                            self.memory_budget)
        mem_lines = [m.line() for m in included]
        state = state_snapshot(session, self.gate, self.clock)
        system = (
            system_prompt(self.clock.now())
            + "\n\n## STATE (authoritative)\n```json\n"
            + json.dumps(state, ensure_ascii=False)
            + "\n```\n\n## MEMORY (current, user-approved preferences relevant to this task)\n"
            + ("\n".join(f"- {line}" for line in mem_lines) if mem_lines else "- (none)")
        )
        system_msg = ChatMessage(role="system", content=system)
        used = message_tokens(system_msg) + self.tool_tokens
        sections = {"tool_schemas": self.tool_tokens,
                    "system_state_memory": message_tokens(system_msg)}

        # Scratch: compact older tool results first if needed.
        scratch = [m.model_copy() for m in scratch]
        for m in scratch:
            if m.role == "tool" and len(m.content) > TOOL_RESULT_CAP:
                m.content = m.content[:TOOL_RESULT_CAP] + "...[truncated]"
        truncated = 0
        scratch_tokens = sum(message_tokens(m) for m in scratch)
        for m in scratch:
            if used + scratch_tokens <= self.budget:
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
            if used + hist_tokens + cost + reserve > self.budget:
                if not picked:  # always keep the latest user message, trimmed if necessary
                    room_chars = max(40, (self.budget - used - reserve - 4) * 4)
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
            "within_budget": used <= self.budget,
            "sections": sections,
            "memory_included": [m.memory_id for m in included],
            "memory_lines": mem_lines,
            "memory_excluded": excluded,
            "history_included_ids": [r["id"] for r in picked if not r.get("withheld")],
            "history_withheld_ids": [r["id"] for r in picked if r.get("withheld")],
            "history_omitted": omitted,
            "scratch_messages": len(scratch),
            "scratch_truncated": truncated,
            "context_sha": digest,
        }
        return messages, manifest
