"""End-to-end smoke tests of the agent loop with the scripted model."""

from __future__ import annotations

from evals import scripts as sc
from evals.harness import build


def test_happy_path_booking_end_to_end():
    h = build(sc.full_booking_steps(), mock={"forced_book_outcomes": ["success"]})
    r1 = h.say(sc.PLUMBER_REQUEST)
    assert r1 == ["Which area should I search in?"]
    r2 = h.say("HSR")
    assert "recommend" in r2[0]
    r3 = h.say("Go with your recommendation")
    assert r3[0].startswith("Please confirm this booking")
    assert h.committed() == []  # nothing booked before approval
    r4 = h.say("yes")
    assert r4[0].startswith("Booked and verified")
    assert len(h.committed()) == 1
    assert h.session().phase.value == "DONE"
    assert h.of_type("run_end")[-1]["outcome"] == "executed:confirmed"
