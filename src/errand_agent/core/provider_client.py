"""Traced, error-normalising client for the provider tools.

Every call to the provider service (from the model's tool use or from the deterministic
executors) goes through here, so tracing, latency measurement and failure handling are
uniform. Transport failures never escape as exceptions: they become `ToolResult(ok=False)`
so no caller can fail silently or crash the turn.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from errand_agent.core.clock import Clock
from errand_agent.mocks.errors import ServiceUnavailable, ToolTimeout, ToolTransportError
from errand_agent.mocks.service import MockProviderService
from errand_agent.obs.trace import Tracer

PROVIDER_TOOLS = (
    "search_providers",
    "get_provider",
    "hold_slot",
    "book",
    "get_booking_status",
    "cancel",
    "reschedule",
    "list_bookings",
)
SIDE_EFFECT_TOOLS = ("book", "cancel", "reschedule")


class ToolResult(BaseModel):
    ok: bool
    data: dict[str, Any] = {}
    error: str | None = None
    retryable: bool = False
    latency_ms: float = 0.0


class ProviderClient:
    def __init__(self, service: MockProviderService, tracer: Tracer, clock: Clock,
                 user_id: str | None = None) -> None:
        self.service = service
        self.tracer = tracer
        self.clock = clock
        self.user_id = user_id  # every call is made as this user, like an auth token

    def call(self, name: str, args: dict[str, Any], origin: str = "model",
             attempt: int | None = None) -> ToolResult:
        if name not in PROVIDER_TOOLS:
            raise ValueError(f"unknown provider tool {name}")
        fn = getattr(self.service, name)
        t0 = self.clock.monotonic()
        acting = getattr(self.service, "acting_as", None)
        try:
            if acting is not None and self.user_id is not None:
                with acting(self.user_id):
                    data = fn(**args)
            else:
                data = fn(**args)
            if not isinstance(data, dict) or "status" not in data:
                result = ToolResult(ok=False, data={"raw": str(data)[:200]},
                                    error="malformed_response", retryable=True)
            else:
                result = ToolResult(ok=True, data=data)
        except ToolTimeout as e:
            result = ToolResult(ok=False, error="timeout", retryable=True, data={"detail": str(e)})
        except ServiceUnavailable as e:
            result = ToolResult(ok=False, error="service_unavailable_503", retryable=True,
                                data={"detail": str(e)})
        except ToolTransportError as e:
            result = ToolResult(ok=False, error=e.code, retryable=True, data={"detail": str(e)})
        except TypeError as e:  # bad arguments from the model
            result = ToolResult(ok=False, error="bad_arguments", retryable=False,
                                data={"detail": str(e)})
        except Exception as e:  # noqa: BLE001 - unexpected bug in the tool
            result = ToolResult(ok=False, error="unexpected_error", retryable=False,
                                data={"detail": f"{type(e).__name__}: {e}"})
        result.latency_ms = round((self.clock.monotonic() - t0) * 1000, 1)
        self.tracer.event(
            "tool_call",
            tool=name,
            origin=origin,
            attempt=attempt,
            args=args,
            ok=result.ok,
            error=result.error,
            latency_ms=result.latency_ms,
            result=_summarise(name, result.data),
        )
        return result


def _summarise(name: str, data: dict[str, Any]) -> dict[str, Any]:
    """Trace-friendly result: keep structure, cap the bulky free-text parts."""
    if name == "get_provider" and "reviews" in data:
        d = dict(data)
        d["reviews"] = [
            {"author": r.get("author"), "rating": r.get("rating"),
             "text": (r.get("text") or "")[:160]}
            for r in data["reviews"]
        ]
        return d
    if name == "search_providers" and "providers" in data:
        d = dict(data)
        d["providers"] = [
            {"provider_id": p["provider_id"], "price": p["price"], "rating": p["rating"],
             "slots": len(p.get("available_slots", []))}
            for p in data["providers"]
        ]
        return d
    return data
