
"""End-to-end smoke tests of the agent loop with the scripted model."""

from __future__ import annotations

from errand_agent.llm.scripted import say
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


def test_open_card_follows_the_latest_reply_and_works_confirms_it():
    # Regression (live UI): after a question, the model said "tap the confirmation card on
    # your screen" while the card sat far up the chat, and "works" didn't confirm it.
    h = build(steps=sc.full_booking_steps()
              + [say("Prices are fixed by the providers and can't be negotiated.")],
              mock={"forced_book_outcomes": ["success"]})
    full = h.say(sc.PLUMBER_REQUEST) + h.say("HSR") + h.say("Go with your recommendation")
    card = full[-1]
    pending = h.agent.parts(h.session_id).gate.pending_for(h.session_id)
    reply = h.say("Can I negotiate the price?")[0]
    assert reply.startswith("Prices are fixed") and reply.endswith(card)
    assert h.agent.parts(h.session_id).gate.pending_for(h.session_id).approval_id == \
        pending.approval_id  # same card, not a new approval
    assert len(h.of_type("approval_card_resurfaced")) == 1
    done = h.say("works")[0]
    assert done.startswith("Booked and verified") and len(h.committed()) == 1


def test_a_freshly_shown_card_is_not_repeated():
    h = build(steps=sc.full_booking_steps())
    h.say(sc.PLUMBER_REQUEST)
    h.say("HSR")
    card = h.say("Go with your recommendation")[0]
    assert card.count("Please confirm") == 1 and not h.of_type("approval_card_resurfaced")
