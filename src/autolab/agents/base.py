"""Base class for AutoLab agents.

Every concrete agent implements `_produce(state)` returning an
`AgentOutput[PayloadT]`. The `__call__` template runs the VeriCoT verifier
on the produced reasoning trace and decides:

  - consistency_score >= HITL_THRESHOLD → emit the state update.
  - otherwise → emit a `HITLRequest` so the orchestrator pauses for review.

Agents never modify state directly; they return a dict that LangGraph will
merge via the `AutoLabState` reducers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Generic, TypeVar

from pydantic import BaseModel

from autolab.config import PASS_THRESHOLD
from autolab.state.models import HITLRequest, ReasoningTrace, VerificationContext
from autolab.state.schema import AutoLabState
from autolab.verification.vericot import Verifier

HITL_THRESHOLD: Final[float] = PASS_THRESHOLD


class AgentDecision(StrEnum):
    """What the VerificationMonitor decided about an agent output."""

    PASS = "pass"
    RETRY = "retry"
    ESCALATE = "escalate"


PayloadT = TypeVar("PayloadT", bound=BaseModel)


@dataclass(frozen=True)
class AgentOutput(Generic[PayloadT]):
    """An agent's structured output plus the reasoning that produced it."""

    payload: PayloadT
    reasoning_trace: ReasoningTrace
    context: VerificationContext | None = None


class BaseAgent(ABC, Generic[PayloadT]):
    """Template for an agent that produces verified, structured output."""

    name: str = "base"

    def __init__(self, verifier: Verifier, *, hitl_threshold: float = HITL_THRESHOLD) -> None:
        self.verifier = verifier
        self.hitl_threshold = hitl_threshold

    @abstractmethod
    async def _produce(self, state: AutoLabState) -> AgentOutput[PayloadT]:
        """Subclasses produce their structured payload + reasoning trace."""

    async def __call__(self, state: AutoLabState) -> dict[str, Any]:
        """Run the agent, verify, and return an `AutoLabState` partial update."""
        output = await self._produce(state)
        verdict = await self.verifier.verify(output.reasoning_trace, context=output.context)

        if verdict.consistency_score < self.hitl_threshold:
            return {
                "reasoning_traces": [output.reasoning_trace],
                "verification_history": [verdict],
                "current_agent": self.name,
                "confidence": verdict.consistency_score,
                "hitl_pending": HITLRequest(
                    agent=self.name,
                    reason=(
                        f"verifier consistency_score={verdict.consistency_score:.2f}"
                        f" < threshold={self.hitl_threshold}"
                    ),
                    payload={
                        "verdict_summary": verdict.summary,
                        "payload_kind": type(output.payload).__name__,
                    },
                ),
            }

        update: dict[str, Any] = {
            "reasoning_traces": [output.reasoning_trace],
            "verification_history": [verdict],
            "current_agent": self.name,
            "confidence": verdict.consistency_score,
            "hitl_pending": None,
        }
        update.update(self._merge_payload(output.payload))
        return update

    @abstractmethod
    def _merge_payload(self, payload: PayloadT) -> dict[str, Any]:
        """Return the AutoLabState fields the payload should be merged into."""


__all__ = ["HITL_THRESHOLD", "AgentDecision", "AgentOutput", "BaseAgent"]
