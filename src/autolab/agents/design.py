"""Design Agent — turns the latest Hypothesis into an ExperimentDesign."""

from __future__ import annotations

from typing import Any

from autolab.agents.base import AgentOutput, BaseAgent
from autolab.agents.common import (
    DESIGN_REPLY,
    conclusion_instruction,
    design_claim,
    design_facts,
    design_figures,
    parse_trace,
    with_feedback,
)
from autolab.state.models import ExperimentDesign, Hypothesis, VerificationContext
from autolab.state.schema import AutoLabState
from autolab.verification.llm import LLMClient
from autolab.verification.vericot import Verifier


class DesignAgent(BaseAgent[ExperimentDesign]):
    """Pick a single experiment that would test the most recent Hypothesis.

    Cost, duration and numeric parameters become code-built `context.facts`, and
    the trace must conclude `design_claim(design)`, so the fields that drive the
    cost/duration pause and the simulator are verified, not trusted. A design
    whose protocol or parameter text states a cost or duration (`design_figures`)
    is never admitted: those figures could disagree with the bound fields unseen.
    """

    name = "design"

    def __init__(self, verifier: Verifier, llm: LLMClient, **kwargs: Any) -> None:
        super().__init__(verifier, **kwargs)
        self._llm = llm

    async def _produce(self, state: AutoLabState) -> AgentOutput[ExperimentDesign]:
        hypotheses = state.get("hypotheses", [])
        if not hypotheses:
            raise ValueError("DesignAgent invoked without any Hypothesis in state")
        target = hypotheses[-1]
        feedback = state.get("feedback", "")
        reply = await self._llm.call_structured(
            "design.experiment", with_feedback(build_design_prompt(target), feedback), DESIGN_REPLY
        )
        payload = ExperimentDesign(**dict(reply), hypothesis_id=target.id)

        trace_raw = await self._llm.call(
            "design.trace", with_feedback(build_design_trace_prompt(target, payload), feedback)
        )
        trace = parse_trace(self.name, trace_raw)
        context = VerificationContext(
            facts=design_facts(payload), payload_claim=design_claim(payload)
        )
        return AgentOutput(payload=payload, reasoning_trace=trace, context=context)

    async def __call__(self, state: AutoLabState) -> dict[str, Any]:
        """Run the base template, then reject a design whose text states cost or duration."""
        update = await super().__call__(state)
        design = update.get("current_experiment") or (update.get("hitl_payload") or {}).get(
            "current_experiment"
        )
        if design is None:
            return update
        in_protocol, in_parameters = design_figures(design)
        stated = [
            f"{where} cost or duration figures {list(figures)}"
            for where, figures in (
                ("protocol states", in_protocol),
                ("parameters state", in_parameters),
            )
            if figures
        ]
        if not stated:
            return update
        reason = (
            f"{'; '.join(stated)}; "
            "these belong only in estimated_cost_usd / estimated_duration_minutes"
        )
        pending = self._request(
            "verification", reason, update["verification_history"][-1].summary, design
        )
        rest = {k: v for k, v in update.items() if k != "current_experiment"}
        return {**rest, "hitl_pending": pending, "hitl_payload": self._merge_payload(design)}

    def _merge_payload(self, payload: ExperimentDesign) -> dict[str, Any]:
        return {"current_experiment": payload}


PROTOCOL_FIGURES_RULE = (
    "Do not state any cost (currency amount) or duration (time) figure in the protocol text "
    "or in any parameter's name, text value or unit; "
    "put them only in estimated_cost_usd and estimated_duration_minutes."
)


def build_design_prompt(hypothesis: Hypothesis) -> str:
    return (
        "Design a single experiment to test the following Hypothesis.\n"
        f"{PROTOCOL_FIGURES_RULE}\n\n"
        f"hypothesis_statement: {hypothesis.statement}"
    )


def build_design_trace_prompt(hypothesis: Hypothesis, design: ExperimentDesign) -> str:
    return (
        "Explain the reasoning that led to this experiment design. "
        'Reply with JSON: {"steps": [{index, claim, claim_type, referenced_step_indices}]}.\n\n'
        f"hypothesis: {hypothesis.statement}\n"
        f"protocol: {design.protocol}\n"
        f"{conclusion_instruction(design_claim(design))}"
    )


__all__ = ["DesignAgent", "build_design_prompt", "build_design_trace_prompt"]
