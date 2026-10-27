"""Base class for AutoLab agents.

Every concrete agent implements `_produce(state)` returning an
`AgentOutput[PayloadT]`. The `__call__` template runs the VeriCoT verifier
on the produced reasoning trace and decides:

  - the verdict passes `VerificationMonitor(pass_threshold=hitl_threshold)`
    (score at or above the threshold and not blocked: no INVALID or
    `no_premises` step, final step VALID) and no `hitl_trigger` fires → emit
    the state update.
  - otherwise → emit a `HITLRequest` and withhold the payload's state update
    in `hitl_payload`. The orchestrator's monitor node then retries the agent
    or pauses for review, and an `approve` resolution merges `hitl_payload`.

`HITLRequest.payload["trigger"]` names the cause: `verification` or `policy`
(a `hitl_trigger` fired). The orchestrator adds `error` for permanent errors
(`orchestrator.recovery.escalate_permanent_errors`). Only `verification`
requests are retried.

Agents never modify state directly; they return a dict that LangGraph will
merge via the `AutoLabState` reducers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Generic, TypeVar

from pydantic import BaseModel

from autolab.agents.verification_monitor import (
    AgentDecision,
    VerificationMonitor,
    blocking_reason,
)
from autolab.config import PASS_THRESHOLD
from autolab.state.models import HITLRequest, ReasoningTrace, VerificationContext
from autolab.state.schema import AutoLabState
from autolab.verification.vericot import Verifier

HITL_THRESHOLD: Final[float] = PASS_THRESHOLD

PayloadT = TypeVar("PayloadT", bound=BaseModel)

HITLTrigger = Callable[[PayloadT, AutoLabState], Awaitable[str | None]]
"""Policy check on a verified payload: a reason to pause for a human, or None."""


@dataclass(frozen=True)
class AgentOutput(Generic[PayloadT]):
    """An agent's structured output plus the reasoning that produced it."""

    payload: PayloadT
    reasoning_trace: ReasoningTrace
    context: VerificationContext | None = None


class BaseAgent(ABC, Generic[PayloadT]):
    """Template for an agent that produces verified, structured output."""

    name: str = "base"

    def __init__(
        self,
        verifier: Verifier,
        *,
        hitl_threshold: float = HITL_THRESHOLD,
        hitl_trigger: HITLTrigger[PayloadT] | None = None,
    ) -> None:
        self.verifier = verifier
        self.hitl_threshold = hitl_threshold
        self.hitl_trigger = hitl_trigger

    @abstractmethod
    async def _produce(self, state: AutoLabState) -> AgentOutput[PayloadT]:
        """Subclasses produce their structured payload + reasoning trace."""

    async def __call__(self, state: AutoLabState) -> dict[str, Any]:
        """Run the agent, verify, and return an `AutoLabState` partial update."""
        output = await self._produce(state)
        verdict = await self.verifier.verify(output.reasoning_trace, context=output.context)
        update: dict[str, Any] = {
            "reasoning_traces": [output.reasoning_trace],
            "verification_history": [verdict],
            "current_agent": self.name,
            "confidence": verdict.consistency_score,
        }
        merged = self._merge_payload(output.payload)
        monitor = VerificationMonitor(pass_threshold=self.hitl_threshold)
        if monitor.decide(verdict) is not AgentDecision.PASS:
            reason = blocking_reason(verdict) or (
                f"verifier consistency_score={verdict.consistency_score:.2f}"
                f" < threshold={self.hitl_threshold}"
            )
            pending = self._request("verification", reason, verdict.summary, output.payload)
            return {**update, "hitl_pending": pending, "hitl_payload": merged}
        if self.hitl_trigger is not None:
            policy = await self.hitl_trigger(output.payload, state)
            if policy is not None:
                pending = self._request("policy", policy, verdict.summary, output.payload)
                return {**update, "hitl_pending": pending, "hitl_payload": merged}
        return {**update, **merged, "hitl_pending": None, "hitl_payload": None}

    def _request(self, trigger: str, reason: str, summary: str, payload: BaseModel) -> HITLRequest:
        return HITLRequest(
            agent=self.name,
            reason=reason,
            payload={
                "trigger": trigger,
                "verdict_summary": summary,
                "payload_kind": type(payload).__name__,
            },
        )

    @abstractmethod
    def _merge_payload(self, payload: PayloadT) -> dict[str, Any]:
        """Return the AutoLabState fields the payload should be merged into."""


__all__ = ["HITL_THRESHOLD", "AgentDecision", "AgentOutput", "BaseAgent", "HITLTrigger"]
