"""Verification Monitor — decides what happens after a verifier verdict.

Not a producing agent. The orchestrator's `monitor` node calls
`VerificationMonitor.decide(verdict)` on the latest VerificationResult and
routes the graph accordingly:

  - PASS     → proceed to the next agent.
  - RETRY    → re-run the previous agent with the verdict as feedback.
  - ESCALATE → pause at the HITL node.

Retry-first default: a trace with a refuted (INVALID) step or one that does
not conclude its payload claim (`no_premises`) is retried with feedback
before a human sees it, whatever its score. Set `retry_rework=False` to
escalate such traces by score alone.
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
        if score >= self.pass_threshold:
            return AgentDecision.PASS
        if score >= self.retry_threshold or (self.retry_rework and _needs_rework(verdict)):
            return AgentDecision.RETRY
        return AgentDecision.ESCALATE


def _needs_rework(verdict: VerificationResult) -> bool:
    """A refuted step or an unbound conclusion: fixable by the agent given feedback."""
    return any(v.verdict == "INVALID" or v.reason == "no_premises" for v in verdict.step_verdicts)


__all__ = ["PASS_THRESHOLD", "RETRY_THRESHOLD", "AgentDecision", "VerificationMonitor"]
