"""Analysis Agent — interprets results into ranked hypotheses."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from autolab.agents.analysis import (
    AnalysisAgent,
    build_analysis_prompt,
    build_analysis_trace_prompt,
    top_interpretation,
)
from autolab.agents.common import measurement_facts
from autolab.state.models import (
    AnalysisReport,
    ExperimentResult,
    Interpretation,
    Measurement,
)
from autolab.state.schema import empty_state
from autolab.verification.llm import FakeLLMClient
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import register_trace, register_trivial_trace
from tests.fixtures.golden_traces import GoldenStep

RESULT = ExperimentResult(
    design_id=uuid4(),
    status="success",
    measurements=(Measurement(name="m", value=1.0),),
)
REPORT = AnalysisReport(
    result_id=RESULT.id,
    interpretations=(
        Interpretation(statement="The yield is positive", rank=0, likelihood=0.8),
        Interpretation(statement="Measurement noise dominates", rank=1, likelihood=0.2),
    ),
    summary="primary signal looks real",
)


async def test_analysis_agent_emits_report() -> None:
    fake = FakeLLMClient()
    fake.register("analysis.report", build_analysis_prompt(RESULT), REPORT.model_dump_json())
    register_trivial_trace(
        fake,
        agent="analysis",
        trace_purpose="analysis.trace",
        trace_prompt=build_analysis_trace_prompt(RESULT, REPORT),
        conclusion="The yield is positive",
        facts=measurement_facts(RESULT),
    )

    state = empty_state("Q?")
    state["results"] = [RESULT]
    agent = AnalysisAgent(verifier=Verifier(fake), llm=fake)
    update = await agent(state)
    assert update["hitl_pending"] is None
    assert update["confidence"] == 1.0


async def test_analysis_agent_ignores_model_supplied_ids() -> None:
    mismatched = AnalysisReport(
        result_id=uuid4(),  # not RESULT.id
        interpretations=(Interpretation(statement="x", rank=0, likelihood=0.5),),
        summary="s",
    )
    fake = FakeLLMClient()
    fake.register("analysis.report", build_analysis_prompt(RESULT), mismatched.model_dump_json())
    register_trivial_trace(
        fake,
        agent="analysis",
        trace_purpose="analysis.trace",
        trace_prompt=build_analysis_trace_prompt(RESULT, mismatched),
        conclusion="x",
        facts=measurement_facts(RESULT),
    )
    state = empty_state("Q?")
    state["results"] = [RESULT]

    update = await AnalysisAgent(verifier=Verifier(fake), llm=fake)(state)

    (report,) = update["analysis_reports"]
    assert report.result_id == RESULT.id
    assert report.id != mismatched.id and report.created_at > mismatched.created_at
    assert str(RESULT.id) not in build_analysis_prompt(RESULT)


async def test_analysis_agent_requires_results() -> None:
    fake = FakeLLMClient()
    agent = AnalysisAgent(verifier=Verifier(fake), llm=fake)
    with pytest.raises(ValueError, match="without any results"):
        await agent(empty_state("Q?"))


async def _run_analysis(report: AnalysisReport, *steps: GoldenStep) -> dict[str, Any]:
    fake = FakeLLMClient()
    fake.register("analysis.report", build_analysis_prompt(RESULT), report.model_dump_json())
    register_trace(
        fake,
        agent="analysis",
        trace_purpose="analysis.trace",
        trace_prompt=build_analysis_trace_prompt(RESULT, report),
        steps=steps,
        facts=measurement_facts(RESULT),
    )
    state = empty_state("Q?")
    state["results"] = [RESULT]
    return await AnalysisAgent(verifier=Verifier(fake), llm=fake)(state)


async def test_analysis_agent_passes_top_interpretation_entailed_by_measurement() -> None:
    update = await _run_analysis(
        REPORT, GoldenStep("The yield is positive", "conclusion", "", "(> m 0.0)")
    )

    assert update["hitl_pending"] is None
    [verdict] = update["verification_history"]
    assert verdict.step_verdicts[0].reason == "entailed"


async def test_analysis_agent_rejects_trace_concluding_a_lower_ranked_interpretation() -> None:
    update = await _run_analysis(
        REPORT,
        GoldenStep("noise is large", "premise", "(declare-const noisy Bool)", "noisy"),
        GoldenStep("Measurement noise dominates", "conclusion", "", "noisy", (0,)),
    )

    assert update["hitl_pending"] is not None
    assert update["confidence"] == 0.0
    [verdict] = update["verification_history"]
    assert all(v.reason == "no_premises" for v in verdict.step_verdicts)


async def test_analysis_agent_marks_inference_contradicting_measurement_invalid() -> None:
    update = await _run_analysis(
        REPORT,
        GoldenStep("the reading was negative", "inference", "", "(< m 0.0)"),
        GoldenStep("The yield is positive", "conclusion", "", "(> m 0.0)"),
    )

    [verdict] = update["verification_history"]
    assert [(v.verdict, v.reason) for v in verdict.step_verdicts] == [
        ("INVALID", "not_entailed"),
        ("VALID", "entailed"),
    ]
    assert update["hitl_pending"] is not None


async def test_analysis_agent_binds_rank_zero_even_when_listed_second() -> None:
    report = REPORT.model_copy(update={"interpretations": REPORT.interpretations[::-1]})
    assert top_interpretation(report).statement == "The yield is positive"
    assert "exactly: The yield is positive" in build_analysis_trace_prompt(RESULT, report)

    update = await _run_analysis(
        report, GoldenStep("The yield is positive", "conclusion", "", "(> m 0.0)")
    )

    assert update["hitl_pending"] is None
