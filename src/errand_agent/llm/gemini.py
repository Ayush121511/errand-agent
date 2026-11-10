"""Gemini adapter (google-genai SDK) implementing the LLMClient protocol.

Notes:
- Automatic function calling is disabled: the SDK must never execute tools itself; every
  tool call goes back to our validating dispatcher.
- Gemini 3.x models attach thought signatures to function-call turns and expect those
  turns to be sent back unchanged. We cache the raw model `Content` per tool-call id and
  replay it verbatim when rebuilding history.
- Transient API errors are retried a bounded number of times; anything else raises and
  the agent runtime turns it into a clear user-facing message.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from errand_agent.llm.base import ChatMessage, LLMResponse, ToolCall, ToolSpec, Usage

DEFAULT_MODEL = "gemini-3.6-flash"
DEFAULT_READER_MODEL = "gemini-3.5-flash-lite"


class GeminiLLM:
    def __init__(self, model: str | None = None, api_key: str | None = None,
                 temperature: float = 0.2, timeout_s: float = 40.0, max_retries: int = 2) -> None:
        from google import genai
        from google.genai import types

        self._types = types
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set (put it in .env)")
        self.client = genai.Client(api_key=key,
                                   http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))
        self.model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
        self.temperature = temperature
        self.max_retries = max_retries
        self._raw_turns: dict[str, Any] = {}  # first tool-call id -> raw model Content

    # ------------------------------------------------------------------ conversion
    def _contents(self, messages: list[ChatMessage]) -> list[Any]:
        t = self._types
        out: list[Any] = []

        def push(role: str, parts: list[Any], mergeable: bool) -> None:
            if (mergeable and out and out[-1].role == role
                    and not any(p.function_call or p.function_response for p in out[-1].parts)):
                out[-1].parts.extend(parts)
            else:
                out.append(t.Content(role=role, parts=parts))

        for m in messages:
            if m.role == "system":
                continue
            if m.role == "user":
                push("user", [t.Part(text=m.content)], mergeable=True)
            elif m.role == "assistant":
                raw = self._raw_turns.get(m.tool_calls[0].id) if m.tool_calls else None
                if raw is not None:
                    out.append(raw)
                    continue
                parts = [t.Part(text=m.content)] if m.content else []
                parts += [t.Part(function_call=t.FunctionCall(id=tc.id, name=tc.name,
                                                              args=tc.args))
                          for tc in m.tool_calls]
                push("model", parts or [t.Part(text="")], mergeable=not m.tool_calls)
            elif m.role == "tool":
                try:
                    payload = json.loads(m.content)
                except json.JSONDecodeError:
                    payload = {"raw": m.content}
                part = t.Part(function_response=t.FunctionResponse(
                    id=m.tool_call_id, name=m.name or "tool", response={"result": payload}))
                if out and out[-1].role == "user" and all(p.function_response
                                                          for p in out[-1].parts):
                    out[-1].parts.append(part)
                else:
                    out.append(t.Content(role="user", parts=[part]))
        if out and out[0].role != "user":
            out.insert(0, t.Content(role="user", parts=[t.Part(text="(conversation continues)")]))
        return out

    def _config(self, system: str, tools: list[ToolSpec]) -> Any:
        t = self._types
        decls = [t.FunctionDeclaration(name=s.name, description=s.description,
                                       parameters_json_schema=s.parameters) for s in tools]
        return t.GenerateContentConfig(
            system_instruction=system or None,
            tools=[t.Tool(function_declarations=decls)] if decls else None,
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
            temperature=self.temperature,
        )

    # ------------------------------------------------------------------ call
    def complete(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMResponse:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        contents = self._contents(messages)
        config = self._config(system, tools)
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.client.models.generate_content(model=self.model, contents=contents,
                                                           config=config)
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                code = getattr(e, "code", None) or getattr(e, "status_code", None)
                if code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
        else:  # pragma: no cover
            raise RuntimeError(f"Gemini call failed: {last_err}")

        cand = resp.candidates[0] if resp.candidates else None
        parts = (cand.content.parts or []) if cand and cand.content else []
        text = "".join(p.text for p in parts if p.text and not p.thought).strip() or None
        calls = []
        for p in parts:
            if p.function_call:
                fc = p.function_call
                calls.append(ToolCall(id=fc.id or f"gc_{uuid.uuid4().hex[:10]}", name=fc.name,
                                      args=dict(fc.args or {})))
        if calls and cand is not None:
            self._raw_turns[calls[0].id] = cand.content
        um = resp.usage_metadata
        usage = Usage(
            input_tokens=(um.prompt_token_count or 0) if um else 0,
            output_tokens=((um.candidates_token_count or 0) + (um.thoughts_token_count or 0))
            if um else 0,
        )
        return LLMResponse(text=text, tool_calls=calls, usage=usage, model=self.model,
                           raw={"finish_reason": str(cand.finish_reason) if cand else None})
