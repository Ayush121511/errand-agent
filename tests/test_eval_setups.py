from evals.scenarios import REGISTRY
from evals.setups import SETUPS


def test_every_scenario_has_a_one_line_setup():
    ids = {s.id for s in REGISTRY}
    assert ids == set(SETUPS), f"missing {ids - set(SETUPS)}, extra {set(SETUPS) - ids}"
    assert all("\n" not in v and len(v) < 200 for v in SETUPS.values())
