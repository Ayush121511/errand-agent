"""Estimated LLM cost per call (USD per 1M tokens). Unknown models cost 0 and are flagged."""

from __future__ import annotations

# Gemini API paid-tier list prices (text, prompts <= 200k tokens), read from
# https://aistudio.google.com/docs/pricing on 2026-11-10. Longest prefix wins.
# gemini-3.6-flash / 3.8-flash double on 2027-01-01.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "scripted": (0.0, 0.0),
    "replay": (0.0, 0.0),
    "gemini-4-argon": (2.00, 10.00),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    key = max((k for k in PRICES_PER_MTOK if model.startswith(k)), key=len, default=None)
    if key is None:
        return None
    pin, pout = PRICES_PER_MTOK[key]
    return round(input_tokens / 1e6 * pin + output_tokens / 1e6 * pout, 6)
