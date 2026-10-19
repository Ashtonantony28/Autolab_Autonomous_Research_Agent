"""Conditional routing for the AutoLab StateGraph."""

from __future__ import annotations

from typing import Literal

from autolab.config import PASS_THRESHOLD
from autolab.state.schema import AutoLabState

RouteOutcome = Literal["next", "hitl", "end"]


def route_after_agent(state: AutoLabState, *, terminal: bool = False) -> RouteOutcome:
    """Decide what happens after an agent emits its state update.

    - `hitl_pending` set → route to the HITL pause node.
    - `confidence < PASS_THRESHOLD` → route to HITL (low confidence is
      worth a human check even if the agent didn't raise a HITLRequest).
    - Otherwise → continue (or end if `terminal` is True).
    """
    if state.get("hitl_pending") is not None:
        return "hitl"
    if state.get("confidence", 0.0) < PASS_THRESHOLD:
        return "hitl"
    return "end" if terminal else "next"


__all__ = ["PASS_THRESHOLD", "RouteOutcome", "route_after_agent"]
