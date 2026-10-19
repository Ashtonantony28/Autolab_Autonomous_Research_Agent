"""Build the LangGraph StateGraph that connects all five agents.

    START → research → monitor → design → monitor → execute → monitor
          → analysis → monitor → END

`monitor` (the VerificationMonitor) can also send control back to the same
agent (RETRY) or to `hitl`; `hitl` resumes to the next agent, the same agent
or END. See `routing.py` and `checkpoints.py`.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from autolab.agents.analysis import AnalysisAgent
from autolab.agents.base import BaseAgent
from autolab.agents.design import DesignAgent
from autolab.agents.execute import ExecuteAgent
from autolab.agents.research import ResearchAgent
from autolab.agents.verification_monitor import VerificationMonitor
from autolab.config import Settings, get_settings
from autolab.orchestrator.checkpoints import (
    contradiction_trigger,
    cost_trigger,
    hitl_node,
    new_in_process_saver,
)
from autolab.orchestrator.recovery import (
    INITIAL_BACKOFF_S,
    RetryingLLMClient,
    escalate_permanent_errors,
)
from autolab.orchestrator.routing import AGENTS, make_monitor_node
from autolab.state.schema import AutoLabState
from autolab.tools.lab_hardware.simulator import LabSimulator
from autolab.verification.llm import LLMClient
from autolab.verification.vericot import Verifier


def build_graph(
    *,
    llm: LLMClient,
    simulator: LabSimulator | None = None,
    checkpointer: MemorySaver | None = None,
    settings: Settings | None = None,
    retry_backoff_seconds: float = INITIAL_BACKOFF_S,
) -> Any:
    """Wire the five agents into a compiled StateGraph.

    Every LLM call (agents and verifier) is retried on transient errors, and
    a permanent agent error pauses at `hitl` instead of raising.
    The compiled graph is returned. Callers invoke it via the standard
    LangGraph API: `await graph.ainvoke(initial_state, config=...)` or
    `await graph.astream(...)`, and resume a HITL pause with
    `Command(resume=HITLResolution(...))`.
    """
    cfg = settings or get_settings()
    llm = RetryingLLMClient(llm, initial_backoff_seconds=retry_backoff_seconds)
    verifier = Verifier(llm, solver_timeout_seconds=cfg.solver_timeout_s)
    common: dict[str, Any] = {
        "verifier": verifier,
        "llm": llm,
        "hitl_threshold": cfg.pass_threshold,
    }
    agents: dict[str, BaseAgent[Any]] = {
        "research": ResearchAgent(**common, hitl_trigger=contradiction_trigger(verifier)),
        "design": DesignAgent(
            **common, hitl_trigger=cost_trigger(cfg.hitl_cost_usd, cfg.hitl_duration_min)
        ),
        "execute": ExecuteAgent(**common, simulator=simulator or LabSimulator()),
        "analysis": AnalysisAgent(**common),
    }
    monitor = VerificationMonitor(
        pass_threshold=cfg.pass_threshold, retry_threshold=cfg.retry_threshold
    )

    graph: StateGraph[AutoLabState, AutoLabState, AutoLabState] = StateGraph(AutoLabState)
    for name in AGENTS:
        graph.add_node(name, escalate_permanent_errors(name, agents[name]))
        graph.add_edge(name, "monitor")
    graph.add_node(
        "monitor",
        make_monitor_node(monitor, cfg.max_retries),
        destinations=(*AGENTS, "hitl", END),
    )
    graph.add_node("hitl", hitl_node, destinations=(*AGENTS, END))
    graph.add_edge(START, "research")

    return graph.compile(checkpointer=checkpointer or new_in_process_saver())


__all__ = ["build_graph"]
