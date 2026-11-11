"""Proactive follow-up when a provider cancels a booking (no user prompt).

Deterministic by design: detection, the notify decision, the replacement search within
stored preferences and the hold are all code. The only thing the user must do is approve
the replacement; nothing is rebooked without a fresh approval.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from errand_agent.agent.ranking import rank
from errand_agent.core.approval import ApprovalGate
from errand_agent.core.clock import Clock
from errand_agent.core.fmt import inr, service_label, when
from errand_agent.core.models import ALLOWED_TRANSITIONS, HoldInfo, Phase, Session, SlotValue, Task
from errand_agent.core.provider_client import ProviderClient
from errand_agent.core.workflow import transition
from errand_agent.memory.store import MemoryStore
from errand_agent.obs.trace import Tracer


def _eligible(memory: MemoryStore, user_id: str, key: str, scope: str | None):
    for m in memory.all(user_id, ("active",)):
        if m.key == key and m.scope in (None, scope) and (m.kind == "explicit" or m.confirmed):
            return m
    return None


def handle_provider_cancellation(
    session: Session, event: dict[str, Any], client: ProviderClient, gate: ApprovalGate,
    memory: MemoryStore, tracer: Tracer, clock: Clock,
) -> str | None:
    r = client.call("list_bookings", {"user_id": session.user_id}, origin="proactive")
    rows = r.data.get("bookings", []) if r.ok else []
    b = next((x for x in rows if x["booking_id"] == event["booking_id"]), None)
    if b is None:
        tracer.event("proactive_decision", event_id=event["event_id"], notify=False,
                     reason="booking_not_found")
        return None
    if b["when"] == "past":
        tracer.event("proactive_decision", event_id=event["event_id"], notify=False,
                     reason="booking_in_past")
        return None
    if b["status"] != "cancelled_by_provider":
        tracer.event("proactive_decision", event_id=event["event_id"], notify=False,
                     reason=f"status_{b['status']}")
        return None
    hours_left = (date.fromisoformat(b["slot"][:10]) - clock.now().date()).days * 24
    urgency = "urgent" if hours_left <= 24 else "normal"
    tracer.event("proactive_decision", event_id=event["event_id"], notify=True,
                 urgency=urgency, reason="upcoming_booking_cancelled_by_provider")
    session.known_bookings[b["booking_id"]] = "cancelled_by_provider"

    service = b["service"]
    head = (f"Heads-up: {b['provider_name']} cancelled your {service_label(service)} "
            f"booking for {when(b['slot'])} ({event.get('reason', 'no reason given')}). "
            "Nothing has been rebooked yet.")

    # Don't trample what the user is doing. With a confirmation card open (or awaiting
    # payment), building a replacement would replace that card: say what happened and let
    # them ask for a replacement after. An unfinished booking errand is parked, not dropped.
    cur = session.task
    open_card = gate.store.get_approval(cur.pending_approval_id) \
        if cur is not None and cur.pending_approval_id else None
    if open_card is not None and open_card.status in ("pending", "approved"):
        tracer.event("proactive_decision", event_id=event["event_id"], notify=True,
                     reason="card_open_replacement_deferred")
        return head + (" You have a confirmation open right now, so I haven't searched for a "
                       "replacement. Once you've finished with it, say \"find a replacement\" "
                       "and I'll look.")
    if (cur is not None and cur.intent == "book" and cur.outcome is None and cur.fields
            and session.phase not in (Phase.DONE, Phase.FAILED, Phase.IDLE)
            and len(session.errands) < 3):
        cur.pending_approval_id = None
        session.errands.insert(0, cur)
        tracer.event("errand_parked", task_id=cur.task_id, reason="proactive_replacement")

    # Replacement task built only from stored preferences and the original booking.
    session.task_counter += 1
    t = Task(task_id=f"T{session.task_counter}", intent="book")
    area_m = _eligible(memory, session.user_id, "area", None)
    t.fields["service"] = SlotValue(value=service, source=f"history:{b['booking_id']}")
    if area_m:
        t.fields["area"] = SlotValue(value=area_m.value, source=f"memory:{area_m.memory_id}")
    else:  # no stored area: search where the cancelled provider works (get_provider contract)
        r = client.call("get_provider", {"provider_id": b["provider_id"]}, origin="proactive")
        areas = r.data.get("areas") if r.ok and r.data.get("status") == "ok" else None
        if not areas:
            tracer.event("proactive_decision", event_id=event["event_id"], notify=True,
                         reason="provider_details_unavailable")
            return head + (" I couldn't look up a replacement right now. Tell me when you'd "
                           "like me to try again.")
        t.fields["area"] = SlotValue(value=areas[0], source=f"history:{b['booking_id']}")
    start = max(date.fromisoformat(b["slot"][:10]), clock.now().date())
    t.fields["date_start"] = SlotValue(value=start.isoformat(),
                                       source=f"history:{b['booking_id']}")
    t.fields["date_end"] = SlotValue(value=(start + timedelta(days=3)).isoformat(),
                                     source="policy:replacement_window_3d")
    used = [f"{t.value('area')}"]
    earliest = _eligible(memory, session.user_id, "earliest_time", None)
    if earliest:
        t.fields["earliest_time"] = SlotValue(value=earliest.value,
                                              source=f"memory:{earliest.memory_id}")
        used.append(f"not before {earliest.value}")
    budget = _eligible(memory, session.user_id, "budget_max", service)
    if budget:
        t.fields["budget_max"] = SlotValue(value=int(budget.value),
                                           source=f"memory:{budget.memory_id}")
        used.append(f"under {inr(int(budget.value))}")
    pref = _eligible(memory, session.user_id, "preferred_provider", service)
    if pref and pref.value != b["provider_id"]:
        t.fields["provider_pref"] = SlotValue(value=pref.value, source=f"memory:{pref.memory_id}")
    avoid = {m.value for m in memory.all(session.user_id, ("active",))
             if m.key == "avoid_provider" and m.scope in (None, service)
             and (m.kind == "explicit" or m.confirmed)}
    avoid.add(b["provider_id"])  # the provider that just cancelled
    session.task = t
    tracer.event("task_started", task_id=t.task_id, intent="book", origin="proactive",
                 fields={k: {"value": v.value, "source": v.source} for k, v in t.fields.items()})

    if Phase.GATHERING in ALLOWED_TRANSITIONS[session.phase]:
        transition(session, Phase.GATHERING, tracer, "proactive_replacement")
    transition(session, Phase.SEARCHING, tracer, "proactive_search")
    sr = client.call("search_providers", {"service": service, "area": t.value("area"),
                                          "date_range": {"start": t.value("date_start"),
                                                         "end": t.value("date_end")}},
                     origin="proactive")
    if not sr.ok or sr.data.get("status") != "ok":
        transition(session, Phase.GATHERING, tracer, "proactive_search_failed")
        return head + " I tried to find a replacement but the provider search failed. " \
                      "Want me to try again?"
    cands = [c for c in rank(sr.data["providers"], t, avoid) if c.provider_id not in avoid]
    t.candidates = cands
    transition(session, Phase.PRESENTING, tracer, "proactive_ranked")
    best = next((c for c in cands if c.fits), None)
    if best is None:
        return head + (" I couldn't find a replacement that fits your saved preferences ("
                       + ", ".join(used) + ") in the next few days. Want me to widen the search?")
    # Closest to the original appointment: the user had planned their day around it.
    slot = min(best.fitting_slots, key=lambda x: abs(
        (datetime.fromisoformat(x) - datetime.fromisoformat(b["slot"])).total_seconds()))
    transition(session, Phase.HOLDING, tracer, "proactive_hold")
    hr = client.call("hold_slot", {"provider_id": best.provider_id, "slot": slot},
                     origin="proactive")
    if not hr.ok or hr.data.get("status") != "ok":
        transition(session, Phase.PRESENTING, tracer, "proactive_hold_failed")
        return head + f" {best.name} looks like the best replacement but I couldn't hold the " \
                      "slot. Want me to try another?"
    d = hr.data
    t.hold = HoldInfo(hold_id=d["hold_id"], provider_id=best.provider_id, provider_name=best.name,
                      service=service, slot=slot, quoted_price=d["quoted_price"],
                      search_price=d["search_price"], expires_at=d["expires_at"])
    warnings = []
    if t.value("budget_max") is not None and d["quoted_price"] > int(t.value("budget_max")):
        warnings.append(f"This is above your budget of {inr(int(t.value('budget_max')))}.")
    approval = gate.create(session, "book", {
        "provider_id": best.provider_id, "provider_name": best.name, "service": service,
        "issue": f"replacement for {b['booking_id']}", "slot": slot, "price": d["quoted_price"],
        "search_price": d["search_price"], "hold_id": d["hold_id"],
        "hold_expires_at": d["expires_at"], "warnings": warnings,
        "replaces_booking_id": b["booking_id"],
    })
    t.pending_approval_id = approval.approval_id
    transition(session, Phase.AWAITING_APPROVAL, tracer, "proactive_approval_requested")
    return (head + f"\n\nBased on your saved preferences ({', '.join(used)}), the best "
            f"replacement is {best.name} (rated {best.rating}). I've held the slot; it will only "
            f"be booked if you confirm.\n\n{approval.card}")
