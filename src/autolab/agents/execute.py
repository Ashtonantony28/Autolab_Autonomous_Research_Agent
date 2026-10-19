"""Execute Agent — runs the current ExperimentDesign on the lab simulator."""

from __future__ import annotations

from typing import Any

from autolab.agents.base import AgentOutput, BaseAgent
from autolab.agents.common import (
    conclusion_instruction,
    measurement_facts,
    parse_trace,
    result_claim,
)
from autolab.state.models import ExperimentDesign, ExperimentResult, VerificationContext
from autolab.state.schema import AutoLabState
from autolab.tools.lab_hardware.simulator import LabSimulator
from autolab.verification.llm import LLMClient
from autolab.verification.vericot import Verifier


class ExecuteAgent(BaseAgent[ExperimentResult]):
    """Run the experiment via the simulator and emit an ExperimentResult.

    The simulator's measurements become code-built `context.facts`, and the
    trace must conclude `result_claim(result)`, so the LLM's narration is
    checked against the data rather than trusted.
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
        trace = parse_trace(self.name, trace_raw)
        context = VerificationContext(
            facts=measurement_facts(result), payload_claim=result_claim(result)
        )
        return AgentOutput(payload=result, reasoning_trace=trace, context=context)

    def _merge_payload(self, payload: ExperimentResult) -> dict[str, Any]:
        return {"results": [payload], "current_experiment": None}


def build_execute_trace_prompt(design: ExperimentDesign, result: ExperimentResult) -> str:
    return (
        "Explain the reasoning for running this experiment and what the "
        'measurements show. Reply with JSON: {"steps": [...]}.\n\n'
        f"protocol: {design.protocol}\n"
        f"status: {result.status}\n"
        f"{conclusion_instruction(result_claim(result))}"
    )


__all__ = ["ExecuteAgent", "build_execute_trace_prompt"]
