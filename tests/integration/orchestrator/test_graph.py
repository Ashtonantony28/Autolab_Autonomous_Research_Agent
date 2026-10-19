"""End-to-end StateGraph runs."""

from __future__ import annotations

from uuid import uuid4

import pytest
from langgraph.types import Command

from autolab.agents.analysis import build_analysis_prompt, build_analysis_trace_prompt
from autolab.agents.common import measurement_facts, result_claim
from autolab.agents.design import build_design_prompt, build_design_trace_prompt
from autolab.agents.execute import build_execute_trace_prompt
from autolab.agents.research import build_hypothesis_prompt, build_trace_prompt
from autolab.orchestrator.graph import build_graph
from autolab.state.models import (
    AnalysisReport,
    Citation,
    ExperimentDesign,
    ExperimentResult,
    Hypothesis,
    Interpretation,
    Measurement,
)
from autolab.state.schema import empty_state
from autolab.tools.lab_hardware.simulator import LabSimulator
from autolab.verification.llm import FakeLLMClient
from tests.fixtures.agent_helpers import (
    register_failing_trace,
    register_trivial_trace,
)


class FixedResultSimulator(LabSimulator):
    """Simulator that returns a pre-built ExperimentResult (deterministic IDs)."""

    def __init__(self, result: ExperimentResult) -> None:
        super().__init__()
        self._result = result

    def run(self, design: ExperimentDesign) -> ExperimentResult:
        return self._result


QUESTION = "Why does X happen?"
HYPOTHESIS = Hypothesis(
    question=QUESTION,
    statement="X happens because Y",
    rationale="Z supports it",
    citations=(Citation(source="arxiv", identifier="42", title="paper"),),
)
DESIGN = ExperimentDesign(
    hypothesis_id=HYPOTHESIS.id,
    protocol="run a single measurement",
    estimated_cost_usd=1.0,
    estimated_duration_minutes=5.0,
)


def _seed_full_run(
    fake: FakeLLMClient,
) -> tuple[Hypothesis, ExperimentDesign, ExperimentResult, AnalysisReport]:
    """Register every LLM response needed for a clean four-agent run."""
    fake.register(
        "research.hypothesis", build_hypothesis_prompt(QUESTION), HYPOTHESIS.model_dump_json()
    )
    register_trivial_trace(
        fake,
        agent="research",
        trace_purpose="research.trace",
        trace_prompt=build_trace_prompt(QUESTION, HYPOTHESIS),
        conclusion=HYPOTHESIS.statement,
    )

    fake.register("design.experiment", build_design_prompt(HYPOTHESIS), DESIGN.model_dump_json())
    register_trivial_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(HYPOTHESIS, DESIGN),
        conclusion=DESIGN.protocol,
    )

    # Deterministic ExperimentResult so the analysis prompts align.
    result = ExperimentResult(
        design_id=DESIGN.id,
        status="success",
        measurements=(Measurement(name="m", value=1.0),),
        notes="stub",
    )
    register_trivial_trace(
        fake,
        agent="execute",
        trace_purpose="execute.trace",
        trace_prompt=build_execute_trace_prompt(DESIGN, result),
        conclusion=result_claim(result),
        facts=measurement_facts(result),
    )

    report = AnalysisReport(
        result_id=result.id,
        interpretations=(Interpretation(statement="x", rank=0, likelihood=0.9),),
        summary="ok",
    )
    fake.register(
        "analysis.report",
        build_analysis_prompt(result),
        report.model_dump_json(),
    )
    register_trivial_trace(
        fake,
        agent="analysis",
        trace_purpose="analysis.trace",
        trace_prompt=build_analysis_trace_prompt(result, report),
        conclusion=report.interpretations[0].statement,
        facts=measurement_facts(result),
    )
    return HYPOTHESIS, DESIGN, result, report


@pytest.mark.asyncio
async def test_full_run_reaches_end_with_all_agents_passing() -> None:
    fake = FakeLLMClient()
    _, _, result, _ = _seed_full_run(fake)
    graph = build_graph(llm=fake, simulator=FixedResultSimulator(result))
    config = {"configurable": {"thread_id": str(uuid4())}}

    final = await graph.ainvoke(empty_state(QUESTION), config=config)

    assert final["hypotheses"][0] == HYPOTHESIS
    assert final["results"][0].design_id == DESIGN.id
    assert len(final["reasoning_traces"]) == 4
    assert len(final["verification_history"]) == 4
    assert all(v.consistency_score >= 0.7 for v in final["verification_history"])
    assert final["hitl_pending"] is None


@pytest.mark.asyncio
async def test_run_pauses_at_hitl_when_design_verifier_fails() -> None:
    fake = FakeLLMClient()
    # Research passes normally.
    fake.register(
        "research.hypothesis", build_hypothesis_prompt(QUESTION), HYPOTHESIS.model_dump_json()
    )
    register_trivial_trace(
        fake,
        agent="research",
        trace_purpose="research.trace",
        trace_prompt=build_trace_prompt(QUESTION, HYPOTHESIS),
        conclusion=HYPOTHESIS.statement,
    )
    # Design produces a payload but its trace verifies INVALID -> HITL.
    fake.register("design.experiment", build_design_prompt(HYPOTHESIS), DESIGN.model_dump_json())
    register_failing_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(HYPOTHESIS, DESIGN),
        conclusion=DESIGN.protocol,
    )

    graph = build_graph(llm=fake)
    config = {"configurable": {"thread_id": str(uuid4())}}

    state = await graph.ainvoke(empty_state(QUESTION), config=config)

    # Graph paused at the HITL node — interrupt surfaces via the `__interrupt__` key
    # in the returned state and state has hitl_pending set.
    assert state.get("hitl_pending") is not None or "__interrupt__" in state

    # Resume the graph — the hitl node clears hitl_pending and routes to END.
    resumed = await graph.ainvoke(Command(resume={"action": "abort"}), config=config)
    assert resumed["hitl_pending"] is None
    assert resumed["current_agent"] == "hitl"


@pytest.mark.asyncio
async def test_compiled_graph_is_reusable_across_threads() -> None:
    fake = FakeLLMClient()
    _, _, result, _ = _seed_full_run(fake)
    graph = build_graph(llm=fake, simulator=FixedResultSimulator(result))

    cfg1 = {"configurable": {"thread_id": str(uuid4())}}
    cfg2 = {"configurable": {"thread_id": str(uuid4())}}

    f1 = await graph.ainvoke(empty_state(QUESTION), config=cfg1)
    f2 = await graph.ainvoke(empty_state(QUESTION), config=cfg2)

    assert f1["hypotheses"] == [HYPOTHESIS]
    assert f2["hypotheses"] == [HYPOTHESIS]
