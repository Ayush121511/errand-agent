"""Deterministic provider comparison: hard constraints are applied by code, not the model."""

from __future__ import annotations

from typing import Any

from errand_agent.core.fmt import inr
from errand_agent.core.models import Candidate, Task


def _minutes(hhmm: str | None) -> int | None:
    if not hhmm:
        return None
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def slot_violations(slot: str, task: Task) -> list[str]:
    date, hhmm = slot[:10], slot[11:16]
    t = _minutes(hhmm)
    out = []
    ds, de = task.value("date_start"), task.value("date_end")
    if ds and date < ds or de and date > de:
        out.append("outside_dates")
    earliest = _minutes(task.value("earliest_time"))
    if earliest is not None and t is not None and t < earliest:
        out.append("before_earliest_time")
    ws, we = _minutes(task.value("time_window_start")), _minutes(task.value("time_window_end"))
    if ws is not None and t is not None and t < ws or we is not None and t is not None and t >= we:
        out.append("outside_time_window")
    return out


def rank(providers: list[dict[str, Any]], task: Task,
         avoid: set[str] | None = None) -> list[Candidate]:
    budget = task.value("budget_max")
    preferred = task.value("provider_pref")
    out: list[Candidate] = []
    for p in providers:
        violations: list[str] = []
        fitting = [s for s in p.get("available_slots", []) if not slot_violations(s, task)]
        if budget is not None and p["price"] > int(budget):
            violations.append(f"price {inr(p['price'])} is over budget {inr(int(budget))}")
        if not fitting:
            violations.append("no available slot matches the requested dates/times")
        if avoid and p["provider_id"] in avoid:
            violations.append("user asked to avoid this provider")
        attrs = p.get("attributes", {})
        score = p["rating"] * 10
        if budget:
            score += max(0.0, (int(budget) - p["price"]) / int(budget)) * 5
        score += 2 if attrs.get("verified") else 0
        score += min(attrs.get("warranty_days", 0), 90) / 30
        if preferred and p["provider_id"] == preferred:
            score += 25
        out.append(Candidate(
            provider_id=p["provider_id"], name=p["name"], price=p["price"], rating=p["rating"],
            fits=not violations, violations=violations, fitting_slots=fitting[:6],
            all_slots=p.get("available_slots", []), score=round(score, 2),
            attributes={k: attrs.get(k) for k in ("verified", "years_experience",
                                                  "warranty_days", "specialties")},
        ))
    out.sort(key=lambda c: (not c.fits, -c.score))
    return out
