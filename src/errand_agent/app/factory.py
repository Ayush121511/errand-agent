"""Builds the live agent: real clock, persistent SQLite stores, Gemini, startup recovery."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from errand_agent.agent.review_reader import DeterministicReviewReader, LLMReviewReader
from errand_agent.agent.runtime import AgentConfig, ErrandAgent
from errand_agent.core.clock import RealClock
from errand_agent.core.store import AgentStore
from errand_agent.llm.base import LLMClient
from errand_agent.mocks.config import MockConfig
from errand_agent.mocks.service import MockProviderService
from errand_agent.seed import seed_user

ROOT = Path(__file__).resolve().parents[3]


@dataclass
class LiveApp:
    agent: ErrandAgent
    provider: MockProviderService
    store: AgentStore
    llm: LLMClient
    recovered: dict[str, list[str]]


def build_live(data_dir: str | Path | None = None, trace_dir: str | Path | None = None,
               record_to: str | Path | None = None, llm: LLMClient | None = None,
               seed: bool = True) -> LiveApp:
    load_dotenv(ROOT / ".env")
    data = Path(data_dir or ROOT / "var")
    data.mkdir(parents=True, exist_ok=True)
    clock = RealClock()
    provider = MockProviderService(MockConfig(seed=int(os.environ.get("MOCK_SEED", "42"))),
                                   clock, db_path=data / "mock.db")
    store = AgentStore(data / "agent.db")

    if llm is None:
        from errand_agent.llm.gemini import DEFAULT_READER_MODEL, GeminiLLM

        llm = GeminiLLM()
        reader = LLMReviewReader(GeminiLLM(
            model=os.environ.get("GEMINI_READER_MODEL") or DEFAULT_READER_MODEL))
    else:
        reader = DeterministicReviewReader()
    if record_to:
        from errand_agent.llm.recorded import RecordingLLM

        llm = RecordingLLM(llm, record_to)

    agent = ErrandAgent(llm, provider, store, clock,
                        AgentConfig(trace_dir=trace_dir or ROOT / "traces" / "live"),
                        reader=reader)
    if seed and not agent.memory.all(agent.config.user_id):
        seed_user(store, agent.memory, clock, agent.config.user_id)
    recovered = agent.recover()  # resolve anything a previous crash left pending
    return LiveApp(agent, provider, store, llm, recovered)
