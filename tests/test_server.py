"""Smoke test of the web API with the scripted model (no network, no key)."""

from fastapi.testclient import TestClient

from errand_agent.app import server
from errand_agent.app.factory import build_live
from errand_agent.llm.scripted import ScriptedLLM
from evals import scripts as sc


def test_web_api_booking_flow(tmp_path):
    server._live = build_live(data_dir=tmp_path, trace_dir=tmp_path / "traces",
                              llm=ScriptedLLM(sc.full_booking_steps()))
    server._live.provider.config.hold_price_drift_prob = 0.0
    server._live.provider.config.forced_book_outcomes = ["timeout_booked"]
    c = TestClient(server.app)
    sid = c.post("/api/session").json()["session_id"]
    assert c.post("/api/chat", json={"session_id": sid, "text": sc.PLUMBER_REQUEST}).json()[
        "replies"] == ["Which area should I search in?"]
    c.post("/api/chat", json={"session_id": sid, "text": "HSR"})
    res = c.post("/api/chat", json={"session_id": sid, "text": "go with the first"}).json()
    assert res["pending"] and res["replies"][0].startswith("Please confirm")
    done = c.post("/api/approve", json={"session_id": sid, **res["pending"],
                                        "approve": True}).json()
    assert done["replies"][0].startswith("Booked and verified")
    assert "no duplicate" in done["replies"][0]
    rows = c.get("/api/bookings").json()["bookings"]
    assert sum(b["when"] == "upcoming" and b["status"] == "confirmed" for b in rows) == 2
    assert c.get(f"/api/session/{sid}").json()["phase"] == "DONE"
    assert (tmp_path / "traces" / f"{sid}.jsonl").exists()
    server._live = None
