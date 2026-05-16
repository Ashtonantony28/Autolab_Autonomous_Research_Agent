"""Analysis Agent — interprets results into ranked hypotheses."""

from __future__ import annotations

from uuid import uuid4

import pytest

from autolab.agents.analysis import (
    AnalysisAgent,
    build_analysis_prompt,
    build_analysis_trace_prompt,
)
from autolab.state.models import (
    AnalysisReport,
    ExperimentResult,
    Interpretation,
    Measurement,
)
from autolab.state.schema import empty_state
from autolab.verification.llm import FakeLLMClient
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import register_trivial_trace

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
    )

    state = empty_state("Q?")
    state["results"] = [RESULT]
    agent = AnalysisAgent(verifier=Verifier(fake), llm=fake)
    update = await agent(state)
    assert update["hitl_pending"] is None
    assert update["confidence"] == 1.0


async def test_analysis_agent_rejects_result_id_mismatch() -> None:
    mismatched = AnalysisReport(
        result_id=uuid4(),  # not RESULT.id
        interpretations=(Interpretation(statement="x", rank=0, likelihood=0.5),),
        summary="s",
    )
    fake = FakeLLMClient()
    fake.register("analysis.report", build_analysis_prompt(RESULT), mismatched.model_dump_json())

    state = empty_state("Q?")
    state["results"] = [RESULT]
    agent = AnalysisAgent(verifier=Verifier(fake), llm=fake)
    with pytest.raises(ValueError, match="result_id"):
        await agent(state)


async def test_analysis_agent_requires_results() -> None:
    fake = FakeLLMClient()
    agent = AnalysisAgent(verifier=Verifier(fake), llm=fake)
    with pytest.raises(ValueError, match="without any results"):
        await agent(empty_state("Q?"))
