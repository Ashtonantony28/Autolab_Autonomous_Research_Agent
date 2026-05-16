"""Execute Agent — runs the current ExperimentDesign on the lab simulator."""

from __future__ import annotations

import json
from typing import Any

from autolab.agents.base import AgentOutput, BaseAgent
from autolab.state.models import (
    ExperimentDesign,
    ExperimentResult,
    ReasoningStep,
    ReasoningTrace,
)
from autolab.state.schema import AutoLabState
from autolab.tools.lab_hardware.simulator import LabSimulator
from autolab.verification.llm import LLMClient
from autolab.verification.vericot import Verifier


class ExecuteAgent(BaseAgent[ExperimentResult]):
    """Run the experiment via the simulator and emit an ExperimentResult.

    The reasoning trace narrates the choice to execute (rather than abort)
    and is what the verifier checks. The LabSimulator output itself isn't
    a reasoning claim — it's data.
    """

    name = "execute"

    def __init__(
        self,
        verifier: Verifier,
        llm: LLMClient,
        simulator: LabSimulator | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(verifier, **kwargs)
        self._llm = llm
        self._simulator = simulator or LabSimulator()

    async def _produce(self, state: AutoLabState) -> AgentOutput[ExperimentResult]:
        design = state.get("current_experiment")
        if design is None:
            raise ValueError("ExecuteAgent invoked without a current_experiment in state")
        result = self._simulator.run(design)

        trace_raw = await self._llm.call(
            "execute.trace", build_execute_trace_prompt(design, result)
        )
        trace = _parse_trace(self.name, trace_raw)
        return AgentOutput(payload=result, reasoning_trace=trace)

    def _merge_payload(self, payload: ExperimentResult) -> dict[str, Any]:
        return {"results": [payload], "current_experiment": None}


def build_execute_trace_prompt(design: ExperimentDesign, result: ExperimentResult) -> str:
    return (
        "Explain the reasoning for running this experiment and what the "
        'measurements show. Reply with JSON: {"steps": [...]}.\n\n'
        f"protocol: {design.protocol}\n"
        f"status: {result.status}"
    )


def _parse_trace(agent: str, raw: str) -> ReasoningTrace:
    data = json.loads(raw)
    steps = tuple(ReasoningStep.model_validate(s) for s in data["steps"])
    return ReasoningTrace(agent=agent, steps=steps)


__all__ = ["ExecuteAgent", "build_execute_trace_prompt"]
