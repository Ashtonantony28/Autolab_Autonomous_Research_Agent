"""Three benchmark scenarios driving the full StateGraph end-to-end.

Each scenario constructs a fully-stubbed `FakeLLMClient` (so no network /
API key needed) and runs the graph against a `FixedResultSimulator` so
ids align. The point isn't real science — it's a reproducible end-to-end
exercise of the orchestration + verifier + agents.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TypedDict
from uuid import uuid4

from langgraph.types import Command

from autolab.agents.analysis import build_analysis_prompt, build_analysis_trace_prompt
from autolab.agents.common import design_claim, design_facts, measurement_facts, result_claim
from autolab.agents.design import build_design_prompt, build_design_trace_prompt
from autolab.agents.execute import build_execute_trace_prompt
from autolab.agents.research import build_hypothesis_prompt, build_trace_prompt
from autolab.orchestrator.graph import build_graph
from autolab.state.models import (
    AnalysisReport,
    Citation,
    ExperimentDesign,
    ExperimentParameter,
    ExperimentResult,
    HITLResolution,
    Hypothesis,
    Interpretation,
    Measurement,
)
from autolab.state.schema import empty_state
from autolab.tools.lab_hardware import LabSimulator
from autolab.verification.llm import FakeLLMClient
from tests.fixtures.agent_helpers import register_trivial_trace


class FixedResultSimulator(LabSimulator):
    def __init__(self, result: ExperimentResult) -> None:
        super().__init__()
        self._result = result

    def run(self, design: ExperimentDesign) -> ExperimentResult:
        return self._result


@dataclass
class Scenario:
    id: str
    question: str
    hypothesis: Hypothesis
    design: ExperimentDesign


def _scenarios() -> list[Scenario]:
    q1 = "What temperature optimizes protein crystallization for lysozyme?"
    h1 = Hypothesis(
        question=q1,
        statement="Lysozyme crystallizes best at 18C.",
        rationale="Prior solubility studies suggest a narrow window.",
        citations=(Citation(source="arxiv", identifier="1801.0001", title="Crystal sweep"),),
    )
    d1 = ExperimentDesign(
        hypothesis_id=h1.id,
        protocol="liquid handler crystal sweep",
        parameters=(
            ExperimentParameter(name="temperature_c", value=18.0, unit="C"),
            ExperimentParameter(name="noise_sigma", value=0.2),
        ),
        estimated_cost_usd=8.0,
        estimated_duration_minutes=120.0,
    )

    q2 = "Which ligand most strongly inhibits target enzyme?"
    h2 = Hypothesis(
        question=q2,
        statement="Ligand B is the strongest inhibitor.",
        rationale="Structural similarity to known inhibitors.",
        citations=(Citation(source="pubmed", identifier="35000001", title="Ligand survey"),),
    )
    d2 = ExperimentDesign(
        hypothesis_id=h2.id,
        protocol="plate reader IC50 screen",
        parameters=(
            ExperimentParameter(name="concentration_uM", value=10.0),
            ExperimentParameter(name="noise_sigma", value=0.1),
        ),
        estimated_cost_usd=15.0,
        estimated_duration_minutes=240.0,
    )

    q3 = "What annealing schedule yields the highest hardness for alloy X?"
    h3 = Hypothesis(
        question=q3,
        statement="Slow cooling at 1.5C/min maximizes hardness.",
        rationale="Grain growth thermodynamics.",
        citations=(Citation(source="manual", identifier="lab-notebook-1", title="prior runs"),),
    )
    d3 = ExperimentDesign(
        hypothesis_id=h3.id,
        protocol="imaging hardness analysis",
        parameters=(
            ExperimentParameter(name="cooling_rate_c_min", value=1.5),
            ExperimentParameter(name="noise_sigma", value=0.05),
        ),
        estimated_cost_usd=25.0,
        estimated_duration_minutes=480.0,
    )

    return [
        Scenario("protein_crystallization", q1, h1, d1),
        Scenario("compound_activity", q2, h2, d2),
        Scenario("materials_optimization", q3, h3, d3),
    ]


def _seed(fake: FakeLLMClient, sc: Scenario) -> ExperimentResult:
    fake.register(
        "research.hypothesis",
        build_hypothesis_prompt(sc.question),
        sc.hypothesis.model_dump_json(),
    )
    register_trivial_trace(
        fake,
        agent="research",
        trace_purpose="research.trace",
        trace_prompt=build_trace_prompt(sc.question, sc.hypothesis),
        conclusion=sc.hypothesis.statement,
    )

    fake.register(
        "design.experiment", build_design_prompt(sc.hypothesis), sc.design.model_dump_json()
    )
    register_trivial_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(sc.hypothesis, sc.design),
        conclusion=design_claim(sc.design),
        facts=design_facts(sc.design),
    )

    result = ExperimentResult(
        design_id=sc.design.id,
        status="success",
        measurements=(Measurement(name="m", value=1.0),),
        notes=f"benchmark {sc.id}",
    )
    register_trivial_trace(
        fake,
        agent="execute",
        trace_purpose="execute.trace",
        trace_prompt=build_execute_trace_prompt(sc.design, result),
        conclusion=result_claim(result),
        facts=measurement_facts(result),
    )

    report = AnalysisReport(
        result_id=result.id,
        interpretations=(Interpretation(statement="ok", rank=0, likelihood=0.9),),
        summary=f"{sc.id} ok",
    )
    fake.register("analysis.report", build_analysis_prompt(result), report.model_dump_json())
    register_trivial_trace(
        fake,
        agent="analysis",
        trace_purpose="analysis.trace",
        trace_prompt=build_analysis_trace_prompt(result, report),
        conclusion=report.interpretations[0].statement,
        facts=measurement_facts(result),
    )
    return result


class ScenarioRow(TypedDict):
    scenario: str
    confidence: float
    hypotheses: int
    results: int
    traces: int
    verifications: int
    hitl: list[str]


async def run_one(scenario: Scenario) -> ScenarioRow:
    """Run one scenario, approving every HITL pause (recorded in `hitl`)."""
    fake = FakeLLMClient()
    result = _seed(fake, scenario)
    graph = build_graph(llm=fake, simulator=FixedResultSimulator(result))
    config = {"configurable": {"thread_id": str(uuid4())}}
    final = await graph.ainvoke(empty_state(scenario.question), config=config)
    pauses: list[str] = []
    while "__interrupt__" in final:
        pauses.append(final["hitl_pending"].reason)
        final = await graph.ainvoke(Command(resume=HITLResolution(action="approve")), config=config)
    return ScenarioRow(
        scenario=scenario.id,
        confidence=final["confidence"],
        hypotheses=len(final["hypotheses"]),
        results=len(final["results"]),
        traces=len(final["reasoning_traces"]),
        verifications=len(final["verification_history"]),
        hitl=pauses,
    )


async def run_all() -> list[ScenarioRow]:
    return [await run_one(sc) for sc in _scenarios()]


def main() -> None:
    summary = asyncio.run(run_all())
    for row in summary:
        print(row)


if __name__ == "__main__":
    main()
