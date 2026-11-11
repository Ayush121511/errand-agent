"""Bug 18 (live UI, S-0036): two errands in one chat ("plumber in the morning and in the
evening book an AC service") failed in several ways.

- The AC errand was dropped: the agent tracked one task per chat.
- "Options for both" was refused for AC (search had to match the one task) and the model
  listed AC options it never searched for, with a made-up rating.
- "Cheaper service for the same slot?" became budget_max=1099, which withdrew the open card.
- The model then asked "proceed with Chill Point?", and "yes" hit a dead end that showed the
  internal code "task_changed:budget_max".
"""

from errand_agent.agent.context import state_snapshot
from errand_agent.agent.tools import tool_specs
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
    c = next(c for c in v.state["other_errands"][0]["options"] if c["fits"])
    return {"provider_id": c["provider_id"], "slot": c["slot"]}


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


def test_parked_errands_are_a_short_summary_and_one_errand_chats_pay_nothing():
    h = build(steps=[PLUMBER, AC, sc.search(), AC_SEARCH, say("Options for both.")])
    h.say(TWO)
    snap = state_snapshot(h.session(), h.agent.parts(h.session_id).gate, h.clock)
    parked = snap["other_errands"][0]
    assert set(parked) <= {"task_id", "fields", "missing", "options"}
    assert parked["fields"]["service"] == "plumber"            # values only, no sources
    assert len(parked["options"]) <= 3 and set(parked["options"][0]) == \
        {"provider_id", "name", "price", "rating", "fits", "slot"}
    s = h.session()
    assert "switch_errand" in [t.name for t in tool_specs(s)]
    s.errands = []
    assert "switch_errand" not in [t.name for t in tool_specs(s)]
    ctx = [c for c in h.of_type("context_built")]
    assert ctx[0]["sections"]["tool_schemas"] < ctx[-1]["sections"]["tool_schemas"]


def test_a_one_errand_chat_gets_no_errand_section():
    h = build(steps=sc.full_booking_steps())
    h.say(sc.PLUMBER_REQUEST)
    assert all("## Parked errands" not in call[0].content for call in h.llm.calls)
    h2 = build(steps=[PLUMBER, AC, sc.search(), say("Plumber options.")])
    h2.say(TWO)
    assert "## Parked errands" in h2.llm.calls[-1][0].content   # only once one is parked
    assert len({c["sections"]["tool_schemas"] for c in h.of_type("context_built")}) == 1


def test_a_choice_for_a_parked_errand_is_stored_on_it():
    def pick_parked(v):
        e = v.state["other_errands"][0]
        c = next(c for c in e["options"] if c["fits"])
        return {"task_id": e["task_id"], "fields": {"provider_pref": c["provider_id"]},
                "evidence": {"provider_pref": "the first one for the AC"}}

    h = build(steps=[
        PLUMBER, AC, sc.search(), AC_SEARCH, say("Options for both."),
        call("hold_slot", hold_parked), call("update_task", pick_parked), sc.propose(),
    ], mock={"forced_book_outcomes": ["success"]})
    h.say(TWO)
    card = h.say("quickdrip for the plumber and the first one for the AC")[0]
    s = h.session()
    assert card.startswith("Please confirm") and s.task.value("service") == "plumber"
    ac = s.errands[0]
    assert ac.value("provider_pref") and ac.fields["provider_pref"].source.startswith("user:")
    assert ac.candidates  # a provider choice doesn't throw away the AC results
    name = next(c.name for c in ac.candidates if c.provider_id == ac.value("provider_pref"))
    done = h.say("yes")[0]
    assert f"You picked {name}" in done and done.endswith("?")


def test_a_parked_errand_with_new_dates_drops_its_stale_results():
    def move_parked(v):
        e = v.state["other_errands"][0]
        return {"task_id": e["task_id"], "fields": {"date_start": "2026-11-12",
                                                    "date_end": "2026-11-12"},
                "evidence": {"date_start": "AC on the 12th instead",
                             "date_end": "AC on the 12th instead"}}

    h = build(steps=[PLUMBER, AC, sc.search(), AC_SEARCH, say("Options for both."),
                     call("hold_slot", hold_parked), call("update_task", move_parked),
                     say("Noted: AC on the 12th.")])
    h.say(TWO)
    h.say("plumber first, and AC on the 12th instead")
    ac = h.session().errands[0]
    assert ac.value("date_start") == "2026-11-12" and ac.candidates == []


def test_a_provider_name_from_the_results_is_accepted_as_the_choice():
    def by_name(v):
        e = v.state["other_errands"][0]
        c = next(c for c in e["options"] if c["fits"])
        return {"task_id": e["task_id"], "fields": {"provider_pref": c["name"].upper()},
                "evidence": {"provider_pref": "the first one for the AC"}}

    h = build(steps=[PLUMBER, AC, sc.search(), AC_SEARCH, say("Options for both."),
                     call("hold_slot", hold_parked), call("update_task", by_name),
                     say("Noted.")])
    h.say(TWO)
    h.say("plumber first, and the first one for the AC")
    ac = h.session().errands[0]
    assert ac.value("provider_pref") in {c.provider_id for c in ac.candidates}


def test_the_fixed_part_shrinks_when_the_conversation_would_not_fit():
    def run(budget):
        h = build(steps=[PLUMBER, AC, sc.search(), AC_SEARCH, say("Options for both.")],
                  seed=True, context_budget=budget)
        h.say(TWO)
        return h.of_type("context_built")

    roomy, tight = run(6000), run(3500)
    assert not any(c["fixed_part_shrunk"] for c in roomy)
    shrunk = [i for i, c in enumerate(tight) if c["fixed_part_shrunk"]]
    assert shrunk
    assert any(x["reason"] == "over_budget_context"
               for i in shrunk for x in tight[i]["memory_excluded"])
    for i in shrunk:  # memory dropped and STATE compacted: the fixed part got smaller
        assert tight[i]["sections"]["system_state_memory"] < \
            roomy[i]["sections"]["system_state_memory"]


def test_a_parked_errand_without_an_area_takes_the_current_one():
    # live S-0039: the area came from memory for the plumber only; the electrician search
    # was refused and the model listed options it never searched for
    ac_no_area = call("add_errand", {
        "fields": {"service": "ac_servicing", **WEEK,
                   "time_window_start": "17:00", "time_window_end": "21:00"},
        "evidence": {"service": "AC servicing", **WEEK_Q,
                     "time_window_start": "evening", "time_window_end": "evening"}})
    h = build(steps=[PLUMBER, ac_no_area, sc.search(), AC_SEARCH, say("Options for both.")])
    h.say(TWO)
    s = h.session()
    assert h.of_type("errand_area_inherited") and not h.of_type("search_args_rejected")
    assert s.task.value("service") == "ac_servicing" and s.task.candidates
    assert s.task.fields["area"].source == s.errands[0].fields["area"].source


def _cancel_upcoming_ac(h):
    rows = h.mock.list_bookings("user_1")["bookings"]
    ac = next(b for b in rows if b["service"] == "ac_servicing" and b["when"] == "upcoming")
    h.mock.simulate_provider_cancellation(ac["booking_id"], "Technician unwell")


def test_a_provider_cancellation_does_not_replace_an_open_card():
    h = build(steps=sc.full_booking_steps(), seed=True,
              mock={"forced_book_outcomes": ["success"]}, session_id="S-test")
    h.say(sc.PLUMBER_REQUEST), h.say("HSR"), h.say("Go with your recommendation")
    gate = h.agent.parts(h.session_id).gate
    card = gate.pending_for(h.session_id)
    _cancel_upcoming_ac(h)
    out = h.proactive().get(h.session_id, [])
    assert out and out[0].startswith("Heads-up") and "find a replacement" in out[0]
    assert gate.pending_for(h.session_id).approval_id == card.approval_id  # card untouched
    assert h.session().task.value("service") == "plumber"


def test_a_provider_cancellation_parks_an_unfinished_booking():
    h = build(steps=sc.plumber_request_steps() + sc.area_and_search_steps(), seed=True,
              session_id="S-test")
    h.say(sc.PLUMBER_REQUEST), h.say("HSR")           # plumber options shown, no card yet
    _cancel_upcoming_ac(h)
    out = h.proactive().get(h.session_id, [])
    s = h.session()
    assert out and "Please confirm" in out[0]           # replacement AC card
    assert s.task.value("service") == "ac_servicing"
    assert [e.value("service") for e in s.errands] == ["plumber"] and s.errands[0].candidates


def test_search_switches_to_a_parked_errand_when_no_booking_is_current():
    h = build(steps=[PLUMBER, AC, sc.search(), say("Plumber options.")])
    h.say(TWO)
    s = h.session()
    s.errands.insert(0, s.task)          # park the plumber too: nothing current
    s.task = None
    h.store.save_session(s, h.clock.now().isoformat())
    h.llm.steps.extend([AC_SEARCH, say("AC options.")])
    h.say("show the AC options")
    s = h.session()
    assert s.task.value("service") == "ac_servicing" and s.task.candidates
