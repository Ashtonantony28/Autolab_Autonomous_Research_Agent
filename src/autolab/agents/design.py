"""Design Agent — turns the latest Hypothesis into an ExperimentDesign."""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

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
from autolab.state.models import ExperimentDesign, HITLRequest, Hypothesis, VerificationContext
from autolab.state.schema import AutoLabState
from autolab.verification.llm import LLMClient
from autolab.verification.vericot import Verifier

ESTIMATE_DISAGREEMENT_RATIO: Final[float] = 3.0
"""A cost or duration further than this factor from the independent estimate is retried
with the gap as feedback, then escalated."""
ESTIMATE_FLOOR_USD: Final[float] = 1.0
ESTIMATE_FLOOR_MINUTES: Final[float] = 10.0
"""Figures below a floor compare as the floor: $0.10 against $0.50 is no disagreement."""


class DesignEstimate(BaseModel):
    """An independent cost and duration estimate of a design (SG25)."""

    model_config = ConfigDict(extra="forbid")

    estimated_cost_usd: float = Field(ge=0.0, allow_inf_nan=False)
    estimated_duration_minutes: float = Field(ge=0.0, allow_inf_nan=False)
    rationale: str


class DesignAgent(BaseAgent[ExperimentDesign]):
    """Pick a single experiment that would test the most recent Hypothesis.

    Cost, duration and numeric parameters become code-built `context.facts`, and
    the trace must conclude `design_claim(design)`, so the fields that drive the
    cost/duration pause and the simulator are verified, not trusted.

    A separate structured call (`design.estimate`) reads the protocol, parameters
    and success criteria, never the design's own figures, and returns its own cost
    and duration. The larger of each pair drives `hitl_trigger`, and a gap beyond
    `ESTIMATE_DISAGREEMENT_RATIO` withholds the design. So does a figure stated
    outright in its text (`design_figures`). Both requests carry their reason in
    `payload["feedback"]`, so the monitor retries the agent with it first.
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
        """Run the base template, then check the figures against the text and an estimate."""
        update = await super().__call__(state)
        design = update.get("current_experiment") or (update.get("hitl_payload") or {}).get(
            "current_experiment"
        )
        if design is None:
            return update
        reason = _stated_figures(design)
        if reason is None:
            estimate = await self._llm.call_structured(
                "design.estimate", build_estimate_prompt(design), DesignEstimate
            )
            reason = _disagreement(design, estimate)
            if reason is None:
                return await self._apply_policy(update, design, estimate, state)
        summary = update["verification_history"][-1].summary
        pending = self._request("verification", reason, summary, design)
        pending.payload["feedback"] = reason
        return self._withhold(update, design, pending)

    async def _apply_policy(
        self,
        update: dict[str, Any],
        design: ExperimentDesign,
        estimate: DesignEstimate,
        state: AutoLabState,
    ) -> dict[str, Any]:
        """Run `hitl_trigger` on the larger figures, where the estimate raised one.

        Otherwise the base template already ran it on these figures, or an
        `approve` of the blocked trace will (`checkpoints.make_hitl_node`).
        """
        pending: HITLRequest | None = update.get("hitl_pending")
        raised = (
            estimate.estimated_cost_usd > design.estimated_cost_usd
            or estimate.estimated_duration_minutes > design.estimated_duration_minutes
        )
        if self.hitl_trigger is None or not raised:
            return update
        if pending is not None and pending.payload["trigger"] == "policy":
            return update
        policy = await self.hitl_trigger(_at_least(design, estimate), state)
        if policy is None:
            return update
        policy = f"independent estimate: {policy}"
        if pending is not None:
            # A blocked trace: whoever approves it must see the estimate's policy too.
            pending.reason = f"{pending.reason}; {policy}"
            return update
        summary = update["verification_history"][-1].summary
        return self._withhold(update, design, self._request("policy", policy, summary, design))

    def _withhold(
        self, update: dict[str, Any], design: ExperimentDesign, pending: HITLRequest
    ) -> dict[str, Any]:
        rest = {k: v for k, v in update.items() if k != "current_experiment"}
        return {**rest, "hitl_pending": pending, "hitl_payload": self._merge_payload(design)}

    def _merge_payload(self, payload: ExperimentDesign) -> dict[str, Any]:
        return {"current_experiment": payload}


def _stated_figures(design: ExperimentDesign) -> str | None:
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
        return None
    return (
        f"{'; '.join(stated)}; these belong only in estimated_cost_usd / estimated_duration_minutes"
    )


def _at_least(design: ExperimentDesign, estimate: DesignEstimate) -> ExperimentDesign:
    """`design` with each figure raised to the estimate's, for the cost/duration trigger."""
    return design.model_copy(
        update={
            "estimated_cost_usd": max(design.estimated_cost_usd, estimate.estimated_cost_usd),
            "estimated_duration_minutes": max(
                design.estimated_duration_minutes, estimate.estimated_duration_minutes
            ),
        }
    )


def _apart(stated: float, estimated: float, floor: float) -> bool:
    low, high = sorted((max(stated, floor), max(estimated, floor)))
    return high > ESTIMATE_DISAGREEMENT_RATIO * low


def _disagreement(design: ExperimentDesign, estimate: DesignEstimate) -> str | None:
    """Why the design's figures and the independent estimate are too far apart, or None."""
    pairs = (
        ("estimated_cost_usd", design.estimated_cost_usd, estimate.estimated_cost_usd),
        (
            "estimated_duration_minutes",
            design.estimated_duration_minutes,
            estimate.estimated_duration_minutes,
        ),
    )
    floors = (ESTIMATE_FLOOR_USD, ESTIMATE_FLOOR_MINUTES)
    gaps = [
        f"{field} = {stated:g} but an independent estimate gives {estimated:g}"
        for (field, stated, estimated), floor in zip(pairs, floors, strict=True)
        if _apart(stated, estimated, floor)
    ]
    if not gaps:
        return None
    return (
        f"{'; '.join(gaps)} (more than {ESTIMATE_DISAGREEMENT_RATIO:g}x apart; "
        f"the estimate's basis: {estimate.rationale}); "
        "state the whole run's cost and duration in those fields"
    )


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


def build_estimate_prompt(design: ExperimentDesign) -> str:
    """The independent estimate's prompt: the design without its own cost and duration."""
    parameters = "\n".join(
        f"- {p.name} = {p.value}" + (f" {p.unit}" if p.unit else "") for p in design.parameters
    )
    criteria = "\n".join(f"- {c}" for c in design.success_criteria)
    return (
        "Estimate independently what running this experiment costs in USD (reagents, "
        "consumables, instrument time) and how long it takes in minutes, from setup to the "
        "last readout, including every incubation, culture and wait. A cost or duration may "
        "be stated anywhere below, in any unit or spelling, or only implied by the steps; "
        "count all of it.\n\n"
        f"protocol: {design.protocol}\n"
        f"parameters:\n{parameters or '- none'}\n"
        f"success_criteria:\n{criteria or '- none'}"
    )


def build_design_trace_prompt(hypothesis: Hypothesis, design: ExperimentDesign) -> str:
    return (
        "Explain the reasoning that led to this experiment design. "
        'Reply with JSON: {"steps": [{index, claim, claim_type, referenced_step_indices}]}.\n\n'
        f"hypothesis: {hypothesis.statement}\n"
        f"protocol: {design.protocol}\n"
        f"{conclusion_instruction(design_claim(design))}"
    )


__all__ = [
    "ESTIMATE_DISAGREEMENT_RATIO",
    "DesignAgent",
    "DesignEstimate",
    "build_design_prompt",
    "build_design_trace_prompt",
    "build_estimate_prompt",
]
