"""Research Agent — turns a research question into a grounded Hypothesis."""

from __future__ import annotations

from typing import Any

from autolab.agents.base import AgentOutput, BaseAgent
from autolab.agents.common import conclusion_instruction, parse_trace, with_feedback
from autolab.state.models import Hypothesis, VerificationContext
from autolab.state.schema import AutoLabState
from autolab.verification.llm import LLMClient
from autolab.verification.vericot import Verifier


class ResearchAgent(BaseAgent[Hypothesis]):
    """Produces a Hypothesis for the current research_question.

    The LLM is asked twice:
      1. `research.hypothesis` — for the Hypothesis JSON.
      2. `research.trace` — for the chain-of-thought steps that explain it.

    The second call is verified by VeriCoT; the agent only emits the
    hypothesis if verification clears the HITL threshold.
    """

    name = "research"

    def __init__(self, verifier: Verifier, llm: LLMClient, **kwargs: Any) -> None:
        super().__init__(verifier, **kwargs)
        self._llm = llm

    async def _produce(self, state: AutoLabState) -> AgentOutput[Hypothesis]:
        question = state.get("research_question", "")
        feedback = state.get("feedback", "")
        raw = await self._llm.call(
            "research.hypothesis", with_feedback(build_hypothesis_prompt(question), feedback)
        )
        payload = Hypothesis.model_validate_json(raw)

        trace_raw = await self._llm.call(
            "research.trace", with_feedback(build_trace_prompt(question, payload), feedback)
        )
        trace = parse_trace(self.name, trace_raw)
        context = VerificationContext(payload_claim=payload.statement)
        return AgentOutput(payload=payload, reasoning_trace=trace, context=context)

    def _merge_payload(self, payload: Hypothesis) -> dict[str, Any]:
        return {"hypotheses": [payload]}


def build_hypothesis_prompt(question: str) -> str:
    return (
        "Propose a single testable Hypothesis for the following research question. "
        "Reply with the JSON of an autolab.state.models.Hypothesis. "
        "Include at least one Citation.\n\n"
        f"research_question: {question}"
    )


def build_trace_prompt(question: str, hypothesis: Hypothesis) -> str:
    return (
        "Explain the reasoning that led from the research question to the hypothesis. "
        'Reply with JSON: {"steps": [{index, claim, claim_type, referenced_step_indices}]}.\n\n'
        f"research_question: {question}\nhypothesis_statement: {hypothesis.statement}\n"
        f"{conclusion_instruction(hypothesis.statement)}"
    )


__all__ = ["ResearchAgent", "build_hypothesis_prompt", "build_trace_prompt"]
