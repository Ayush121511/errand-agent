"""Bug 18 (live UI, S-0036): two errands in one chat ("plumber in the morning and in the
evening book an AC service") failed in several ways.

- The AC errand was dropped: the agent tracked one task per chat.
- "Options for both" was refused for AC (search had to match the one task) and the model
  listed AC options it never searched for, with a made-up rating.
- "Cheaper service for the same slot?" became budget_max=1099, which withdrew the open card.
- The model then asked "proceed with Chill Point?", and "yes" hit a dead end that showed the
  internal code "task_changed:budget_max".
"""

from errand_agent.llm.scripted import call, say
from evals import scripts as sc
from evals.harness import build

TWO = ("plumber for my kitchen tap this week in the mornings in HSR, and AC servicing this "
       "week in the evening")
WEEK = {"date_start": "2026-11-10", "date_end": "2026-11-15"}
WEEK_Q = {"date_start": "this week", "date_end": "this week"}

PLUMBER = sc.update(
    intent="book",
    fields={"service": "plumber", "area": "HSR Layout", **WEEK,
            "time_window_start": "08:00", "time_window_end": "12:00"},
    evidence={"service": "plumber", "area": "HSR", **WEEK_Q,
              "time_window_start": "mornings", "time_window_end": "mornings"})
AC = call("add_errand", {
    "fields": {"service": "ac_servicing", "area": "HSR Layout", **WEEK,
               "time_window_start": "17:00", "time_window_end": "21:00"},
    "evidence": {"service": "AC servicing", "area": "HSR", **WEEK_Q,
                 "time_window_start": "evening", "time_window_end": "evening"}})
AC_SEARCH = call("search_providers", {"service": "ac_servicing", "area": "HSR Layout",
                                      "date_range": {"start": "2026-11-10",
                                                     "end": "2026-11-15"}})


def hold_parked(v):
    """Hold the first fitting candidate of the parked errand (seen in STATE.other_errands)."""
    c = next(c for c in v.state["other_errands"][0]["candidates"] if c["fits"])
    return {"provider_id": c["provider_id"], "slot": c["fitting_slots"][0]}


def tool_results(h, tool):
    return [e["result"] for e in h.events
            if e["type"] == "tool_result_to_model" and e["tool"] == tool]


def test_two_errands_are_tracked_searched_and_booked_one_after_the_other():
    h = build(steps=[
        PLUMBER, AC, sc.search(), AC_SEARCH, say("Here are options for both."),
        call("hold_slot", hold_parked), sc.propose(),          # pick the plumber
        sc.hold_top(), sc.propose(),                            # "yes" to "Next: AC ...?"
    ], mock={"forced_book_outcomes": ["success", "success"]})
    h.say(TWO)
    s = h.session()
    assert h.of_type("errand_added") and not h.of_type("search_args_rejected")
    assert s.task.value("service") == "ac_servicing" and s.task.candidates  # switched by search
    assert [e.value("service") for e in s.errands] == ["plumber"]
    assert s.errands[0].candidates                                 # plumber results kept

    card = h.say("quickdrip for the plumber")[0]
    assert card.startswith("Please confirm") and "Plumber" in card
    assert h.session().task.value("service") == "plumber"          # hold_slot switched back

    done = h.say("yes")[0]
    assert done.startswith("Booked and verified")
    assert "Next: your AC servicing request" in done and done.endswith("?")
    s = h.session()
    assert s.task.value("service") == "ac_servicing" and s.phase.value == "GATHERING"
    assert s.errands == []

    card2 = h.say("yes")[0]                 # answers the question, not a duplicate replay
    assert card2.startswith("Please confirm") and "AC servicing" in card2
    assert not h.of_type("duplicate_confirmation")
    assert h.say("yes")[0].startswith("Booked and verified")
    assert len(h.committed()) == 2


def test_no_switching_errands_while_a_card_is_open():
    h = build(steps=[
        PLUMBER, AC, sc.search(), say("Plumber options."),
        sc.hold_top(), sc.propose(),
        lambda v: call("switch_errand", {"task_id": v.state["other_errands"][0]["task_id"]})(v),
        say("Let's finish the plumber first."),
    ])
    h.say(TWO)
    h.say("first one")
    h.say("switch to the AC one")
    assert "confirmation card" in tool_results(h, "switch_errand")[-1]["error"]
    assert h.session().task.value("service") == "plumber"
    assert h.agent.parts(h.session_id).gate.pending_for(h.session_id) is not None


def test_a_duplicate_errand_is_not_parked_twice():
    h = build(steps=[PLUMBER, AC, AC, say("ok")])
    h.say(TWO)
    assert tool_results(h, "add_errand")[-1]["error"] == "already_tracked"
    assert len(h.session().errands) == 1


def test_cheaper_is_a_question_not_a_budget_and_the_card_survives():
    h = build(steps=sc.full_booking_steps() + [
        sc.update(fields={"budget_max": 1099}, evidence={"budget_max": "cheaper"}),
        say("This is already the cheapest slot that fits."),
    ], mock={"forced_book_outcomes": ["success"]})
    h.say(sc.PLUMBER_REQUEST), h.say("HSR"), h.say("Go with your recommendation")
    h.say("anything cheaper for the same slot?")
    assert "amount" in tool_results(h, "update_task")[-1]["errors"]["budget_max"]
    assert h.agent.parts(h.session_id).gate.pending_for(h.session_id) is not None
    assert h.say("yes")[0].startswith("Booked and verified")


def test_a_correction_the_held_slot_still_meets_keeps_the_card():
    h = build(steps=sc.full_booking_steps() + [
        sc.update(fields={"budget_max": 2000}, evidence={"budget_max": "2000"}),
        say("Noted, the held slot is within ₹2,000."),
    ], mock={"forced_book_outcomes": ["success"]})
    h.say(sc.PLUMBER_REQUEST), h.say("HSR"), h.say("Go with your recommendation")
    h.say("my budget is 2000 actually")
    assert h.of_type("approval_kept") and not h.of_type("approval_invalidated")
    assert h.say("yes")[0].startswith("Booked and verified")


def test_a_correction_the_slot_breaks_withdraws_the_card_in_plain_words():
    h = build(steps=sc.full_booking_steps() + [
        sc.update(fields={"time_window_start": "17:00", "time_window_end": "21:00"},
                  evidence={"time_window_start": "evening", "time_window_end": "evening"}),
        say("Got it, evenings instead. I'll need to search again."),
    ])
    h.say(sc.PLUMBER_REQUEST), h.say("HSR"), h.say("Go with your recommendation")
    h.say("make it evening")
    assert h.of_type("approval_invalidated")
    reply = h.say("yes")[0]
    assert "the request changed (time)" in reply and "task_changed" not in reply
    assert h.committed() == []


def test_yes_to_the_models_own_question_goes_to_the_model():
    h = build(steps=sc.full_booking_steps() + [
        sc.update(fields={"time_window_start": "17:00", "time_window_end": "21:00"},
                  evidence={"time_window_start": "evening", "time_window_end": "evening"}),
        say("Evenings it is. Shall I search for evening slots?"),
        sc.search(), say("Here are the evening options."),
    ])
    h.say(sc.PLUMBER_REQUEST), h.say("HSR"), h.say("Go with your recommendation")
    h.say("make it evening")
    assert h.say("yes") == ["Here are the evening options."]
    assert not h.of_type("stale_confirmation")


def test_resending_a_known_value_with_a_normalised_quote_is_not_an_error():
    h = build(steps=sc.plumber_request_steps() + [
        sc.update(fields={"service": "plumber", "date_start": "2026-11-10"},
                  evidence={"service": "plumber", "date_start": "2026-11-10"}),
        say("ok"),
    ])
    h.say(sc.PLUMBER_REQUEST)
    h.say("go on")
    r = tool_results(h, "update_task")[-1]
    assert r["ok"] and r["errors"] == {} and not h.of_type("evidence_rejected")
