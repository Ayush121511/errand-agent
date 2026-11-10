"""Bug 17: "change location to indranagar" set the area of one request, the reply said "I've
updated your location", and nothing was saved. The greeting, the user picker and the next chat
all kept the old area."""

from errand_agent.agent.greeting import current_area
from errand_agent.agent.runtime import MEMORY_NOT_SAVED, strip_memory_claims
from errand_agent.llm.scripted import call, say
from evals.harness import build

MOVE = "change location to indranagar"
SET_AREA = call("update_task", {"fields": {"area": "Indiranagar"},
                                "evidence": {"area": "indranagar"}})


def lives_in_whitefield(steps):
    h = build(steps=steps)
    h.agent.memory.add("user_1", "area", "Whitefield", "explicit", evidence="I live in Whitefield")
    return h


def tool_result(h, tool):
    return [e for e in h.events if e["type"] == "tool_result_to_model" and e["tool"] == tool][-1][
        "result"]


def test_request_only_area_gets_a_note_and_the_false_claim_is_corrected():
    # exactly what Gemini did live (traces/live/S-0019): update_task only, then the claim
    h = lives_in_whitefield([SET_AREA, say("I've updated your location to Indiranagar. \n\n"
                                           "What service would you like to arrange?")])
    reply = h.say(MOVE)[0]
    note = tool_result(h, "update_task")["note"]
    assert "saved area is still Whitefield" in note and "remember(key='area')" in note
    assert reply.startswith(MEMORY_NOT_SAVED) and "What service would you like" in reply
    assert "updated your location" not in reply
    assert h.of_type("unverified_memory_claim_blocked")
    assert current_area(h.agent.memory, "user_1") == "Whitefield"  # honest: nothing changed


def test_a_real_move_is_saved_and_shows_everywhere():
    h = lives_in_whitefield([
        call("remember", {"key": "area", "value": "Indiranagar", "evidence": MOVE}),
        SET_AREA, say("I've updated your location to Indiranagar.")])
    reply = h.say(MOVE)[0]
    assert reply == "I've updated your location to Indiranagar."  # true now, so untouched
    assert "note" not in tool_result(h, "update_task")             # matches the saved area
    assert not h.of_type("unverified_memory_claim_blocked")
    assert current_area(h.agent.memory, "user_1") == "Indiranagar"  # picker label + greeting
    assert "in Indiranagar" in h.agent.greet("S-next").splitlines()[0]
    old = [m for m in h.agent.memory.all("user_1") if m.key == "area" and m.value == "Whitefield"]
    assert old[0].status == "superseded"


def test_one_visit_elsewhere_keeps_the_saved_area():
    h = lives_in_whitefield([
        call("update_task", {"intent": "book", "fields": {"service": "plumber",
                                                           "area": "Koramangala"},
                             "evidence": {"service": "plumber", "area": "Koramangala"}}),
        say("Sure, a plumber at your mom's place in Koramangala. Which day works?")])
    reply = h.say("need a plumber at my mom's place in Koramangala")[0]
    assert "only" in tool_result(h, "update_task")["note"]
    assert reply.startswith("Sure, a plumber")
    assert current_area(h.agent.memory, "user_1") == "Whitefield"


def test_only_claims_of_saving_are_rewritten():
    assert strip_memory_claims("Got it! I've noted down that you live in Koramangala. "
                               "Anything else?") == f"Got it! {MEMORY_NOT_SAVED} Anything else?"
    assert strip_memory_claims("Your location has been updated to HSR Layout.") == \
        MEMORY_NOT_SAVED
    assert strip_memory_claims("Sure, I'll remember that.") == MEMORY_NOT_SAVED
    for honest in ("I haven't saved your area yet. Want me to?",
                   "I've set the area to Koramangala for this visit. Which day?",
                   "Here are three plumbers. I recommend AquaCare."):
        assert strip_memory_claims(honest) is None
