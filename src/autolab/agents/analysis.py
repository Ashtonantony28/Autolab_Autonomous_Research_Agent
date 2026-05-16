"""Analysis Agent — interprets an ExperimentResult into ranked hypotheses."""

from __future__ import annotations

import json
from typing import Any

from autolab.agents.base import AgentOutput, BaseAgent
from autolab.state.models import (
    AnalysisReport,
    ExperimentResult,
    ReasoningStep,
    ReasoningTrace,
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
        raw = await self._llm.call("analysis.report", build_analysis_prompt(latest))
        payload = AnalysisReport.model_validate_json(raw)
        if payload.result_id != latest.id:
            raise ValueError(
                f"AnalysisReport.result_id={payload.result_id} does not match "
                f"latest result id={latest.id}"
            )

        trace_raw = await self._llm.call(
            "analysis.trace", build_analysis_trace_prompt(latest, payload)
        )
        trace = _parse_trace(self.name, trace_raw)
        return AgentOutput(payload=payload, reasoning_trace=trace)

    def _merge_payload(self, payload: AnalysisReport) -> dict[str, Any]:
        return {}  # AnalysisReport is informational; not appended to state in Phase 3.


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
        f"top_interpretation: {report.interpretations[0].statement}"
    )


def _parse_trace(agent: str, raw: str) -> ReasoningTrace:
    data = json.loads(raw)
    steps = tuple(ReasoningStep.model_validate(s) for s in data["steps"])
    return ReasoningTrace(agent=agent, steps=steps)


__all__ = ["AnalysisAgent", "build_analysis_prompt", "build_analysis_trace_prompt"]
