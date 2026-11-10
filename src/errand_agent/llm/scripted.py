"""Scripted model for offline evals.

A scenario supplies the model's behaviour as an ordered list of steps. A step is either a
fixed `LLMResponse` or a function of the current view (parsed STATE block from the
context, the latest tool result, the latest user message), so scripts stay robust to ids,
slots and prices that are only known at run time.

Scripts can also make the model *misbehave* (call forbidden tools, invent provider ids,
follow injected instructions) to prove that application code, not the model, holds the
invariants.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from errand_agent.llm.base import (
    ChatMessage,
    LLMResponse,
    ToolCall,
    ToolSpec,
    Usage,
    message_tokens,
)

STATE_RE = re.compile(r"## STATE \(authoritative\)\n```json\n(.*?)\n```", re.S)
MEM_RE = re.compile(r"^- (M-\d+): (\w+)(?: \[(\w+)\])? = ", re.M)


@dataclass
class View:
    state: dict[str, Any]
    last_tool: dict[str, Any] | None
    last_tool_name: str | None
    last_user: str
    messages: list[ChatMessage]

    @property
    def task(self) -> dict[str, Any]:
        return self.state.get("task") or {}

    def candidates(self) -> list[dict[str, Any]]:
        return self.task.get("candidates") or []

    def memory_id(self, key: str, scope: str | None = None) -> str | None:
        """Id of a memory line the model was shown in the MEMORY block."""
        for m in self.messages:
            if m.role == "system":
                for mid, k, sc in MEM_RE.findall(m.content):
                    if k == key and (sc or None) == scope:
                        return mid
        return None


Step = LLMResponse | Callable[[View], LLMResponse]


def call(name: str, args: dict[str, Any] | Callable[[View], dict[str, Any]] | None = None,
         text: str | None = None) -> Step:
    """A step that issues one tool call (args may be computed from the view)."""

    def step(v: View) -> LLMResponse:
        a = args(v) if callable(args) else (args or {})
        return LLMResponse(text=text, tool_calls=[ToolCall(id=f"call_{name}", name=name, args=a)])

    return step


def say(text: str | Callable[[View], str]) -> Step:
    def step(v: View) -> LLMResponse:
        return LLMResponse(text=text(v) if callable(text) else text)

    return step


def make_view(messages: list[ChatMessage]) -> View:
    state: dict[str, Any] = {}
    for m in messages:
        if m.role == "system":
            match = STATE_RE.search(m.content)
            if match:
                state = json.loads(match.group(1))
    last_tool = last_tool_name = None
    last_user = ""
    for m in reversed(messages):
        if m.role == "tool" and last_tool is None:
            try:
                last_tool = json.loads(m.content)
            except json.JSONDecodeError:
                last_tool = {"raw": m.content}
            last_tool_name = m.name
        if m.role == "user":
            last_user = m.content
            break
    return View(state=state, last_tool=last_tool, last_tool_name=last_tool_name,
                last_user=last_user, messages=messages)


@dataclass
class ScriptedLLM:
    steps: list[Step] = field(default_factory=list)
    model: str = "scripted"
    calls: list[list[ChatMessage]] = field(default_factory=list)

    def complete(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResponse:
        self.calls.append(list(messages))
        if not self.steps:
            resp = LLMResponse(text="(scripted model has no more steps)")
        else:
            step = self.steps.pop(0)
            resp = step(make_view(messages)) if callable(step) else step
        resp.model = self.model
        resp.usage = Usage(input_tokens=sum(message_tokens(m) for m in messages),
                           output_tokens=sum(message_tokens(ChatMessage(
                               role="assistant", content=resp.text or "",
                               tool_calls=resp.tool_calls)) for _ in [0]))
        return resp

    def extend(self, steps: list[Step]) -> None:
        self.steps.extend(steps)
