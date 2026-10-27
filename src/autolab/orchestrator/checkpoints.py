"""HITL pause node, HITL policy triggers and checkpoint plumbing.

The HITL node uses LangGraph's `interrupt()` to suspend the graph and
return control to the caller, who inspects `hitl_pending` and resumes with
`Command(resume=HITLResolution(...))` (or the equivalent dict):

  - `approve` merges the withheld `hitl_payload` and continues to the next agent.
  - `retry` re-runs the agent, with the human's note (or the request's
    reason) as `feedback`.
  - `abort` ends the run.

LangGraph ships its own `MemorySaver` for in-process runs — we use that
here and keep our `Checkpointer` protocol (Phase 1, ADR-0004) as the
*external* checkpoint for state we want to persist across processes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END
from langgraph.types import Command, interrupt

from autolab.agents.base import HITLTrigger
from autolab.orchestrator.routing import NEXT_NODE
from autolab.state.models import (
    AnalysisReport,
    ExperimentDesign,
    ExperimentResult,
    HITLRequest,
    HITLResolution,
    Hypothesis,
    ReasoningStep,
    ReasoningTrace,
    VerificationResult,
)
from autolab.state.schema import AutoLabState
from autolab.verification.vericot import Verifier

CHECKPOINT_TYPES: Final[tuple[type, ...]] = (
    AnalysisReport,
    ExperimentDesign,
    ExperimentResult,
    HITLRequest,
    HITLResolution,
    Hypothesis,
    ReasoningTrace,
    VerificationResult,
)
"""State models the checkpointer may deserialize (LangGraph's msgpack allowlist)."""


def hitl_node(state: AutoLabState) -> Command[Any]:
    """LangGraph node: suspend for a human and route on their `HITLResolution`."""
    pending = state.get("hitl_pending")
    agent = pending.agent if pending else ""
    resolution = HITLResolution.model_validate(
        interrupt(
            {
                "agent": agent,
                "reason": pending.reason if pending else "",
                "payload": dict(pending.payload) if pending else {},
            }
        )
    )
    cleared: dict[str, Any] = {
        "hitl_pending": None,
        "hitl_payload": None,
        "current_agent": "hitl",
        "retry_count": 0,
        "feedback": "",
    }
    if resolution.action == "approve":
        merged = state.get("hitl_payload") or {}
        return Command(goto=NEXT_NODE.get(agent, END), update={**merged, **cleared})
    if resolution.action == "retry" and agent in NEXT_NODE:
        note = resolution.note or (pending.reason if pending else "")
        return Command(goto=agent, update={**cleared, "feedback": note})
    return Command(goto=END, update=cleared)


def cost_trigger(max_cost_usd: float, max_duration_min: float) -> HITLTrigger[ExperimentDesign]:
    """Pause before any design estimated above `max_cost_usd` or `max_duration_min`."""

    async def check(design: ExperimentDesign, _state: AutoLabState) -> str | None:
        if design.estimated_cost_usd > max_cost_usd:
            return f"estimated cost ${design.estimated_cost_usd:.2f} > ${max_cost_usd:.2f}"
        if design.estimated_duration_minutes > max_duration_min:
            return (
                f"estimated duration {design.estimated_duration_minutes:.0f} min"
                f" > {max_duration_min:.0f} min"
            )
        return None

    return check


def contradiction_trace(prior: Sequence[Hypothesis], new: Hypothesis) -> ReasoningTrace:
    """Premises: every prior statement and the new one; then the new one, citing them all.

    The verifier checks each premise against the earlier ones, so some step is
    INVALID/`premises_inconsistent` exactly when the formalized statements are
    jointly unsatisfiable. The trigger fails closed on any other non-VALID
    premise; the final step's own verdict is not a contradiction signal.
    """
    statements = [h.statement for h in prior] + [new.statement]
    steps = [
        ReasoningStep(index=i, claim=s, claim_type="premise") for i, s in enumerate(statements)
    ]
    steps.append(
        ReasoningStep(
            index=len(steps),
            claim=new.statement,
            claim_type="inference",
            referenced_step_indices=tuple(range(len(steps))),
        )
    )
    return ReasoningTrace(agent="contradiction_check", steps=tuple(steps))


def contradiction_trigger(verifier: Verifier) -> HITLTrigger[Hypothesis]:
    """Pause on a hypothesis that contradicts the hypotheses already in state (principle 7c)."""

    async def check(hypothesis: Hypothesis, state: AutoLabState) -> str | None:
        prior = state.get("hypotheses", [])
        if not prior:
            return None
        result = await verifier.verify(contradiction_trace(prior, hypothesis))
        if any(v.reason == "premises_inconsistent" for v in result.step_verdicts):
            return f"hypothesis contradicts {len(prior)} prior hypothesis(es) in state"
        # Fail closed (AUDIT-1 #6): a statement that did not formalize, or whose check
        # timed out, leaves the contradiction undecided.
        for v in result.step_verdicts[:-1]:
            if v.verdict != "VALID":
                return f"could not rule out a contradiction: step {v.step_index} is {v.reason}"
        return None

    return check


def new_in_process_saver() -> MemorySaver:
    """Return a LangGraph in-process checkpointer that allows `CHECKPOINT_TYPES`."""
    return MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES))


__all__ = [
    "contradiction_trace",
    "contradiction_trigger",
    "cost_trigger",
    "hitl_node",
    "new_in_process_saver",
]
