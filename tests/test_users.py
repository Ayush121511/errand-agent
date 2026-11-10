"""Several users: each sees only their own data, and nobody can act on someone else's."""

import pytest
from fastapi.testclient import TestClient

from errand_agent.app import factory, server
from errand_agent.app.factory import build_live
from errand_agent.core.clock import FakeClock
from errand_agent.llm.scripted import ScriptedLLM, call, say
from errand_agent.seed import seed_all
from evals import scripts as sc
from evals.harness import build


def three_users(steps=None, **kw):
    h = build(steps=steps or [], **kw)
    seed_all(h.store, h.agent.memory, h.clock, h.mock)
    return h


def chat(h, user_id):
    sid = f"S-{user_id}"
    h.agent.open_session(sid, user_id=user_id)
    return sid


def last_tool_result(h, sid, tool):
    evs = [e for e in h.agent.tracer(sid).events
           if e["type"] == "tool_result_to_model" and e["tool"] == tool]
    return evs[-1]["result"]


def test_three_profiles_with_their_own_memory_and_history():
    h = three_users()
    assert [u.name for u in h.store.users()] == ["Ananya Rao", "Rohan Mehta", "Priya Nair"]
    areas = {uid: [m.value for m in h.agent.memory.all(uid, ("active",)) if m.key == "area"]
             for uid in ("user_1", "user_2", "user_3")}
    assert areas == {"user_1": ["HSR Layout"], "user_2": ["Indiranagar"],
                     "user_3": ["Whitefield"]}
    with h.mock.acting_as("user_2"):
        rows = h.mock.list_bookings("user_2")["bookings"]
    assert rows and all(r["provider_id"] in ("ELE-04", "CLN-04", "PLB-01") for r in rows)


def test_greeting_is_personal():
    h = three_users()
    text = h.agent.greet(chat(h, "user_2"))
    assert text.startswith("Welcome, Rohan! Here's the next free slot for each service in "
                           "Indiranagar, from 10:00")


def test_list_bookings_and_memory_never_cross_users():
    h = three_users(steps=[call("list_bookings"), say("Here are your bookings.")])
    sid = chat(h, "user_2")
    h.agent.handle_message(sid, "show my bookings")
    rows = last_tool_result(h, sid, "list_bookings")["bookings"]
    with h.mock.acting_as("user_1"):
        user_1_ids = {b["booking_id"] for b in h.mock.list_bookings("user_1")["bookings"]}
    assert rows and not ({b["booking_id"] for b in rows} & user_1_ids)
    ctx = [e for e in h.agent.tracer(sid).events if e["type"] == "context_built"][-1]
    user_2_ids = {m.memory_id for m in h.agent.memory.all("user_2")}
    assert set(ctx["memory_included"]) <= user_2_ids
    assert not any("HSR Layout" in line for line in ctx["memory_lines"])


def test_a_booking_belongs_to_the_user_who_made_it():
    steps = [sc.update(intent="book",
                       fields={"service": "electrician", "date_start": "2026-11-12",
                               "date_end": "2026-11-12"},
                       evidence={"service": "electrician", "date_start": "Thursday",
                                 "date_end": "Thursday"},
                       from_memory=lambda v: {"area": v.memory_id("area")}),
             sc.search(), sc.hold_top(), sc.propose()]
    h = three_users(steps=steps, mock={"forced_book_outcomes": ["success"]})
    sid = chat(h, "user_2")
    card = h.agent.handle_message(sid, "electrician on Thursday")[0]
    assert card.startswith("Please confirm")
    done = h.agent.handle_message(sid, "yes")[0]
    assert done.startswith("Booked and verified")
    (booking,) = h.committed()
    assert booking.user_id == "user_2"


def test_cannot_cancel_or_see_another_users_booking():
    h = three_users()
    with h.mock.acting_as("user_1"):
        theirs = next(b["booking_id"] for b in h.mock.list_bookings("user_1")["bookings"]
                      if b["when"] == "upcoming")
    with h.mock.acting_as("user_2"):
        assert h.mock.list_bookings("user_1")["error"] == "forbidden"
        assert h.mock.cancel(theirs, "k-1")["error"] == "booking_not_found"
        assert h.mock.reschedule(theirs, "2026-11-14T10:00", "k-2")["error"] == \
            "booking_not_found"
    # and through the agent: user_2 asking to cancel user_1's booking gets no card
    h.llm.extend([call("propose_cancel", {"booking_id": theirs}), say("I can't find that.")])
    sid = chat(h, "user_2")
    h.agent.handle_message(sid, f"cancel {theirs}")
    assert not last_tool_result(h, sid, "propose_cancel")["ok"]
    assert not h.store.approvals(sid)


def test_one_users_chat_cannot_confirm_another_users_card():
    h = three_users(steps=sc.full_booking_steps(), mock={"forced_book_outcomes": ["success"]})
    h.say(sc.PLUMBER_REQUEST)
    h.say("HSR")
    h.say("Go with your recommendation")
    card = h.agent.parts(h.session_id).gate.pending_for(h.session_id)
    other = chat(h, "user_2")
    reply = h.agent.handle_approval(other, card.approval_id, card.fingerprint, True)[0]
    assert "belongs to a different chat" in reply
    assert not h.committed()
    assert h.store.get_approval(card.approval_id).status == "pending"


def test_provider_cancellation_reaches_the_right_user():
    h = three_users()
    sid_1, sid_2 = chat(h, "user_1"), chat(h, "user_2")
    with h.mock.acting_as("user_2"):
        mine = next(b["booking_id"] for b in h.mock.list_bookings("user_2")["bookings"]
                    if b["when"] == "upcoming")
    h.mock.simulate_provider_cancellation(mine, "Technician unavailable")
    out = h.agent.check_proactive()
    assert list(out) == [sid_2] and "Heads-up" in out[sid_2][0]
    assert sid_1 not in out


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, "RealClock", FakeClock)
    server._live = build_live(data_dir=tmp_path, trace_dir=tmp_path / "traces",
                              llm=ScriptedLLM([]))
    yield TestClient(server.app)
    server._live = None


def test_web_api_users_sessions_and_bookings(client):
    users = client.get("/api/users").json()["users"]
    assert [u["user_id"] for u in users] == ["user_1", "user_2", "user_3"]
    sid = client.post("/api/session", json={"user_id": "user_3"}).json()["session_id"]
    s = client.get(f"/api/session/{sid}").json()
    assert s["user"]["name"] == "Priya Nair"
    assert s["messages"][0]["text"].startswith("Welcome, Priya!")
    assert client.post("/api/session", json={"user_id": "nobody"}).status_code == 404
    mine = client.get("/api/bookings", params={"session_id": sid}).json()["bookings"]
    assert mine and all(b["provider_id"] in ("PST-02", "ACS-04", "CRP-02") for b in mine)


def test_picker_shows_the_area_from_memory_not_the_signup_area(client):
    def labels():
        return {u["user_id"]: u["area"] for u in client.get("/api/users").json()["users"]}
    assert labels()["user_1"] == "HSR Layout"
    server._live.agent.memory.add("user_1", "area", "Indiranagar", "explicit",
                                  evidence="I've moved to Indiranagar")
    after = client.get("/api/users").json()["users"][0]
    assert after["area"] == "Indiranagar" and after["home_area"] == "HSR Layout"
    assert labels() == {"user_1": "Indiranagar", "user_2": "Indiranagar",
                        "user_3": "Whitefield"}
