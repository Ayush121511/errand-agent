"""Estimated LLM cost per call (USD per 1M tokens). Unknown models cost 0 and are flagged."""

from __future__ import annotations

# Gemini API paid-tier list prices for prompts <= 200k tokens. Check
# https://ai.google.dev/gemini-api/docs/pricing before relying on these numbers.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "scripted": (0.0, 0.0),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    key = next((k for k in PRICES_PER_MTOK if model.startswith(k)), None)
    if key is None:
        return None
    pin, pout = PRICES_PER_MTOK[key]
    return round(input_tokens / 1e6 * pin + output_tokens / 1e6 * pout, 6)
