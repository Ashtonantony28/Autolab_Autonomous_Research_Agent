"""Design Agent — happy path + missing-hypothesis error."""

from __future__ import annotations

import pytest

from autolab.agents.design import (
    DesignAgent,
    build_design_prompt,
    build_design_trace_prompt,
)
from autolab.state.models import ExperimentDesign, Hypothesis
from autolab.state.schema import empty_state
from autolab.verification.llm import FakeLLMClient
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import content, register_trivial_trace

HYPOTHESIS = Hypothesis(question="Q?", statement="S", rationale="R")
DESIGN = ExperimentDesign(
    hypothesis_id=HYPOTHESIS.id,
    protocol="run a sweep",
    estimated_cost_usd=10.0,
    estimated_duration_minutes=30.0,
)


async def test_design_agent_emits_experiment_design() -> None:
    fake = FakeLLMClient()
    fake.register("design.experiment", build_design_prompt(HYPOTHESIS), DESIGN.model_dump_json())
    register_trivial_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(HYPOTHESIS, DESIGN),
        conclusion=DESIGN.protocol,
    )

    state = empty_state("Q?")
    state["hypotheses"] = [HYPOTHESIS]

    agent = DesignAgent(verifier=Verifier(fake), llm=fake)
    update = await agent(state)

    design = update["current_experiment"]
    assert content(design) == content(DESIGN)
    assert design.hypothesis_id == HYPOTHESIS.id and design.id != DESIGN.id
    assert update["hitl_pending"] is None


async def test_design_agent_requires_hypothesis_in_state() -> None:
    fake = FakeLLMClient()
    agent = DesignAgent(verifier=Verifier(fake), llm=fake)
    with pytest.raises(ValueError, match="without any Hypothesis"):
        await agent(empty_state("Q?"))


async def test_design_agent_uses_latest_hypothesis() -> None:
    # Two hypotheses; agent should target the newest one.
    older = Hypothesis(question="Q?", statement="old", rationale="r")
    newer = Hypothesis(question="Q?", statement="new", rationale="r")
    design_for_newer = ExperimentDesign(
        hypothesis_id=newer.id,
        protocol="p",
        estimated_cost_usd=1.0,
        estimated_duration_minutes=1.0,
    )

    fake = FakeLLMClient()
    fake.register(
        "design.experiment", build_design_prompt(newer), design_for_newer.model_dump_json()
    )
    register_trivial_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(newer, design_for_newer),
        conclusion=design_for_newer.protocol,
    )

    state = empty_state("Q?")
    state["hypotheses"] = [older, newer]
    agent = DesignAgent(verifier=Verifier(fake), llm=fake)
    update = await agent(state)
    assert content(update["current_experiment"]) == content(design_for_newer)
    assert update["current_experiment"].hypothesis_id == newer.id


async def test_design_agent_rejects_trace_concluding_the_hypothesis_not_the_protocol() -> None:
    fake = FakeLLMClient()
    fake.register("design.experiment", build_design_prompt(HYPOTHESIS), DESIGN.model_dump_json())
    register_trivial_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(HYPOTHESIS, DESIGN),
        conclusion=HYPOTHESIS.statement,
    )
    state = empty_state("Q?")
    state["hypotheses"] = [HYPOTHESIS]

    update = await DesignAgent(verifier=Verifier(fake), llm=fake)(state)

    assert update["hitl_pending"] is not None
    assert update["confidence"] == 0.0
    [verdict] = update["verification_history"]
    assert all(v.reason == "no_premises" for v in verdict.step_verdicts)
    assert "current_experiment" not in update


async def test_design_agent_ignores_a_model_supplied_id_and_hypothesis_id() -> None:
    other = Hypothesis(question="Q?", statement="other", rationale="r")
    stray = DESIGN.model_copy(update={"hypothesis_id": other.id})
    fake = FakeLLMClient()
    fake.register("design.experiment", build_design_prompt(HYPOTHESIS), stray.model_dump_json())
    register_trivial_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(HYPOTHESIS, stray),
        conclusion=stray.protocol,
    )
    state = empty_state("Q?")
    state["hypotheses"] = [other, HYPOTHESIS]

    design = (await DesignAgent(verifier=Verifier(fake), llm=fake)(state))["current_experiment"]

    assert design.hypothesis_id == HYPOTHESIS.id
    assert design.id != stray.id


def test_design_prompt_carries_no_ids() -> None:
    assert str(HYPOTHESIS.id) not in build_design_prompt(HYPOTHESIS)


def test_design_trace_prompt_names_the_required_conclusion() -> None:
    last = build_design_trace_prompt(HYPOTHESIS, DESIGN).splitlines()[-1]
    assert last.endswith(f"exactly: {DESIGN.protocol}")
