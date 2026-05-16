"""Design Agent — turns the latest Hypothesis into an ExperimentDesign."""

from __future__ import annotations

import json
from typing import Any

from autolab.agents.base import AgentOutput, BaseAgent
from autolab.state.models import (
    ExperimentDesign,
    Hypothesis,
    ReasoningStep,
    ReasoningTrace,
)
from autolab.state.schema import AutoLabState
from autolab.verification.llm import LLMClient
from autolab.verification.vericot import Verifier


class DesignAgent(BaseAgent[ExperimentDesign]):
    """Pick a single experiment that would test the most recent Hypothesis."""

    name = "design"

    def __init__(self, verifier: Verifier, llm: LLMClient, **kwargs: Any) -> None:
        super().__init__(verifier, **kwargs)
        self._llm = llm

    async def _produce(self, state: AutoLabState) -> AgentOutput[ExperimentDesign]:
        hypotheses = state.get("hypotheses", [])
        if not hypotheses:
            raise ValueError("DesignAgent invoked without any Hypothesis in state")
        target = hypotheses[-1]
        raw = await self._llm.call("design.experiment", build_design_prompt(target))
        payload = ExperimentDesign.model_validate_json(raw)

        trace_raw = await self._llm.call("design.trace", build_design_trace_prompt(target, payload))
        trace = _parse_trace(self.name, trace_raw)
        return AgentOutput(payload=payload, reasoning_trace=trace)

    def _merge_payload(self, payload: ExperimentDesign) -> dict[str, Any]:
        return {"current_experiment": payload}


def build_design_prompt(hypothesis: Hypothesis) -> str:
    return (
        "Design a single experiment to test the following Hypothesis. "
        "Reply with the JSON of an autolab.state.models.ExperimentDesign whose "
        "hypothesis_id matches the input.\n\n"
        f"hypothesis_id: {hypothesis.id}\n"
        f"hypothesis_statement: {hypothesis.statement}"
    )


def build_design_trace_prompt(hypothesis: Hypothesis, design: ExperimentDesign) -> str:
    return (
        "Explain the reasoning that led to this experiment design. "
        'Reply with JSON: {"steps": [{index, claim, claim_type, referenced_step_indices}]}.\n\n'
        f"hypothesis: {hypothesis.statement}\n"
        f"protocol: {design.protocol}"
    )


def _parse_trace(agent: str, raw: str) -> ReasoningTrace:
    data = json.loads(raw)
    steps = tuple(ReasoningStep.model_validate(s) for s in data["steps"])
    return ReasoningTrace(agent=agent, steps=steps)


__all__ = ["DesignAgent", "build_design_prompt", "build_design_trace_prompt"]
