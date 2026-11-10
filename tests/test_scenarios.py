"""Runs every eval scenario under pytest; conftest writes the eval table at session end."""

import pytest

from evals.run import run_scenario
from evals.scenarios import REGISTRY

RESULTS: list[dict] = []


@pytest.mark.parametrize("scenario", REGISTRY, ids=[s.id for s in REGISTRY])
def test_scenario(scenario):
    result = run_scenario(scenario)
    RESULTS.append(result)
    assert result["passed"], result["notes"]
