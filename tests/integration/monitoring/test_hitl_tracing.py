"""The `hitl` node is observed: every human decision and policy re-pause is traced (SG18)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest_asyncio
from langgraph.types import Command

from autolab.agents.analysis import build_analysis_trace_prompt
from autolab.agents.common import design_claim, design_facts, measurement_facts
from autolab.agents.design import build_design_prompt, build_design_trace_prompt
from autolab.config import Settings
from autolab.monitoring.dashboard import load_run
from autolab.monitoring.metrics import Metrics
from autolab.monitoring.tracer import RunTracer
from autolab.orchestrator.graph import build_graph
from autolab.state.models import ExperimentDesign, HITLResolution
from autolab.state.schema import empty_state
from autolab.state.store import StateStore
from autolab.verification.llm import FakeLLMClient
from tests.fixtures.agent_helpers import register_failing_trace
from tests.integration.orchestrator.test_graph import (
    DESIGN,
    EXPENSIVE,
    HYPOTHESIS,
    QUESTION,
    REPORT,
    RESULT,
    FixedResultSimulator,
    _seed_design,
    _seed_full_run,
)


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[StateStore]:
    s = await StateStore.create(f"sqlite+aiosqlite:///{tmp_path / 'store.db'}")
    try:
        yield s
    finally:
        await s.dispose()


class Run:
    """A traced graph with `max_retries=0`, so a failing trace escalates at once."""

    def __init__(self, fake: FakeLLMClient, store: StateStore) -> None:
        self.metrics = Metrics()
        self.graph = build_graph(
            llm=fake,
            simulator=FixedResultSimulator(RESULT),
            settings=Settings(max_retries=0),
            retry_backoff_seconds=0,
            tracer=RunTracer(store),
            metrics=self.metrics,
        )
        self.id = uuid4()
        self.config: dict[str, Any] = {"configurable": {"thread_id": str(self.id)}}

    async def start(self) -> dict[str, Any]:
        out: dict[str, Any] = await self.graph.ainvoke(empty_state(QUESTION), config=self.config)
        return out

    async def resume(self, action: str, note: str = "") -> dict[str, Any]:
        resolution = HITLResolution.model_validate({"action": action, "note": note})
        out: dict[str, Any] = await self.graph.ainvoke(
            Command(resume=resolution), config=self.config
        )
        return out


def fail_design(fake: FakeLLMClient, design: ExperimentDesign = DESIGN) -> None:
    fake.register("design.experiment", build_design_prompt(HYPOTHESIS), design.model_dump_json())
    register_failing_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(HYPOTHESIS, design),
        conclusion=design_claim(design),
        facts=design_facts(design),
    )


def decisions(events: list[Any]) -> list[tuple[str, str]]:
    return [(e.node, e.decision) for e in events]


async def test_an_approved_analysis_report_is_persisted_and_traced(store: StateStore) -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake)
    register_failing_trace(
        fake,
        agent="analysis",
        trace_purpose="analysis.trace",
        trace_prompt=build_analysis_trace_prompt(RESULT, REPORT),
        conclusion=REPORT.interpretations[0].statement,
        facts=measurement_facts(RESULT),
    )
    run = Run(fake, store)
    assert "__interrupt__" in await run.start()

    final = await run.resume("approve", "reviewed by hand")

    assert len(final["analysis_reports"]) == 1
    view = await load_run(store, run.id)
    assert view.state["analysis_reports"] == final["analysis_reports"]
    assert decisions(view.events)[-2:] == [("analysis", "escalate"), ("hitl", "approve")]
    approved = view.events[-1]
    assert (approved.agent, approved.trigger, approved.note) == (
        "analysis",
        "verification",
        "reviewed by hand",
    )
    assert approved.hitl_reason == view.events[-2].hitl_reason
    assert approved.run_id == run.id
    assert run.metrics.hitl_decisions == {"approve": 1}
    assert "hitl" not in run.metrics.snapshot()


async def test_a_retry_decision_is_traced_with_its_note(store: StateStore) -> None:
    note = "use a cheaper reagent"
    fake = FakeLLMClient()
    _seed_full_run(fake)
    fail_design(fake)
    _seed_design(fake, DESIGN, feedback=note)
    run = Run(fake, store)
    await run.start()

    final = await run.resume("retry", note)

    assert "__interrupt__" not in final
    events = await store.load_events(run.id)
    assert decisions(events) == [
        ("research", "pass"),
        ("design", "escalate"),
        ("hitl", "retry"),
        ("design", "pass"),
        ("execute", "pass"),
        ("analysis", "pass"),
    ]
    assert (events[2].agent, events[2].note) == ("design", note)
    assert run.metrics.hitl_decisions == {"retry": 1}


async def test_an_abort_decision_is_traced_and_merges_nothing(store: StateStore) -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake)
    fail_design(fake)
    run = Run(fake, store)
    await run.start()

    final = await run.resume("abort", "not worth it")

    assert final["current_experiment"] is None
    events = await store.load_events(run.id)
    assert decisions(events) == [("research", "pass"), ("design", "escalate"), ("hitl", "abort")]
    assert (events[-1].agent, events[-1].note) == ("design", "not worth it")
    assert await store.load_reports(run.id) == []
    assert run.metrics.hitl_decisions == {"abort": 1}


async def test_a_policy_repause_is_traced(store: StateStore) -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake, EXPENSIVE)
    fail_design(fake, EXPENSIVE)
    run = Run(fake, store)
    await run.start()

    again = await run.resume("approve")

    assert again["__interrupt__"][0].value["payload"]["trigger"] == "policy"
    events = await store.load_events(run.id)
    assert decisions(events) == [
        ("research", "pass"),
        ("design", "escalate"),
        ("hitl", "approve"),
        ("hitl", "escalate"),
    ]
    approved, repaused = events[2:]
    assert approved.trigger == "verification"
    assert (repaused.agent, repaused.trigger) == ("design", "policy")
    assert repaused.hitl_reason == "estimated cost $50.00 > $10.00"
    assert repaused.started_at >= approved.started_at

    final = await run.resume("approve")

    assert "__interrupt__" not in final
    events = await store.load_events(run.id)
    assert decisions(events)[4:] == [("hitl", "approve"), ("execute", "pass"), ("analysis", "pass")]
    assert events[4].trigger == "policy"
    assert await store.load_reports(run.id) == final["analysis_reports"]
    assert run.metrics.hitl_decisions == {"approve": 2, "escalate": 1}


async def test_hitl_events_need_no_tracer() -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake, EXPENSIVE)
    metrics = Metrics()
    graph = build_graph(
        llm=fake, simulator=FixedResultSimulator(RESULT), retry_backoff_seconds=0, metrics=metrics
    )
    config = {"configurable": {"thread_id": "not-a-uuid"}}
    await graph.ainvoke(empty_state(QUESTION), config=config)

    await graph.ainvoke(Command(resume={"action": "approve"}), config=config)

    assert metrics.hitl_decisions == {"approve": 1}
