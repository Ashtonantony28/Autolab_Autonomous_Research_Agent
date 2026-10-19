"""Design Agent: happy path, missing hypothesis, and the payload-field binding (SG12)."""

from __future__ import annotations

import pytest

from autolab.agents.common import design_claim, design_facts
from autolab.agents.design import (
    DesignAgent,
    build_design_prompt,
    build_design_trace_prompt,
)
from autolab.orchestrator.checkpoints import cost_trigger
from autolab.state.models import ExperimentDesign, ExperimentParameter, Hypothesis
from autolab.state.schema import empty_state
from autolab.verification.llm import FakeLLMClient
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import content, register_trace, register_trivial_trace
from tests.fixtures.golden_traces import GoldenStep

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
        conclusion=design_claim(DESIGN),
        facts=design_facts(DESIGN),
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
        conclusion=design_claim(design_for_newer),
        facts=design_facts(design_for_newer),
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
        conclusion=design_claim(stray),
        facts=design_facts(stray),
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
    assert last.endswith(f"exactly: {design_claim(DESIGN)}")


# SG12 (AUDIT-5 #2): cost, duration and numeric parameters are bound to the trace.

SWEEP = ExperimentDesign(
    hypothesis_id=HYPOTHESIS.id,
    protocol="incubate at 37 C",
    parameters=(
        ExperimentParameter(name="temperature", value=37, unit="C"),
        ExperimentParameter(name="offset", value=-0.5),
        ExperimentParameter(name="shaken", value=True),
        ExperimentParameter(name="buffer", value="PBS"),
    ),
    estimated_cost_usd=5.0,
    estimated_duration_minutes=30.0,
)


def test_design_facts_cover_cost_duration_and_numeric_parameters_only() -> None:
    assert [f.assertion for f in design_facts(SWEEP)] == [
        "(= estimated_cost_usd 5.0)",
        "(= estimated_duration_minutes 30.0)",
        "(= param_temperature 37.0)",
        "(= param_offset (- 0.5))",
    ]
    assert all(f.declarations.endswith(" Real)") for f in design_facts(SWEEP))


def test_design_claim_names_the_protocol_and_every_field() -> None:
    assert design_claim(SWEEP) == (
        "design protocol: incubate at 37 C; estimated_cost_usd = 5.0, "
        "estimated_duration_minutes = 30.0, param_temperature = 37.0 C, "
        "param_offset = -0.5, param_shaken = True, param_buffer = PBS"
    )


def _sweep_agent(fake: FakeLLMClient, *steps: GoldenStep) -> DesignAgent:
    fake.register("design.experiment", build_design_prompt(HYPOTHESIS), SWEEP.model_dump_json())
    register_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(HYPOTHESIS, SWEEP),
        steps=steps,
        facts=design_facts(SWEEP),
    )
    return DesignAgent(verifier=Verifier(fake), llm=fake, hitl_trigger=cost_trigger(10.0, 60.0))


async def test_design_trace_derived_from_the_code_built_facts_passes() -> None:
    fake = FakeLLMClient()
    agent = _sweep_agent(
        fake,
        GoldenStep(
            "A run under 10 USD and 60 minutes at 37 C is chosen",
            "premise",
            "(declare-const chosen Bool)",
            "(=> (and (< estimated_cost_usd 10.0) (< estimated_duration_minutes 60.0)"
            " (= param_temperature 37.0)) chosen)",
        ),
        GoldenStep(design_claim(SWEEP), "conclusion", "", "chosen", (0,)),
    )

    update = await agent({**empty_state("Q?"), "hypotheses": [HYPOTHESIS]})

    assert update["confidence"] == 1.0
    assert update["hitl_pending"] is None
    assert content(update["current_experiment"]) == content(SWEEP)


@pytest.mark.parametrize(
    "stated",
    [
        "(= estimated_cost_usd 500.0)",
        "(= estimated_duration_minutes 240.0)",
        "(= param_offset 2.0)",
    ],
)
async def test_design_trace_stating_other_values_is_premises_inconsistent(stated: str) -> None:
    fake = FakeLLMClient()
    agent = _sweep_agent(
        fake,
        GoldenStep("The run uses other values than the design states", "premise", "", stated),
        GoldenStep(
            "If so, the protocol is chosen",
            "premise",
            "(declare-const chosen Bool)",
            f"(=> {stated} chosen)",
        ),
        GoldenStep(design_claim(SWEEP), "conclusion", "", "chosen", (0, 1)),
    )

    update = await agent({**empty_state("Q?"), "hypotheses": [HYPOTHESIS]})

    [verdict] = update["verification_history"]
    assert verdict.step_verdicts[0].reason == "premises_inconsistent"
    assert update["hitl_pending"].payload["trigger"] == "verification"
    assert "current_experiment" not in update
