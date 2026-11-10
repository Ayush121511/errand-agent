"""Provider-agnostic LLM interface."""

from __future__ import annotations

import math
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class LLMResponse(BaseModel):
    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model: str = "unknown"
    raw: dict[str, Any] | None = None


class LLMClient(Protocol):
    model: str

    def complete(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResponse: ...


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate (~4 chars/token) used for the context budget.

    A fixed estimator keeps budget tests reproducible offline; real usage reported by the
    provider is traced alongside it.
    """
    return math.ceil(len(text) / 4) if text else 0


def message_tokens(m: ChatMessage) -> int:
    n = estimate_tokens(m.content) + 4
    for tc in m.tool_calls:
        n += estimate_tokens(tc.name) + estimate_tokens(str(tc.args)) + 4
    return n
