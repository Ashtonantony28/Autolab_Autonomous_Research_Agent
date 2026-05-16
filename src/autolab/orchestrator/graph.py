"""Build the LangGraph StateGraph that connects all five agents."""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from autolab.agents.analysis import AnalysisAgent
from autolab.agents.design import DesignAgent
from autolab.agents.execute import ExecuteAgent
from autolab.agents.research import ResearchAgent
from autolab.orchestrator.checkpoints import hitl_node, new_in_process_saver
from autolab.orchestrator.routing import route_after_agent
from autolab.state.schema import AutoLabState
from autolab.tools.lab_hardware.simulator import LabSimulator
from autolab.verification.llm import LLMClient
from autolab.verification.vericot import Verifier


def build_graph(
    *,
    llm: LLMClient,
    simulator: LabSimulator | None = None,
    checkpointer: MemorySaver | None = None,
) -> Any:
    """Wire the five agents into a compiled StateGraph.

    The compiled graph is returned. Callers invoke it via the standard
    LangGraph API: `await graph.ainvoke(initial_state, config=...)` or
    `await graph.astream(...)`.
    """
    verifier = Verifier(llm)
    research = ResearchAgent(verifier=verifier, llm=llm)
    design = DesignAgent(verifier=verifier, llm=llm)
    execute = ExecuteAgent(verifier=verifier, llm=llm, simulator=simulator or LabSimulator())
    analysis = AnalysisAgent(verifier=verifier, llm=llm)

    graph: StateGraph[AutoLabState, AutoLabState, AutoLabState] = StateGraph(AutoLabState)
    graph.add_node("research", research)
    graph.add_node("design", design)
    graph.add_node("execute", execute)
    graph.add_node("analysis", analysis)
    graph.add_node("hitl", hitl_node)

    graph.add_edge(START, "research")
    graph.add_conditional_edges(
        "research",
        _make_router("design"),
        {"next": "design", "hitl": "hitl"},
    )
    graph.add_conditional_edges(
        "design",
        _make_router("execute"),
        {"next": "execute", "hitl": "hitl"},
    )
    graph.add_conditional_edges(
        "execute",
        _make_router("analysis"),
        {"next": "analysis", "hitl": "hitl"},
    )
    graph.add_conditional_edges(
        "analysis",
        _make_router(END, terminal=True),
        {"end": END, "hitl": "hitl"},
    )
    graph.add_edge("hitl", END)

    return graph.compile(checkpointer=checkpointer or new_in_process_saver())


def _make_router(_next: str, *, terminal: bool = False) -> Any:
    def router(state: AutoLabState) -> str:
        return route_after_agent(state, terminal=terminal)

    return router


__all__ = ["build_graph"]
