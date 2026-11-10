"""Searching for a service the user already has booked on those dates adds a short
code-written note to the reply (live UI: Rohan asked for plumbing options today while a
FlowFix plumber was already booked for 11:00 today, and nothing said so)."""

from errand_agent.llm.scripted import say
from evals import scripts as sc
from evals.harness import build

AGAIN = "another plumber in HSR this week?"
SEARCH_AGAIN = [
    sc.update(intent="book",
              fields={"service": "plumber", "area": "HSR Layout",
                      "date_start": "2026-11-10", "date_end": "2026-11-15"},
              evidence={"service": "plumber", "area": "HSR",
                        "date_start": "this week", "date_end": "this week"}),
    sc.search(), sc.present()]


def booked_once():
    h = build(steps=sc.full_booking_steps() + SEARCH_AGAIN,
              mock={"forced_book_outcomes": ["success"]})
    first = h.say(sc.PLUMBER_REQUEST) + h.say("HSR")
    h.say("Go with your recommendation")
    done = h.say("yes")[0]
    return h, first, done


def test_a_search_on_a_day_already_booked_gets_a_note():
    h, first, done = booked_once()
    assert not any("already have" in r for r in first)   # nothing booked yet: no note
    booking_id = h.committed()[0]["booking_id"] if isinstance(h.committed()[0], dict) \
        else h.committed()[0].booking_id
    reply = h.say(AGAIN)[0]
    assert reply.startswith("Here are the best options")
    assert reply.endswith(f"({booking_id}).") and "Note: you already have" in reply
    assert h.of_type("existing_booking_noted")
    result = [e["result"] for e in h.events if e["type"] == "tool_result_to_model"
              and e["tool"] == "search_providers"][-1]
    assert result["already_booked"][0]["booking_id"] == booking_id


def test_no_repeat_when_the_reply_already_mentions_it():
    h = build(steps=sc.full_booking_steps() + SEARCH_AGAIN[:2] + [
        say(lambda v: "You already have "
            + v.last_tool["already_booked"][0]["booking_id"] + " this week. Options: ...")],
        mock={"forced_book_outcomes": ["success"]})
    h.say(sc.PLUMBER_REQUEST), h.say("HSR"), h.say("Go with your recommendation"), h.say("yes")
    reply = h.say(AGAIN)[0]
    assert "Note: you already have" not in reply
