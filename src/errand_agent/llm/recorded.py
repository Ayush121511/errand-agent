"""Record a real model's responses to a cassette, and replay them offline.

    RecordingLLM(GeminiLLM(), "evals/cassettes/demo.jsonl")   # live run, saves responses
    ReplayLLM("evals/cassettes/demo.jsonl")                    # offline, same responses
"""

from __future__ import annotations

import json
from pathlib import Path

from errand_agent.llm.base import ChatMessage, LLMClient, LLMResponse, ToolSpec


class RecordingLLM:
    def __init__(self, inner: LLMClient, path: str | Path) -> None:
        self.inner = inner
        self.model = inner.model
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def complete(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResponse:
        resp = self.inner.complete(messages, tools)
        with self.path.open("a") as f:
            f.write(json.dumps({"last_user": next((m.content for m in reversed(messages)
                                                    if m.role == "user"), ""),
                                "response": resp.model_dump(exclude={"raw"})}) + "\n")
        return resp


class ReplayLLM:
    def __init__(self, path: str | Path) -> None:
        rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
        self._responses = [LLMResponse.model_validate(r["response"]) for r in rows]
        self.model = self._responses[0].model if self._responses else "replay"
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResponse:
        self.calls.append(list(messages))
        if not self._responses:
            return LLMResponse(text="(recording exhausted)", model=self.model)
        return self._responses.pop(0)
