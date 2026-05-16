"""Research Agent — happy path + HITL fallback."""

from __future__ import annotations

from autolab.agents.research import (
    ResearchAgent,
    build_hypothesis_prompt,
    build_trace_prompt,
)
from autolab.state.models import Citation, Hypothesis
from autolab.state.schema import empty_state
from autolab.verification.llm import FakeLLMClient
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import register_failing_trace, register_trivial_trace

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
        fake, agent="research", trace_purpose="research.trace", trace_prompt=trace_prompt
    )

    agent = ResearchAgent(verifier=Verifier(fake), llm=fake)
    update = await agent(empty_state(QUESTION))

    assert update["current_agent"] == "research"
    assert update["confidence"] == 1.0
    assert update["hitl_pending"] is None
    assert update["hypotheses"] == [HYPOTHESIS]
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
