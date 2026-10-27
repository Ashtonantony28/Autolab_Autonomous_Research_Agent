"""Execute Agent — runs the simulator and emits a result."""

from __future__ import annotations

from typing import Any

import pytest

from autolab.agents.common import measurement_facts, result_claim
from autolab.agents.execute import ExecuteAgent, build_execute_trace_prompt
from autolab.state.models import ExperimentDesign, ExperimentParameter, FormalizedStep
from autolab.state.schema import empty_state
from autolab.tools.lab_hardware.simulator import LabSimulator
from autolab.verification.llm import FakeLLMClient
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import register_trace, register_trivial_trace
from tests.fixtures.golden_traces import GoldenStep

DESIGN = ExperimentDesign(
    hypothesis_id=__import__("uuid").uuid4(),
    protocol="sweep T",
    parameters=(
        ExperimentParameter(name="t1", value=10.0),
        ExperimentParameter(name="t2", value=20.0),
    ),
    estimated_cost_usd=5.0,
    estimated_duration_minutes=15.0,
)


async def test_execute_agent_runs_simulator_and_emits_result() -> None:
    sim = LabSimulator()
    expected_result = sim.run(DESIGN)

    fake = FakeLLMClient()
    register_trivial_trace(
        fake,
        agent="execute",
        trace_purpose="execute.trace",
        trace_prompt=build_execute_trace_prompt(DESIGN, expected_result),
        conclusion=result_claim(expected_result),
        facts=measurement_facts(expected_result),
    )

    state = empty_state("Q?")
    state["current_experiment"] = DESIGN

    agent = ExecuteAgent(verifier=Verifier(fake), llm=fake, simulator=sim)
    update = await agent(state)

    assert len(update["results"]) == 1
    assert update["results"][0].design_id == DESIGN.id
    assert update["current_experiment"] is None
    assert update["hitl_pending"] is None


async def test_execute_agent_requires_current_experiment() -> None:
    fake = FakeLLMClient()
    agent = ExecuteAgent(verifier=Verifier(fake), llm=fake)
    with pytest.raises(ValueError, match="current_experiment"):
        await agent(empty_state("Q?"))


def test_lab_simulator_handles_designs_without_numeric_parameters() -> None:
    design = ExperimentDesign(
        hypothesis_id=__import__("uuid").uuid4(),
        protocol="manual",
        parameters=(ExperimentParameter(name="note", value="manual"),),
        estimated_cost_usd=0.0,
        estimated_duration_minutes=0.0,
    )
    result = LabSimulator().run(design)
    assert result.status == "success"
    assert result.measurements[0].value == 0.0


SIM_RESULT = LabSimulator().run(DESIGN)  # synthetic_yield = mean(10, 20) = 15.0
FACTS = measurement_facts(SIM_RESULT)
CLAIM = result_claim(SIM_RESULT)


async def _run_execute(*steps: GoldenStep) -> dict[str, Any]:
    fake = FakeLLMClient()
    register_trace(
        fake,
        agent="execute",
        trace_purpose="execute.trace",
        trace_prompt=build_execute_trace_prompt(DESIGN, SIM_RESULT),
        steps=steps,
        facts=FACTS,
    )
    state = empty_state("Q?")
    state["current_experiment"] = DESIGN
    agent = ExecuteAgent(verifier=Verifier(fake), llm=fake, simulator=LabSimulator())
    return await agent(state)


def test_execute_binds_to_simulator_measurements() -> None:
    assert CLAIM == "experiment success: synthetic_yield = 15.0"
    expected = FormalizedStep(
        declarations="(declare-const synthetic_yield Real)",
        assertion="(= synthetic_yield 15.0)",
    )
    assert (expected,) == FACTS
    assert CLAIM in build_execute_trace_prompt(DESIGN, SIM_RESULT)


async def test_execute_agent_passes_trace_concluding_what_the_data_entails() -> None:
    update = await _run_execute(GoldenStep(CLAIM, "conclusion", "", "(= synthetic_yield 15.0)"))

    assert update["hitl_pending"] is None
    [verdict] = update["verification_history"]
    assert [(v.verdict, v.reason) for v in verdict.step_verdicts] == [("VALID", "entailed")]
    assert update["results"][0].measurements[0].value == 15.0


async def test_execute_agent_rejects_consistent_trace_with_unrelated_conclusion() -> None:
    update = await _run_execute(
        GoldenStep("the run finished", "premise", "(declare-const done Bool)", "done"),
        GoldenStep("the run finished", "conclusion", "", "done", (0,)),
    )

    assert update["hitl_pending"] is not None
    assert update["confidence"] == 0.0
    assert "results" not in update


async def test_execute_agent_marks_inference_contradicting_measurement_invalid() -> None:
    update = await _run_execute(
        GoldenStep("the sweep ran", "premise", "(declare-const ran Bool)", "ran"),
        GoldenStep("the yield was 99", "inference", "", "(= synthetic_yield 99.0)", (0,)),
        GoldenStep(CLAIM, "conclusion", "", "(= synthetic_yield 15.0)", (0,)),
    )

    [verdict] = update["verification_history"]
    assert [(v.verdict, v.reason) for v in verdict.step_verdicts] == [
        ("VALID", "assumed"),
        ("INVALID", "not_entailed"),
        ("VALID", "entailed"),
    ]
    assert update["hitl_pending"] is not None
    assert "results" not in update


async def test_execute_agent_marks_premise_contradicting_measurement_inconsistent() -> None:
    update = await _run_execute(
        GoldenStep("the yield was 99", "premise", "", "(= synthetic_yield 99.0)"),
        GoldenStep(CLAIM, "conclusion", "", "(= synthetic_yield 15.0)", (0,)),
    )

    [verdict] = update["verification_history"]
    # The premise itself contradicts the measurement fact (AUDIT-1 #4).
    assert verdict.step_verdicts[0].verdict == "INVALID"
    assert verdict.step_verdicts[0].reason == "premises_inconsistent"
    assert update["hitl_pending"] is not None


async def test_execute_agent_marks_conclusion_misreporting_measurement_invalid() -> None:
    update = await _run_execute(GoldenStep(CLAIM, "conclusion", "", "(= synthetic_yield 16.0)"))

    [verdict] = update["verification_history"]
    assert verdict.step_verdicts[0].verdict == "INVALID"
    assert update["hitl_pending"] is not None
