"""VerificationMonitor decision routing."""

from __future__ import annotations

from uuid import uuid4

import pytest

from autolab.agents.base import AgentDecision
from autolab.agents.verification_monitor import VerificationMonitor, blocking_reason
from autolab.state.models import StepReason, StepVerdict, Verdict, VerificationResult


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


def _rework(score: float, verdict: Verdict, reason: StepReason) -> VerificationResult:
    return VerificationResult(
        trace_id=uuid4(),
        step_verdicts=(
            StepVerdict(step_index=0, verdict="VALID"),
            StepVerdict(step_index=1, verdict=verdict, reason=reason),
        ),
        consistency_score=score,
    )


def test_refuted_or_unbound_trace_is_retried_whatever_its_score() -> None:
    m = VerificationMonitor()
    assert m.decide(_rework(0.1, "INVALID", "not_entailed")) == AgentDecision.RETRY
    assert m.decide(_rework(0.0, "UNGROUNDED", "no_premises")) == AgentDecision.RETRY


def test_retry_rework_off_escalates_by_score() -> None:
    m = VerificationMonitor(retry_rework=False)
    assert m.decide(_rework(0.1, "INVALID", "not_entailed")) == AgentDecision.ESCALATE
    assert m.decide(_rework(0.0, "UNGROUNDED", "no_premises")) == AgentDecision.ESCALATE


def _steps(score: float, *steps: tuple[Verdict, StepReason]) -> VerificationResult:
    return VerificationResult(
        trace_id=uuid4(),
        step_verdicts=tuple(
            StepVerdict(step_index=i, verdict=v, reason=r) for i, (v, r) in enumerate(steps)
        ),
        consistency_score=score,
    )


def test_low_score_without_rework_escalates() -> None:
    m = VerificationMonitor()
    result = _steps(0.2, ("UNGROUNDED", "timeout"), ("VALID", "entailed"))
    assert m.decide(result) == AgentDecision.ESCALATE


@pytest.mark.parametrize("reason", ["timeout", "formalization_failed"])
def test_unproven_final_step_never_passes_and_is_retried(reason: StepReason) -> None:
    # AUDIT-1 #3: the rework rule is checked before the score shortcut.
    result = _steps(0.9, ("VALID", "assumed"), ("VALID", "assumed"), ("UNGROUNDED", reason))
    assert VerificationMonitor().decide(result) == AgentDecision.RETRY
    assert VerificationMonitor(retry_rework=False).decide(result) == AgentDecision.RETRY
    assert blocking_reason(result) == f"final step 2 is UNGROUNDED ({reason})"


@pytest.mark.parametrize("pass_threshold", [0.0, 0.3, 0.5, 0.7])
def test_blocked_trace_never_passes_at_any_threshold(pass_threshold: float) -> None:
    refuted = _steps(0.95, ("VALID", "assumed"), ("INVALID", "not_entailed"), ("VALID", "entailed"))
    m = VerificationMonitor(pass_threshold=pass_threshold, retry_threshold=pass_threshold)
    assert m.decide(refuted) == AgentDecision.RETRY
    no_rework = VerificationMonitor(pass_threshold=pass_threshold, retry_rework=False)
    assert no_rework.decide(refuted) is not AgentDecision.PASS


def test_retry_rework_off_routes_a_blocked_trace_by_score() -> None:
    m = VerificationMonitor(retry_rework=False)
    assert m.decide(_rework(0.69, "INVALID", "not_entailed")) == AgentDecision.RETRY
    assert m.decide(_rework(0.39, "INVALID", "not_entailed")) == AgentDecision.ESCALATE


def test_empty_trace_is_blocked() -> None:
    result = VerificationResult(trace_id=uuid4(), step_verdicts=(), consistency_score=0.0)
    assert blocking_reason(result) == "no steps verified"
    assert VerificationMonitor().decide(result) == AgentDecision.RETRY


def test_blocking_reason_names_the_first_blocking_step() -> None:
    result = _steps(
        0.5, ("VALID", "assumed"), ("UNGROUNDED", "no_premises"), ("INVALID", "not_entailed")
    )
    assert blocking_reason(result) == "step 1 is UNGROUNDED (no_premises)"
    assert blocking_reason(_vr(1.0)) is None
