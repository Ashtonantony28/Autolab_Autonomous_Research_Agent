"""HITL pause node + checkpoint plumbing for the orchestrator.

The HITL node uses LangGraph's `interrupt()` to suspend the graph and
return control to the caller, who can inspect `hitl_pending` and either
resolve it or terminate the run.

LangGraph ships its own `MemorySaver` for in-process runs — we use that
here and keep our `Checkpointer` protocol (Phase 1, ADR-0004) as the
*external* checkpoint for state we want to persist across processes.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt

from autolab.state.schema import AutoLabState


def hitl_node(state: AutoLabState) -> dict[str, Any]:
    """LangGraph node: suspend the graph for human resolution.

    The caller must inspect `hitl_pending`, decide what to do, and resume
    via `Command(resume=...)`. The `interrupt` return value (the resume
    payload) is discarded for now — Phase 6's dashboard will surface and
    consume it.
    """
    pending = state.get("hitl_pending")
    interrupt(
        {
            "agent": pending.agent if pending else "unknown",
            "reason": pending.reason if pending else "",
            "payload": dict(pending.payload) if pending else {},
        }
    )
    return {"hitl_pending": None, "current_agent": "hitl", "confidence": 1.0}


def new_in_process_saver() -> MemorySaver:
    """Return a LangGraph in-process checkpointer."""
    return MemorySaver()


__all__ = ["hitl_node", "new_in_process_saver"]
