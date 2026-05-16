"""VerificationMonitor decision routing."""

from __future__ import annotations

from uuid import uuid4

from autolab.agents.base import AgentDecision
from autolab.agents.verification_monitor import VerificationMonitor
from autolab.state.models import StepVerdict, VerificationResult


def _vr(score: float) -> VerificationResult:
    return VerificationResult(
        trace_id=uuid4(),
        step_verdicts=(StepVerdict(step_index=0, verdict="VALID"),),
        consistency_score=score,
    )


def test_pass_when_above_pass_threshold() -> None:
    m = VerificationMonitor()
    assert m.decide(_vr(0.9)) == AgentDecision.PASS
    assert m.decide(_vr(0.7)) == AgentDecision.PASS


def test_retry_between_thresholds() -> None:
    m = VerificationMonitor()
    assert m.decide(_vr(0.5)) == AgentDecision.RETRY
    assert m.decide(_vr(0.4)) == AgentDecision.RETRY


def test_escalate_below_retry_threshold() -> None:
    m = VerificationMonitor()
    assert m.decide(_vr(0.39)) == AgentDecision.ESCALATE
    assert m.decide(_vr(0.0)) == AgentDecision.ESCALATE


def test_custom_thresholds() -> None:
    m = VerificationMonitor(pass_threshold=0.95, retry_threshold=0.6)
    assert m.decide(_vr(0.95)) == AgentDecision.PASS
    assert m.decide(_vr(0.94)) == AgentDecision.RETRY
    assert m.decide(_vr(0.59)) == AgentDecision.ESCALATE
