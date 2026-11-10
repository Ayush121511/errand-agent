"""Explicit workflow phase transitions (validated and traced)."""

from __future__ import annotations

from errand_agent.core.models import ALLOWED_TRANSITIONS, IllegalTransition, Phase, Session
from errand_agent.obs.trace import Tracer


def transition(session: Session, to: Phase, tracer: Tracer, reason: str) -> None:
    frm = session.phase
    if frm == to:
        return
    if to not in ALLOWED_TRANSITIONS[frm]:
        tracer.event("illegal_transition", from_=frm.value, to=to.value, reason=reason)
        raise IllegalTransition(f"{frm.value} -> {to.value} ({reason})")
    session.phase = to
    tracer.event("state_transition", from_=frm.value, to=to.value, reason=reason)
