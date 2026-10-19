"""Build the LangGraph StateGraph that connects all five agents.

    START → research → monitor → design → monitor → execute → monitor
          → analysis → monitor → END

`monitor` (the VerificationMonitor) can also send control back to the same
agent (RETRY) or to `hitl`; `hitl` resumes to the next agent, the same agent
or END. See `routing.py` and `checkpoints.py`.

Every agent node is wrapped by `_observed`, which feeds an optional
`Metrics` and `RunTracer` one `TraceEvent` per execution.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from autolab.agents.analysis import AnalysisAgent
from autolab.agents.base import BaseAgent
from autolab.agents.design import DesignAgent
from autolab.agents.execute import ExecuteAgent
from autolab.agents.research import ResearchAgent
from autolab.agents.verification_monitor import VerificationMonitor
from autolab.config import Settings, get_settings
from autolab.monitoring.events import NodeDecision, TraceEvent
from autolab.monitoring.metrics import Metrics
from autolab.monitoring.tracer import RunTracer
from autolab.orchestrator.checkpoints import (
    contradiction_trigger,
    cost_trigger,
    hitl_node,
    new_in_process_saver,
)
from autolab.orchestrator.recovery import (
    INITIAL_BACKOFF_S,
    AgentNode,
    RetryingLLMClient,
    escalate_permanent_errors,
)
from autolab.orchestrator.routing import AGENTS, MonitorNode, make_monitor_node
from autolab.state.schema import AutoLabState
from autolab.tools.lab_hardware.simulator import LabSimulator
from autolab.verification.llm import LLMClient
from autolab.verification.vericot import Verifier


def build_graph(
    *,
    llm: LLMClient,
    simulator: LabSimulator | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    settings: Settings | None = None,
    retry_backoff_seconds: float = INITIAL_BACKOFF_S,
    tracer: RunTracer | None = None,
    metrics: Metrics | None = None,
) -> Any:
    """Wire the five agents into a compiled StateGraph.

    Every LLM call (agents and verifier) is retried on transient errors, and
    a permanent agent error pauses at `hitl` instead of raising.
    The compiled graph is returned. Callers invoke it via the standard
    LangGraph API: `await graph.ainvoke(initial_state, config=...)` or
    `await graph.astream(...)`, and resume a HITL pause with
    `Command(resume=HITLResolution(...))`.

    With a `tracer`, the run's `thread_id` must be a UUID: it becomes the run id.
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

    route = make_monitor_node(monitor, cfg.max_retries)

    graph: StateGraph[AutoLabState, AutoLabState, AutoLabState] = StateGraph(AutoLabState)
    for name in AGENTS:
        node = escalate_permanent_errors(name, agents[name])
        graph.add_node(name, _observed(name, node, route, tracer, metrics))
        graph.add_edge(name, "monitor")
    graph.add_node("monitor", route, destinations=(*AGENTS, "hitl", END))
    graph.add_node("hitl", hitl_node, destinations=(*AGENTS, END))
    graph.add_edge(START, "research")

    return graph.compile(checkpointer=checkpointer or new_in_process_saver())


class ObservedNode(Protocol):
    """An agent node that also takes the run config (for its `thread_id`)."""

    async def __call__(self, state: AutoLabState, config: RunnableConfig) -> dict[str, Any]: ...


def _observed(
    name: str,
    node: AgentNode,
    route: MonitorNode,
    tracer: RunTracer | None,
    metrics: Metrics | None,
) -> ObservedNode:
    """Wrap an agent node to time it and record a `TraceEvent` for each execution.

    The decision is what `route` (the pure monitor node) will do with this update.
    """

    async def observed(state: AutoLabState, config: RunnableConfig) -> dict[str, Any]:
        started = datetime.now(tz=UTC)
        t0 = time.perf_counter()
        update = await node(state)
        latency_ms = (time.perf_counter() - t0) * 1000
        new = update.get("verification_history") or []
        verdict = new[-1] if new else None
        pending = update.get("hitl_pending")
        probe: AutoLabState = {
            **state,
            "current_agent": name,
            "hitl_pending": pending,
            "verification_history": [*state.get("verification_history", []), *new],
        }
        goto = route(probe).goto
        decision: NodeDecision = (
            "retry" if goto == name else "escalate" if goto == "hitl" else "pass"
        )
        score = verdict.consistency_score if verdict else 0.0
        if metrics is not None:
            metrics.observe_agent(
                name,
                latency_ms=latency_ms,
                consistency_score=score,
                escalated=decision == "escalate",
                retried=decision == "retry",
            )
        if tracer is not None:
            event = TraceEvent(
                run_id=UUID(config["configurable"]["thread_id"]),
                node=name,
                attempt=state.get("retry_count", 0) + 1,
                started_at=started,
                latency_ms=latency_ms,
                trace_id=verdict.trace_id if verdict else None,
                consistency_score=verdict.consistency_score if verdict else None,
                verdict_summary=verdict.summary if verdict else "",
                decision=decision,
                trigger=pending.payload.get("trigger") if pending else None,
                hitl_reason=pending.reason if pending else None,
            )
            # An agent update is a partial AutoLabState.
            partial = cast(AutoLabState, update)
            await tracer.record_node(event, state.get("research_question", ""), state, partial)
        return update

    return observed


__all__ = ["build_graph"]
