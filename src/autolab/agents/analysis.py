"""Analysis Agent — interprets an ExperimentResult into ranked hypotheses."""

from __future__ import annotations

from typing import Any

from autolab.agents.base import AgentOutput, BaseAgent
from autolab.agents.common import (
    conclusion_instruction,
    measurement_facts,
    parse_trace,
    result_claim,
    with_feedback,
)
from autolab.state.models import (
    AnalysisReport,
    ExperimentResult,
    Interpretation,
    VerificationContext,
)
from autolab.state.schema import AutoLabState
from autolab.verification.llm import LLMClient
from autolab.verification.vericot import Verifier


class AnalysisAgent(BaseAgent[AnalysisReport]):
    """Produces a ranked list of interpretations for the latest result."""

    name = "analysis"

    def __init__(self, verifier: Verifier, llm: LLMClient, **kwargs: Any) -> None:
        super().__init__(verifier, **kwargs)
        self._llm = llm

    async def _produce(self, state: AutoLabState) -> AgentOutput[AnalysisReport]:
        results = state.get("results", [])
        if not results:
            raise ValueError("AnalysisAgent invoked without any results in state")
        latest = results[-1]
        feedback = state.get("feedback", "")
        raw = await self._llm.call(
            "analysis.report", with_feedback(build_analysis_prompt(latest), feedback)
        )
        payload = AnalysisReport.model_validate_json(raw)
        if payload.result_id != latest.id:
            raise ValueError(
                f"AnalysisReport.result_id={payload.result_id} does not match "
                f"latest result id={latest.id}"
            )

        trace_raw = await self._llm.call(
            "analysis.trace",
            with_feedback(build_analysis_trace_prompt(latest, payload), feedback),
        )
        trace = parse_trace(self.name, trace_raw)
        context = VerificationContext(
            facts=measurement_facts(latest), payload_claim=top_interpretation(payload).statement
        )
        return AgentOutput(payload=payload, reasoning_trace=trace, context=context)

    def _merge_payload(self, payload: AnalysisReport) -> dict[str, Any]:
        return {"analysis_reports": [payload]}


def build_analysis_prompt(result: ExperimentResult) -> str:
    return (
        "Produce a ranked AnalysisReport for the following ExperimentResult. "
        "Reply with the JSON of an autolab.state.models.AnalysisReport whose "
        "result_id matches the input.\n\n"
        f"result_id: {result.id}\n"
        f"status: {result.status}\n"
        f"measurement_summary: {len(result.measurements)} measurements"
    )


def build_analysis_trace_prompt(result: ExperimentResult, report: AnalysisReport) -> str:
    return (
        "Explain the reasoning for the ranked interpretations. "
        'Reply with JSON: {"steps": [...]}.\n\n'
        f"result_id: {result.id}\n"
        f"measurements: {result_claim(result)}\n"
        f"{conclusion_instruction(top_interpretation(report).statement)}"
    )


def top_interpretation(report: AnalysisReport) -> Interpretation:
    """The rank-0 interpretation, which the trace must conclude."""
    return min(report.interpretations, key=lambda i: i.rank)


__all__ = [
    "AnalysisAgent",
    "build_analysis_prompt",
    "build_analysis_trace_prompt",
    "top_interpretation",
]
