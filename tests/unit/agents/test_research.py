"""Research Agent — happy path + HITL fallback."""

from __future__ import annotations

import json

from autolab.agents.research import (
    ResearchAgent,
    build_hypothesis_prompt,
    build_trace_prompt,
)
from autolab.state.models import Citation, Hypothesis
from autolab.state.schema import empty_state
from autolab.verification.llm import FakeLLMClient
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import (
    content,
    register_failing_trace,
    register_trivial_trace,
)

QUESTION = "Why does crystallization rate drop above 40C?"
HYPOTHESIS = Hypothesis(
    question=QUESTION,
    statement="Above 40C, increased solubility outpaces nucleation rate.",
    rationale="Thermodynamic argument supported by prior work.",
    citations=(Citation(source="arxiv", identifier="2401.0001", title="Crystallization at T"),),
    confidence=0.7,
)


async def test_research_agent_emits_hypothesis_when_verifier_passes() -> None:
    fake = FakeLLMClient()
    fake.register(
        "research.hypothesis", build_hypothesis_prompt(QUESTION), HYPOTHESIS.model_dump_json()
    )
    trace_prompt = build_trace_prompt(QUESTION, HYPOTHESIS)
    register_trivial_trace(
        fake,
        agent="research",
        trace_purpose="research.trace",
        trace_prompt=trace_prompt,
        conclusion=HYPOTHESIS.statement,
    )

    agent = ResearchAgent(verifier=Verifier(fake), llm=fake)
    update = await agent(empty_state(QUESTION))

    assert update["current_agent"] == "research"
    assert update["confidence"] == 1.0
    assert update["hitl_pending"] is None
    assert content(*update["hypotheses"]) == content(HYPOTHESIS)
    assert len(update["reasoning_traces"]) == 1
    assert len(update["verification_history"]) == 1


async def test_research_agent_escalates_to_hitl_when_verifier_fails() -> None:
    fake = FakeLLMClient()
    fake.register(
        "research.hypothesis", build_hypothesis_prompt(QUESTION), HYPOTHESIS.model_dump_json()
    )
    trace_prompt = build_trace_prompt(QUESTION, HYPOTHESIS)
    register_failing_trace(
        fake, agent="research", trace_purpose="research.trace", trace_prompt=trace_prompt
    )

    agent = ResearchAgent(verifier=Verifier(fake), llm=fake)
    update = await agent(empty_state(QUESTION))

    assert update["hitl_pending"] is not None
    assert update["hitl_pending"].agent == "research"
    assert update["confidence"] < 0.7
    # Hypothesis is NOT merged into state when HITL fires.
    assert "hypotheses" not in update


def _fake_with_hypothesis() -> FakeLLMClient:
    fake = FakeLLMClient()
    fake.register(
        "research.hypothesis", build_hypothesis_prompt(QUESTION), HYPOTHESIS.model_dump_json()
    )
    return fake


async def test_research_agent_assigns_id_timestamp_and_question_in_code() -> None:
    reply = HYPOTHESIS.model_dump(mode="json") | {"question": "injected?"}
    fake = FakeLLMClient()
    fake.register("research.hypothesis", build_hypothesis_prompt(QUESTION), json.dumps(reply))
    register_trivial_trace(
        fake,
        agent="research",
        trace_purpose="research.trace",
        trace_prompt=build_trace_prompt(QUESTION, HYPOTHESIS),
        conclusion=HYPOTHESIS.statement,
    )

    (h,) = (await ResearchAgent(verifier=Verifier(fake), llm=fake)(empty_state(QUESTION)))[
        "hypotheses"
    ]

    assert h.id != HYPOTHESIS.id and h.created_at > HYPOTHESIS.created_at
    assert h.question == QUESTION


def test_research_trace_prompt_names_the_required_conclusion() -> None:
    assert HYPOTHESIS.statement in build_trace_prompt(QUESTION, HYPOTHESIS).splitlines()[-1]


async def test_research_agent_rejects_consistent_trace_with_unrelated_conclusion() -> None:
    fake = _fake_with_hypothesis()
    register_trivial_trace(
        fake,
        agent="research",
        trace_purpose="research.trace",
        trace_prompt=build_trace_prompt(QUESTION, HYPOTHESIS),
        conclusion="Cooling the solution speeds up nucleation.",
    )

    update = await ResearchAgent(verifier=Verifier(fake), llm=fake)(empty_state(QUESTION))

    assert update["hitl_pending"] is not None
    assert update["confidence"] == 0.0
    [verdict] = update["verification_history"]
    assert {(v.verdict, v.reason) for v in verdict.step_verdicts} == {("UNGROUNDED", "no_premises")}
    assert "hypotheses" not in update


async def test_research_agent_accepts_conclusion_differing_in_case_and_whitespace() -> None:
    fake = _fake_with_hypothesis()
    register_trivial_trace(
        fake,
        agent="research",
        trace_purpose="research.trace",
        trace_prompt=build_trace_prompt(QUESTION, HYPOTHESIS),
        conclusion="above 40C,   INCREASED solubility\noutpaces nucleation rate.",
    )

    update = await ResearchAgent(verifier=Verifier(fake), llm=fake)(empty_state(QUESTION))

    assert update["hitl_pending"] is None
    assert content(*update["hypotheses"]) == content(HYPOTHESIS)
