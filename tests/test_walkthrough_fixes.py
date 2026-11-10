"""Loopholes found by walking "Find me a plumber asap" through the live agent."""

from errand_agent.agent.context import ContextBuilder
from errand_agent.agent.runtime import ErrandAgent
from errand_agent.llm.base import LLMResponse, Usage, default_token_ratio
from evals import scripts as sc
from evals.harness import build


def test_remembered_budget_and_earliest_time_bind_even_if_the_model_skips_them():
    # Live run: Gemini recorded service, area and dates but not the remembered ₹1,500 plumber
    # budget or 09:00 earliest time, so code ranked without them. Code now applies them.
    # Same shape with AC servicing, where a provider (₹1,900) is over the remembered ₹1,800.
    steps = [sc.update(intent="book",
                       fields={"service": "ac_servicing", "date_start": "2026-11-11",
                               "date_end": "2026-11-11"},
                       evidence={"service": "AC servicing", "date_start": "asap",
                                 "date_end": "asap"},
                       from_memory=lambda v: {"area": v.memory_id("area")}),
             sc.search(), sc.present()]
    h = build(steps=steps, seed=True)
    h.say("Need AC servicing asap")
    t = h.session().task
    assert t.value("budget_max") == 1800 and t.fields["budget_max"].source.startswith("memory:")
    assert t.value("earliest_time") == "09:00"
    applied = h.of_type("constraints_from_memory")[0]["fields"]
    assert set(applied) >= {"budget_max", "earliest_time"}
    over = [c for c in t.candidates if c.price > 1800]
    assert over and all(not c.fits and any("over budget" in v for v in c.violations)
                        for c in over)
    assert all(s[11:16] >= "09:00" for c in t.candidates for s in c.fitting_slots)


def test_remembered_constraints_follow_a_change_of_service():
    # Found re-checking fix 1 live: moving from plumber to AC in one chat kept the plumber
    # budget (₹1,500) instead of the AC one (₹1,800).
    def update(service, quote):
        return sc.update(intent="book",
                         fields={"service": service, "date_start": "2026-11-11",
                                 "date_end": "2026-11-11"},
                         evidence={"service": quote, "date_start": "tomorrow",
                                   "date_end": "tomorrow"},
                         from_memory=lambda v: {"area": v.memory_id("area")})
    steps = [update("plumber", "plumber"), sc.search(), sc.present(),
             update("ac_servicing", "AC"), sc.search(), sc.present()]
    h = build(steps=steps, seed=True)
    h.say("plumber tomorrow")
    assert h.session().task.value("budget_max") == 1500
    h.say("Actually the AC, tomorrow")
    t = h.session().task
    assert t.value("service") == "ac_servicing" and t.value("budget_max") == 1800


def test_what_the_user_says_in_chat_beats_memory():
    steps = [sc.update(intent="book",
                       fields={"service": "plumber", "date_start": "2026-11-11",
                               "date_end": "2026-11-11", "budget_max": 3000},
                       evidence={"service": "plumber", "date_start": "tomorrow",
                                 "date_end": "tomorrow", "budget_max": "up to 3000"},
                       from_memory=lambda v: {"area": v.memory_id("area")}),
             sc.search(), sc.present()]
    h = build(steps=steps, seed=True)
    h.say("plumber tomorrow, I can pay up to 3000 this time")
    assert h.session().task.value("budget_max") == 3000


class CountingLLM:
    """Reports more input tokens than the estimate, like Gemini does."""
    model = "stub"

    def __init__(self, factor: float):
        self.factor = factor
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append(messages)
        est = sum(len(m.content) for m in messages) // 4 + 1300
        return LLMResponse(text="ok", model=self.model,
                           usage=Usage(input_tokens=int(est * self.factor), output_tokens=5))


def test_token_ratio_starts_conservative_for_gemini():
    assert default_token_ratio("gemini-3.6-flash") > 1.25
    assert default_token_ratio("scripted") == 1.0


def test_cap_is_calibrated_against_reported_tokens():
    # Live run: estimates were 6-26% under Gemini's own counts, so a 6,000 "cap" could really
    # be ~7,500. The builder caps estimate x ratio, and the ratio learns from real counts.
    h = build(seed=True)
    llm = CountingLLM(1.6)
    agent = ErrandAgent(llm, h.mock, h.store, h.clock, h.agent.config)
    agent.handle_message(h.session_id, "hello")
    assert agent.token_ratio > 1.5
    agent.handle_message(h.session_id, "hello again")
    ctx = [e for e in agent.tracer(h.session_id).events if e["type"] == "context_built"][-1]
    assert ctx["token_ratio"] > 1.5
    assert ctx["projected_tokens"] <= ctx["budget_tokens"]
    assert any(e["type"] == "token_ratio_calibrated" for e in agent.tracer(h.session_id).events)


def test_builder_respects_ratio_at_every_budget():
    h = build(seed=True)
    s = h.session()
    for budget in range(4500, 6001, 250):  # below ~4,000 the fixed parts alone don't fit
        b = ContextBuilder(h.store, h.agent.memory, h.agent.parts(h.session_id).gate, h.clock,
                           budget, 600, tool_tokens=1300, token_ratio=1.4)
        _, m = b.build(s, [])
        assert m["projected_tokens"] <= budget, (budget, m["projected_tokens"])
