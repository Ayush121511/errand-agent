"""Opening message of a chat: a welcome (back) and the next free slot for every service.

Written by code, with no model call, so it costs nothing and is always accurate. It uses
the user's stored area and earliest time when they are known (explicit or confirmed, and
active), searches each service for the next 7 days and shows the earliest free slot with
its provider and price. Without a known area it searches every area and names the area.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from errand_agent.core.clock import Clock
from errand_agent.core.fmt import inr, service_label, when
from errand_agent.core.models import Session
from errand_agent.core.provider_client import ProviderClient
from errand_agent.core.store import AgentStore
from errand_agent.memory.store import MemoryStore
from errand_agent.mocks.catalog import AREAS, SERVICES, normalize_area

DAYS_AHEAD = 7


def _preference(memory: MemoryStore, user_id: str, key: str) -> str | None:
    usable = [m for m in memory.active(user_id, key, None)
              if m.kind == "explicit" or m.confirmed]
    return usable[-1].value if usable else None


def current_area(memory: MemoryStore, user_id: str) -> str | None:
    """The area the user searches in now: their latest stated (or confirmed) area in memory.
    The profile's home_area is only where they signed up; "I've moved to X" updates memory."""
    return normalize_area(_preference(memory, user_id, "area") or "") or None


def _when(slot: str, today: date) -> str:
    """"Today, 11:00 AM" / "Tomorrow, 9:00 AM", else the usual "Thu 12 Nov, 9:00 AM"."""
    label = when(slot)
    day = date.fromisoformat(slot[:10])
    if day == today:
        return "Today," + label.split(",", 1)[1]
    if day == today + timedelta(days=1):
        return "Tomorrow," + label.split(",", 1)[1]
    return label


def next_slots(client: ProviderClient, today: str, end: str, areas: list[str],
               earliest: str | None) -> list[dict[str, Any]]:
    """Earliest free slot per service: earliest time first, then rating, then price."""
    rows = []
    for svc in SERVICES:
        best: tuple | None = None
        failed = False
        for ar in areas:
            r = client.call("search_providers", {"service": svc, "area": ar,
                                                 "date_range": {"start": today, "end": end}},
                            origin="greeting")
            if not r.ok or r.data.get("status") != "ok":
                failed = True
                continue
            for p in r.data.get("providers", []):
                for slot in p.get("available_slots", []):
                    if earliest and slot[11:16] < earliest:
                        continue
                    key = (slot, -p["rating"], p["price"])
                    if best is None or key < best[0]:
                        best = (key, {"slot": slot, "provider_id": p["provider_id"],
                                      "provider_name": p["name"], "price": p["price"],
                                      "rating": p["rating"], "area": ar})
        rows.append({"service": svc, **(best[1] if best else {}),
                     "status": "ok" if best else ("unavailable" if failed else "none")})
    return rows


def build_greeting(session: Session, client: ProviderClient, memory: MemoryStore,
                   store: AgentStore, clock: Clock) -> tuple[str, list[dict[str, Any]]]:
    uid = session.user_id
    returning = any(m["session_id"] != session.session_id
                    for m in store.messages(uid) if m["role"] == "user")
    area = current_area(memory, uid)
    earliest = _preference(memory, uid, "earliest_time")
    today = clock.now().date()
    rows = next_slots(client, today.isoformat(),
                      (today + timedelta(days=DAYS_AHEAD - 1)).isoformat(),
                      [area] if area else list(AREAS), earliest)

    where = f"in {area}" if area else "across Bangalore"
    after = f", from {earliest} (your earliest time)" if earliest else ""
    profile = store.get_user(uid)
    first = f", {profile.name.split()[0]}" if profile and profile.name else ""
    hello = f"Welcome back{first}!" if returning else f"Welcome{first}!"
    lines = [f"{hello} Here's the next free slot for "
             f"each service {where}{after}:"]
    for r in rows:
        label = service_label(r["service"])
        if r["status"] == "ok":
            place = "" if area else f", {r['area']}"
            lines.append(f"• {label}: {_when(r['slot'], today)} with {r['provider_name']} "
                         f"({inr(r['price'])}{place})")
        elif r["status"] == "unavailable":
            lines.append(f"• {label}: couldn't check right now")
        else:
            lines.append(f"• {label}: no free slot in the next {DAYS_AHEAD} days")
    lines.append("Tell me what you need, for example \"book the plumber slot\" or \"AC "
                 "servicing on Saturday afternoon\".")
    return "\n".join(lines), rows
