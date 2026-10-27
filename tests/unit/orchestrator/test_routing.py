"""The `monitor` node's routing."""

from __future__ import annotations

from uuid import uuid4

from langgraph.graph import END

from autolab.agents.verification_monitor import VerificationMonitor
from autolab.orchestrator.routing import make_monitor_node
from autolab.state.models import HITLRequest, StepVerdict, VerificationResult
from autolab.state.schema import AutoLabState, empty_state

node = make_monitor_node(VerificationMonitor(), max_retries=2)


def _vr(score: float) -> VerificationResult:
    return VerificationResult(
        trace_id=uuid4(),
        step_verdicts=(StepVerdict(step_index=0, verdict="VALID"),),
        consistency_score=score,
        summary=f"summary {score}",
    )


def _state(agent: str, trigger: str | None, score: float = 0.5, retries: int = 0) -> AutoLabState:
    state = empty_state("Q?")
    state["current_agent"] = agent
    state["retry_count"] = retries
    state["verification_history"] = [_vr(score)]
    if trigger is not None:
        state["hitl_pending"] = HITLRequest(agent=agent, reason="r", payload={"trigger": trigger})
    return state


def test_pass_goes_to_next_agent_and_resets_retries() -> None:
    cmd = node(_state("research", None, 0.9, retries=1))
    assert cmd.goto == "design"
    assert cmd.update == {"retry_count": 0, "feedback": ""}


def test_pass_after_analysis_ends() -> None:
    assert node(_state("analysis", None, 0.9)).goto == END


def test_retry_band_reruns_same_agent_with_feedback() -> None:
    cmd = node(_state("design", "verification", 0.5, retries=1))
    assert cmd.goto == "design"
    assert cmd.update == {
        "hitl_pending": None,
        "hitl_payload": None,
        "retry_count": 2,
        "feedback": "summary 0.5",
    }


def test_retries_exhausted_escalates() -> None:
    assert node(_state("design", "verification", 0.5, retries=2)).goto == "hitl"


def test_escalate_band_goes_to_hitl() -> None:
    assert node(_state("design", "verification", 0.1)).goto == "hitl"


def test_policy_and_error_requests_are_never_retried() -> None:
    assert node(_state("design", "policy", 0.9)).goto == "hitl"
    assert node(_state("design", "error", 0.5)).goto == "hitl"


def test_verification_request_without_history_escalates() -> None:
    state = _state("research", "verification")
    state["verification_history"] = []
    assert node(state).goto == "hitl"


# SG25 (AUDIT-19 #2, AUDIT-15 #3): a request with its own `feedback` (a Design
# figure rejection or estimate disagreement) is retried with that reason first.


def _fixable(score: float, retries: int = 0, trigger: str = "verification") -> AutoLabState:
    state = _state("design", trigger, score, retries=retries)
    state["hitl_pending"] = HITLRequest(
        agent="design", reason="fix it", payload={"trigger": trigger, "feedback": "fix it"}
    )
    return state


def test_a_fixable_request_on_a_passing_verdict_is_retried_with_its_reason() -> None:
    cmd = node(_fixable(0.9, retries=1))
    assert cmd.goto == "design"
    assert cmd.update == {
        "hitl_pending": None,
        "hitl_payload": None,
        "retry_count": 2,
        "feedback": "fix it",
    }


def test_a_fixable_request_on_a_retry_verdict_carries_both_reasons() -> None:
    cmd = node(_fixable(0.5))
    assert cmd.goto == "design"
    assert isinstance(cmd.update, dict)
    assert cmd.update["feedback"] == "summary 0.5; fix it"


def test_a_fixable_request_still_escalates_when_exhausted_or_rated_escalate() -> None:
    assert node(_fixable(0.9, retries=2)).goto == "hitl"
    assert node(_fixable(0.1)).goto == "hitl"
    assert node(_fixable(0.9, trigger="policy")).goto == "hitl"


def test_a_passing_verdict_without_feedback_is_not_retried() -> None:
    assert node(_state("design", "verification", 0.9)).goto == "hitl"
