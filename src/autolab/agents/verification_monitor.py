"""Verification Monitor — decides what happens after a verifier verdict.

Not a producing agent. The orchestrator's `monitor` node calls
`VerificationMonitor.decide(verdict)` on the latest VerificationResult and
routes the graph accordingly:

  - PASS     → proceed to the next agent.
  - RETRY    → re-run the previous agent with the verdict as feedback.
  - ESCALATE → pause at the HITL node.

A *blocked* trace never passes, whatever its score or the thresholds: it has
an INVALID step, a `no_premises` step (an unbound or circular conclusion),
or a final step (the payload conclusion) that is not VALID. Retry-first
default: a blocked trace is retried with feedback before a human sees it.
Set `retry_rework=False` to route it by score instead (RETRY or ESCALATE).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from autolab.config import PASS_THRESHOLD, RETRY_THRESHOLD
from autolab.state.models import VerificationResult


class AgentDecision(StrEnum):
    """What the VerificationMonitor decided about an agent output."""

    PASS = "pass"
    RETRY = "retry"
    ESCALATE = "escalate"


@dataclass
class VerificationMonitor:
    """Threshold-based verdict router."""

    pass_threshold: float = PASS_THRESHOLD
    retry_threshold: float = RETRY_THRESHOLD
    retry_rework: bool = True

    def decide(self, verdict: VerificationResult) -> AgentDecision:
        """Map a verdict to PASS, RETRY or ESCALATE."""
        score = verdict.consistency_score
        if blocking_reason(verdict) is not None:
            if self.retry_rework or score >= self.retry_threshold:
                return AgentDecision.RETRY
            return AgentDecision.ESCALATE
        if score >= self.pass_threshold:
            return AgentDecision.PASS
        if score >= self.retry_threshold:
            return AgentDecision.RETRY
        return AgentDecision.ESCALATE


def blocking_reason(verdict: VerificationResult) -> str | None:
    """Why `verdict` can never pass, or None (it may pass on score)."""
    for v in verdict.step_verdicts:
        if v.verdict == "INVALID" or v.reason == "no_premises":
            return f"step {v.step_index} is {v.verdict} ({v.reason})"
    last = verdict.step_verdicts[-1] if verdict.step_verdicts else None
    if last is None:
        return "no steps verified"
    if last.verdict != "VALID":
        return f"final step {last.step_index} is {last.verdict} ({last.reason})"
    return None


__all__ = [
    "PASS_THRESHOLD",
    "RETRY_THRESHOLD",
    "AgentDecision",
    "VerificationMonitor",
    "blocking_reason",
]
