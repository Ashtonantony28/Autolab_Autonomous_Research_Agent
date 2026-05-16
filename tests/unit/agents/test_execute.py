"""Execute Agent — runs the simulator and emits a result."""

from __future__ import annotations

import pytest

from autolab.agents.execute import ExecuteAgent, build_execute_trace_prompt
from autolab.state.models import ExperimentDesign, ExperimentParameter
from autolab.state.schema import empty_state
from autolab.tools.lab_hardware.simulator import LabSimulator
from autolab.verification.llm import FakeLLMClient
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import register_trivial_trace

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
