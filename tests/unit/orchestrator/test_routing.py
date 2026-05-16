"""Conditional routing behaviour."""

from __future__ import annotations

from autolab.orchestrator.routing import route_after_agent
from autolab.state.models import HITLRequest
from autolab.state.schema import empty_state


def test_next_when_confidence_above_threshold() -> None:
    state = empty_state("Q?")
    state["confidence"] = 0.9
    assert route_after_agent(state) == "next"


def test_end_when_terminal_and_passing() -> None:
    state = empty_state("Q?")
    state["confidence"] = 0.9
    assert route_after_agent(state, terminal=True) == "end"


def test_hitl_when_pending_set() -> None:
    state = empty_state("Q?")
    state["confidence"] = 1.0  # high confidence, but a pending request exists
    state["hitl_pending"] = HITLRequest(agent="design", reason="cost too high")
    assert route_after_agent(state) == "hitl"


def test_hitl_when_confidence_below_threshold() -> None:
    state = empty_state("Q?")
    state["confidence"] = 0.4
    assert route_after_agent(state) == "hitl"
    assert route_after_agent(state, terminal=True) == "hitl"
