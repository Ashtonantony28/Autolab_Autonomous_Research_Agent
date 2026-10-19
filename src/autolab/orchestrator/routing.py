"""Conditional routing for the AutoLab StateGraph.

Every agent node is followed by the `monitor` node, which applies the
`VerificationMonitor` to the agent's update:

  - no `hitl_pending` → the next agent (or END after analysis).
  - a `verification` request the monitor rates RETRY, with retries left →
    the same agent again, with the verdict summary as `feedback`.
  - anything else (ESCALATE, retries exhausted, a policy trigger or an
    error) → the `hitl` node.
"""

from __future__ import annotations

from typing import Any, Final, Protocol

from langgraph.graph import END
from langgraph.types import Command

from autolab.agents.verification_monitor import AgentDecision, VerificationMonitor
from autolab.config import PASS_THRESHOLD
from autolab.state.schema import AutoLabState

AGENTS: Final[tuple[str, ...]] = ("research", "design", "execute", "analysis")
NEXT_NODE: Final[dict[str, str]] = {
    "research": "design",
    "design": "execute",
    "execute": "analysis",
    "analysis": END,
}


class MonitorNode(Protocol):
    """The `monitor` LangGraph node."""

    def __call__(self, state: AutoLabState) -> Command[Any]: ...


def make_monitor_node(monitor: VerificationMonitor, max_retries: int) -> MonitorNode:
    """Build the `monitor` node: route the last agent's update to next, retry or hitl."""

    def monitor_node(state: AutoLabState) -> Command[Any]:
        agent = state.get("current_agent", "")
        pending = state.get("hitl_pending")
        if pending is None:
            return Command(
                goto=NEXT_NODE.get(agent, END), update={"retry_count": 0, "feedback": ""}
            )
        retries = state.get("retry_count", 0)
        history = state.get("verification_history", [])
        if (
            pending.payload.get("trigger") == "verification"
            and history
            and retries < max_retries
            and monitor.decide(history[-1]) is AgentDecision.RETRY
        ):
            return Command(
                goto=agent,
                update={
                    "hitl_pending": None,
                    "hitl_payload": None,
                    "retry_count": retries + 1,
                    "feedback": history[-1].summary,
                },
            )
        return Command(goto="hitl")

    return monitor_node


__all__ = ["AGENTS", "NEXT_NODE", "PASS_THRESHOLD", "make_monitor_node"]
