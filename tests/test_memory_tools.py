"""remember / forget with a service: an unknown service must not silently mean 'all'."""

from errand_agent.llm.scripted import call, say
from evals.harness import build


def budgets(h):
    return {(m.scope, m.value) for m in h.agent.memory.all("user_1", ("active",))
            if m.key == "budget_max"}


def test_unknown_service_is_rejected_not_stored_as_general():
    h = build(steps=[
        call("remember", {"key": "budget_max", "value": "1500", "evidence": "under 1500",
                          "scope": "kitchen"}),
        say("Which service is that budget for?"),
    ])
    h.say("keep the kitchen work under 1500")
    result = next(e["result"] for e in h.of_type("tool_result_to_model")
                  if e["tool"] == "remember")
    assert not result["ok"] and "unknown service 'kitchen'" in result["error"]
    assert budgets(h) == set()  # before the fix: {(None, '1500')}, a budget for everything


def test_service_aliases_still_work_and_no_service_means_general():
    h = build(steps=[
        call("remember", {"key": "budget_max", "value": "1500", "evidence": "under 1500",
                          "scope": "plumbing"}),
        call("remember", {"key": "earliest_time", "value": "09:00", "evidence": "after 9",
                          "scope": ""}),
        say("Saved."),
    ])
    h.say("keep plumbing under 1500, and always after 9")
    assert budgets(h) == {("plumber", "1500")}
    assert any(m.key == "earliest_time" and m.scope is None
               for m in h.agent.memory.all("user_1", ("active",)))


def test_forget_with_unknown_service_forgets_nothing():
    h = build(seed=True, steps=[
        call("forget", {"key": "budget_max", "scope": "kitchen",
                        "evidence": "forget my kitchen budget"}),
        say("Which service's budget should I forget?"),
    ])
    before = budgets(h)
    h.say("forget my kitchen budget")
    assert len(before) >= 4 and budgets(h) == before  # before the fix: every budget gone


def test_forget_with_a_known_service_forgets_only_that_one():
    h = build(seed=True, steps=[
        call("forget", {"key": "budget_max", "scope": "AC servicing",
                        "evidence": "forget my AC budget"}),
        say("Done."),
    ])
    before = budgets(h)
    h.say("forget my AC budget")
    after = budgets(h)
    assert before - after == {("ac_servicing", "1800")} and len(after) == len(before) - 1


def test_memory_list_is_written_for_customers_and_guesses_confirm_by_key():
    h = build(steps=[call("show_memories"),
                     call("confirm_memory", {"key": "preferred_time_of_day",
                                             "evidence": "yes, I prefer mornings"}),
                     call("show_memories")], seed=True)
    shown = h.say("What do you remember about me?")[0]
    assert "M-0" not in shown and "PLB-" not in shown and "ELE-" not in shown
    assert "up to ₹1,500" in shown and "9:00 AM" in shown
    assert "you'd rather not have QuickDrip Services" in shown
    assert "personal things I don't use for bookings" in shown and "Cricket" not in shown
    assert "Guesses from your past bookings" in shown and "seem to prefer" in shown
    again = h.say("yes, I prefer mornings")[0]
    guesses = again.split("**Guesses")[1] if "**Guesses" in again else ""
    assert "book in the morning" not in guesses and "book in the morning" in again
