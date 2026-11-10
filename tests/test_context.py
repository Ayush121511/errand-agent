"""Context builder: budget cap and memory gating (bug #3 in docs/BUGS.md guards the cap)."""

import pytest

from errand_agent.agent.context import ContextBuilder
from errand_agent.core.approval import ApprovalGate
from errand_agent.core.clock import FakeClock
from errand_agent.core.models import Session, SlotValue, Task
from errand_agent.core.store import AgentStore
from errand_agent.memory.store import MemoryStore
from errand_agent.obs.trace import Tracer
from errand_agent.seed import seed_user


def _env():
    clock = FakeClock()
    store = AgentStore()
    mem = MemoryStore(store, clock)
    seed_user(store, mem, clock)
    gate = ApprovalGate(store, Tracer("s", None, clock), clock)
    s = Session(session_id="s", user_id="user_1",
                task=Task(task_id="T1", intent="book",
                          fields={"service": SlotValue(value="plumber", source="user:1")}))
    store.add_message("s", "user_1", "user", "Find a plumber this week", clock.now().isoformat())
    return store, mem, gate, clock, s


@pytest.mark.parametrize("budget", range(1600, 4200, 73))
def test_context_never_exceeds_budget_when_history_is_cut(budget):
    store, mem, gate, clock, s = _env()
    msgs, manifest = ContextBuilder(store, mem, gate, clock, budget).build(s, [])
    assert manifest["history_omitted"] > 0
    assert manifest["total_tokens"] <= budget, manifest["sections"]


def test_only_relevant_current_memories_reach_the_model():
    store, mem, gate, clock, s = _env()
    msgs, manifest = ContextBuilder(store, mem, gate, clock, 6000).build(s, [])
    system = msgs[0].content
    assert "Gate code 4521" not in system  # forgotten
    assert "Koramangala" not in system  # superseded
    assert "preferred_time_of_day" not in system  # inferred, unconfirmed
    assert "RCB" not in system  # irrelevant
    assert "HSR Layout" in system and "1500" in system  # relevant + current
    keys = {mem.get(i).key for i in manifest["memory_included"]}
    assert keys <= {"area", "earliest_time", "budget_max", "avoid_provider", "language",
                    "access_notes"}
