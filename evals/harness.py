"""Test/eval harness: builds an agent over fresh stores with a fake clock and scripted model."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from errand_agent.agent.runtime import AgentConfig, ErrandAgent
from errand_agent.core.clock import FakeClock
from errand_agent.core.limits import Limits
from errand_agent.core.store import AgentStore
from errand_agent.llm.scripted import ScriptedLLM, Step
from errand_agent.mocks.config import MockConfig
from errand_agent.mocks.service import MockProviderService


@dataclass
class Harness:
    clock: FakeClock
    mock: MockProviderService
    store: AgentStore
    llm: ScriptedLLM
    agent: ErrandAgent
    session_id: str
    transcript: list[dict[str, str]] = field(default_factory=list)
    trace_dir: Path | None = None

    def say(self, text: str) -> list[str]:
        self.transcript.append({"role": "user", "text": text})
        replies = self.agent.handle_message(self.session_id, text)
        self.transcript += [{"role": "agent", "text": r} for r in replies]
        return replies

    def click(self, approve: bool = True, approval_id: str | None = None,
              fingerprint: str | None = None) -> list[str]:
        if approval_id is None:
            pending = self.agent.parts(self.session_id).gate.pending_for(self.session_id)
            last = pending or max(self.store.approvals(self.session_id),
                                  key=lambda a: a.created_at)
            approval_id = last.approval_id
            fingerprint = fingerprint or last.fingerprint
        self.transcript.append({"role": "user", "text": f"[{'Confirm' if approve else 'Decline'}"
                                                        f" button {approval_id}]"})
        replies = self.agent.handle_approval(self.session_id, approval_id, fingerprint, approve)
        self.transcript += [{"role": "agent", "text": r} for r in replies]
        return replies

    def restart(self, crash_points: set[str] | None = None) -> Harness:
        """Simulate a process restart: new agent over the same DB and the same provider."""
        agent = ErrandAgent(self.llm, self.mock, self.store, self.clock, self.agent.config,
                            crash_points=crash_points or set())
        h = Harness(self.clock, self.mock, self.store, self.llm, agent, self.session_id,
                    self.transcript, self.trace_dir)
        h.agent._tracers = self.agent._tracers  # keep one trace file per session
        return h

    def proactive(self) -> dict[str, list[str]]:
        out = self.agent.check_proactive()
        for msgs in out.values():
            self.transcript += [{"role": "agent", "text": m} for m in msgs]
        return out

    # ---------------------------------------------------------------- inspection helpers
    @property
    def events(self) -> list[dict[str, Any]]:
        return self.agent.tracer(self.session_id).events

    def of_type(self, t: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["type"] == t]

    def session(self):
        return self.store.load_session(self.session_id)

    def committed(self):
        return self.mock.committed_bookings()

    def calls(self, tool: str) -> list[dict[str, Any]]:
        return [c for c in self.mock.state.call_log if c["tool"] == tool]


def build(steps: list[Step] | None = None, mock: dict[str, Any] | None = None,
          limits: Limits | None = None, context_budget: int = 6000,
          crash_points: set[str] | None = None, seed: bool = False,
          trace_dir: str | Path | None = None, session_id: str = "S-test",
          llm: Any = None) -> Harness:
    clock = FakeClock()
    if trace_dir is not None:  # fresh trace file per run of a scenario
        Path(trace_dir).mkdir(parents=True, exist_ok=True)
        (Path(trace_dir) / f"{session_id}.jsonl").unlink(missing_ok=True)
    cfg = {"hold_price_drift_prob": 0.0, **(mock or {})}
    provider = MockProviderService(MockConfig(**cfg), clock)
    store = AgentStore()
    llm = llm if llm is not None else ScriptedLLM(list(steps or []))
    agent = ErrandAgent(
        llm, provider, store, clock,
        AgentConfig(limits=limits or Limits(), context_budget=context_budget,
                    trace_dir=trace_dir),
        crash_points=crash_points,
    )
    if seed:
        from errand_agent.seed import seed_user

        seed_user(store, agent.memory, clock, "user_1")
    h = Harness(clock, provider, store, llm, agent, session_id,
                trace_dir=Path(trace_dir) if trace_dir else None)
    agent.open_session(session_id)
    return h
