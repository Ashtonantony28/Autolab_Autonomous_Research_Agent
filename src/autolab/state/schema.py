"""LangGraph state schema for AutoLab.

`AutoLabState` is the single source of truth that flows through the graph.
List fields use the additive reducer so multiple agents emitting in parallel
merge cleanly; scalars are last-writer-wins by default.
"""

from __future__ import annotations

from operator import add
from typing import Annotated, TypedDict

from autolab.state.models import (
    ExperimentDesign,
    ExperimentResult,
    HITLRequest,
    Hypothesis,
    ReasoningTrace,
    VerificationResult,
)


class AutoLabState(TypedDict, total=False):
    """The global state passed between every agent."""

    research_question: str
    hypotheses: Annotated[list[Hypothesis], add]
    current_experiment: ExperimentDesign | None
    results: Annotated[list[ExperimentResult], add]
    reasoning_traces: Annotated[list[ReasoningTrace], add]
    verification_history: Annotated[list[VerificationResult], add]
    hitl_pending: HITLRequest | None
    current_agent: str
    confidence: float


def empty_state(research_question: str) -> AutoLabState:
    """Return a freshly-initialized state for a new research question."""
    return AutoLabState(
        research_question=research_question,
        hypotheses=[],
        current_experiment=None,
        results=[],
        reasoning_traces=[],
        verification_history=[],
        hitl_pending=None,
        current_agent="",
        confidence=0.0,
    )


__all__ = ["AutoLabState", "empty_state"]
