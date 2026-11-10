"""The agent talks to the provider only through the brief's 8 fixed tools.

The brief fixes the provider tools ("names, inputs, outputs and failure behaviours") and
leaves the rest to us. Here every scenario runs with the agent's provider wrapped so that
only those 8 tools (plus the simulated cancellation event feed the brief asks for) exist;
any other access, such as reading the mock's catalogue directly, fails the test.
"""

import pytest

import evals.harness as harness
from errand_agent.agent.runtime import ErrandAgent
from errand_agent.core.provider_client import PROVIDER_TOOLS
from evals.run import run_scenario
from evals.scenarios import REGISTRY

# The 8 tools, the simulated cancellation feed, and caller identity (`acting_as`, the mock's
# stand-in for an API auth token; it carries who is calling, not any data).
ALLOWED = set(PROVIDER_TOOLS) | {"poll_events", "ack_event", "acting_as"}


class ContractOnly:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name in ALLOWED:
            return getattr(self._inner, name)
        raise AssertionError(f"agent used the provider outside its contract: .{name}")


class ContractOnlyAgent(ErrandAgent):
    def __init__(self, llm, provider, *args, **kwargs):
        super().__init__(llm, ContractOnly(provider), *args, **kwargs)


@pytest.mark.parametrize("scenario", REGISTRY, ids=[s.id for s in REGISTRY])
def test_scenario_uses_only_the_contract_tools(scenario, monkeypatch):
    monkeypatch.setattr(harness, "ErrandAgent", ContractOnlyAgent)
    result = run_scenario(scenario)
    assert result["passed"], result["notes"]


def test_guard_catches_a_catalogue_read():
    with pytest.raises(AssertionError, match="outside its contract"):
        ContractOnly(object()).catalog  # noqa: B018
