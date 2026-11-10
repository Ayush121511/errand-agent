"""Re-score the recorded real-model responses (evals/cassettes/live/) offline, for $0.

`python -m evals.live` records one cassette per case; this test replays each one through
the current code and fails if the agent now handles that real model output differently.
"""

import pytest

from evals.live import CASES, CASSETTES, run_case

RECORDED = [c for c in CASES if (CASSETTES / f"{c.id}.jsonl").exists()]


@pytest.mark.skipif(not RECORDED, reason="no live cassettes recorded yet")
@pytest.mark.parametrize("case", RECORDED, ids=[c.id for c in RECORDED])
def test_recorded_live_case_still_passes(case):
    r = run_case(case, replay=True)
    assert r["passed"], f"{r['detail']} | reply: {r['reply'][:120]}"
