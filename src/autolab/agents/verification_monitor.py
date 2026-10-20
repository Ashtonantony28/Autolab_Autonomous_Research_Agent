"""Verification Monitor — decides what happens after a verifier verdict.

Not a producing agent. The orchestrator (Phase 4) calls
`VerificationMonitor.decide(verdict)` on the latest VerificationResult and
routes the graph accordingly:

  - PASS     → proceed to the next agent.
  - RETRY    → re-run the previous agent with the verdict as feedback.
  - ESCALATE → emit a HITLRequest and pause.
"""

from __future__ import annotations

from dataclasses import dataclass

from autolab.agents.base import AgentDecision
from autolab.config import PASS_THRESHOLD, RETRY_THRESHOLD
from autolab.state.models import VerificationResult


@dataclass
class VerificationMonitor:
    """Threshold-based verdict router."""

    pass_threshold: float = PASS_THRESHOLD
    retry_threshold: float = RETRY_THRESHOLD

    def decide(self, verdict: VerificationResult) -> AgentDecision:
        score = verdict.consistency_score
        if score >= self.pass_threshold:
            return AgentDecision.PASS
        if score >= self.retry_threshold:
            return AgentDecision.RETRY
        return AgentDecision.ESCALATE


__all__ = ["PASS_THRESHOLD", "RETRY_THRESHOLD", "VerificationMonitor"]
