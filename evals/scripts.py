"""Reusable scripted-model steps (what a well-behaved model would do at each point)."""

from __future__ import annotations

from typing import Any

from errand_agent.core.fmt import inr, when
from errand_agent.llm.scripted import Step, View, call, say


def update(fields: dict[str, Any] | None = None, evidence: dict[str, str] | None = None,
           intent: str | None = None, from_memory: dict[str, str] | None = None,
           from_history: dict[str, str] | Any = None) -> Step:
    def args(v: View) -> dict[str, Any]:
        a: dict[str, Any] = {"fields": fields or {}, "evidence": evidence or {}}
        if intent:
            a["intent"] = intent
        if from_memory:
            a["from_memory"] = from_memory(v) if callable(from_memory) else from_memory
        if from_history:
            a["from_history"] = from_history(v) if callable(from_history) else from_history
        return a

    return call("update_task", args)


def ask(fields: list[str], question: str) -> Step:
    return call("ask_user", {"fields": fields, "question": question})


def search() -> Step:
    def args(v: View) -> dict[str, Any]:
        f = v.task["fields"]
        return {"service": f["service"]["value"], "area": f["area"]["value"],
                "date_range": {"start": f["date_start"]["value"], "end": f["date_end"]["value"]}}

    return call("search_providers", args)


def present() -> Step:
    def text(v: View) -> str:
        cands = v.last_tool.get("candidates", []) if v.last_tool else v.candidates()
        fitting = [c for c in cands if c["fits"]][:3]
        if not fitting:
            return "None of the providers fit all your constraints."
        lines = [f"{i + 1}. {c['name']} — {inr(c['price'])}, rated {c['rating']}, "
                 f"earliest fitting slot {when(c['fitting_slots'][0])}"
                 for i, c in enumerate(fitting)]
        return "Here are the best options:\n" + "\n".join(lines) + \
            f"\nI'd recommend {fitting[0]['name']}. Want me to go ahead with it?"

    return say(text)


def hold_top(rank: int = 0, provider_id: str | None = None) -> Step:
    def args(v: View) -> dict[str, Any]:
        cands = [c for c in v.candidates() if c["fits"]] or v.candidates()
        c = next(c for c in cands if c["provider_id"] == provider_id) if provider_id \
            else cands[rank]
        return {"provider_id": c["provider_id"], "slot": c["fitting_slots"][0]}

    return call("hold_slot", args)


def propose() -> Step:
    return call("propose_booking")


def plumber_request_steps() -> list[Step]:
    """Turn 1: 'Find a plumber for my kitchen tap this week, mornings only, under ₹1,500.'"""
    return [
        update(intent="book",
               fields={"service": "plumber", "issue": "kitchen tap", "date_start": "2026-11-10",
                       "date_end": "2026-11-15", "time_window_start": "08:00",
                       "time_window_end": "12:00", "budget_max": 1500},
               evidence={"service": "plumber", "issue": "kitchen tap",
                         "date_start": "this week", "date_end": "this week",
                         "time_window_start": "mornings only", "time_window_end": "mornings only",
                         "budget_max": "under ₹1,500"}),
        ask(["area"], "Which area should I search in?"),
    ]


def area_and_search_steps(area: str = "HSR Layout", quote: str = "HSR") -> list[Step]:
    """Turn 2: user gives the area; model searches and presents options."""
    return [update(fields={"area": area}, evidence={"area": quote}), search(), present()]


def choose_steps(rank: int = 0, provider_id: str | None = None) -> list[Step]:
    """Turn 3: user picks; model holds and proposes."""
    return [hold_top(rank, provider_id), propose()]


PLUMBER_REQUEST = "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500."


def full_booking_steps() -> list[Step]:
    return plumber_request_steps() + area_and_search_steps() + choose_steps()
