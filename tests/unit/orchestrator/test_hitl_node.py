"""The `hitl` node re-runs policy on an approved verification escalation (SG12, AUDIT-5 #3)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from autolab.orchestrator.checkpoints import (
    MergedPolicy,
    cost_trigger,
    make_hitl_node,
    merged_policy,
    new_in_process_saver,
)
from autolab.state.models import ExperimentDesign, HITLRequest, Hypothesis
from autolab.state.schema import AutoLabState, empty_state

HYPOTHESIS = Hypothesis(question="q", statement="s", rationale="r")
CHEAP = ExperimentDesign(
    hypothesis_id=HYPOTHESIS.id,
    protocol="p",
    estimated_cost_usd=1.0,
    estimated_duration_minutes=1.0,
)
EXPENSIVE = CHEAP.model_copy(update={"estimated_cost_usd": 50.0})
COST = merged_policy(cost_trigger(10.0, 60.0), "current_experiment")


def _app(policy: MergedPolicy) -> Any:
    """`hitl` plus a stub `execute` node (the node after `design`)."""
    graph: StateGraph[AutoLabState, AutoLabState, AutoLabState] = StateGraph(AutoLabState)
    graph.add_node(
        "hitl", make_hitl_node({"design": policy}), destinations=("hitl", "execute", END)
    )
    graph.add_node("execute", lambda _s: {"current_agent": "execute"})
    graph.add_edge(START, "hitl")
    graph.add_edge("execute", END)
    return graph.compile(checkpointer=new_in_process_saver())


async def _pause(app: Any, trigger: str, design: ExperimentDesign) -> tuple[dict[str, Any], Any]:
    config = {"configurable": {"thread_id": str(uuid4())}}
    state = empty_state("q")
    state["hitl_pending"] = HITLRequest(
        agent="design", reason="blocked", payload={"trigger": trigger, "verdict_summary": "v"}
    )
    state["hitl_payload"] = {"current_experiment": design}
    paused = await app.ainvoke(state, config=config)
    assert "__interrupt__" in paused
    return config, paused


async def _approve(app: Any, config: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = await app.ainvoke(Command(resume={"action": "approve"}), config=config)
    return result


async def test_approved_verification_escalation_pauses_again_when_policy_fires() -> None:
    app = _app(COST)
    config, _ = await _pause(app, "verification", EXPENSIVE)

    again = await _approve(app, config)

    (pause,) = again["__interrupt__"]
    assert pause.value["payload"] == {"trigger": "policy", "verdict_summary": "v"}
    assert pause.value["reason"] == "estimated cost $50.00 > $10.00"
    assert again["current_experiment"] is None
    assert again["hitl_payload"] == {"current_experiment": EXPENSIVE}

    final = await _approve(app, config)

    assert "__interrupt__" not in final
    assert final["current_experiment"] == EXPENSIVE
    assert final["current_agent"] == "execute"


async def test_approved_verification_escalation_merges_when_policy_is_quiet() -> None:
    app = _app(COST)
    config, _ = await _pause(app, "verification", CHEAP)

    final = await _approve(app, config)

    assert "__interrupt__" not in final
    assert final["current_experiment"] == CHEAP
    assert final["current_agent"] == "execute"


async def test_approved_policy_request_is_not_rechecked() -> None:
    calls = 0

    async def firing(_merged: dict[str, Any], _state: AutoLabState) -> str | None:
        nonlocal calls
        calls += 1
        return "always"

    app = _app(firing)
    config, _ = await _pause(app, "policy", EXPENSIVE)

    final = await _approve(app, config)

    assert calls == 0
    assert final["current_experiment"] == EXPENSIVE


async def test_merged_policy_reads_the_last_list_item_and_skips_a_missing_key() -> None:
    seen: list[object] = []

    async def trigger(payload: object, _state: AutoLabState) -> str | None:
        seen.append(payload)
        return "fired"

    policy = merged_policy(trigger, "hypotheses")
    other = HYPOTHESIS.model_copy(update={"statement": "t"})
    state = empty_state("q")

    assert await policy({"hypotheses": [HYPOTHESIS, other]}, state) == "fired"
    assert await policy({}, state) is None
    assert await policy({"hypotheses": []}, state) is None
    assert seen == [other]
