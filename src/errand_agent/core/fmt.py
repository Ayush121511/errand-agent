"""Human-readable formatting used by deterministic, code-rendered messages."""

from __future__ import annotations

from datetime import datetime

from errand_agent.core.clock import IST


def inr(amount: int | float | None) -> str:
    if amount is None:
        return "₹?"
    n = int(round(amount))
    s = str(n)
    if len(s) > 3:  # Indian grouping: 1,23,456
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return f"₹{s}"


def when(slot: str) -> str:
    dt = datetime.strptime(slot, "%Y-%m-%dT%H:%M")
    hour = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dt.strftime('%a %d %b')}, {hour}"


def clock_time(iso: str) -> str:
    dt = datetime.fromisoformat(iso).astimezone(IST)
    return dt.strftime("%I:%M %p").lstrip("0")


SERVICE_LABEL = {
    "plumber": "Plumber",
    "electrician": "Electrician",
    "ac_servicing": "AC servicing",
    "house_cleaning": "House cleaning",
    "carpenter": "Carpenter",
    "pest_control": "Pest control",
}


def service_label(s: str | None) -> str:
    return SERVICE_LABEL.get(s or "", s or "service")
