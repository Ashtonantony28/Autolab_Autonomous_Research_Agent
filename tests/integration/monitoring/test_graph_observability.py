"""Metrics and RunTracer wired into the graph: one observation per agent execution."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from langgraph.types import Command

from autolab.agents.research import build_hypothesis_prompt
from autolab.config import get_settings
from autolab.monitoring.dashboard import EVENT_COLUMNS, load_run, render_event_table
from autolab.monitoring.metrics import Metrics
from autolab.monitoring.tracer import RunTracer
from autolab.orchestrator.graph import build_graph
from autolab.state.schema import AutoLabState, empty_state
from autolab.state.store import StateStore
from autolab.verification.llm import FakeLLMClient
from tests.integration.orchestrator.test_graph import (
    DESIGN,
    EXPENSIVE,
    FAILING_SUMMARY,
    HYPOTHESIS,
    QUESTION,
    REPORT,
    RESULT,
    FixedResultSimulator,
    _fail_design_trace,
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


async def run(
    fake: FakeLLMClient,
    *,
    metrics: Metrics | None = None,
    store: StateStore | None = None,
    state: AutoLabState | None = None,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    graph = build_graph(
        llm=fake,
        simulator=FixedResultSimulator(RESULT),
        retry_backoff_seconds=0,
        metrics=metrics,
        tracer=RunTracer(store) if store else None,
    )
    config = {"configurable": {"thread_id": str(uuid4())}}
    final = await graph.ainvoke(state or empty_state(QUESTION), config=config)
    return graph, config, final


def invocations(metrics: Metrics) -> dict[str, float]:
    return {name: snap["invocations"] for name, snap in metrics.snapshot().items()}


def run_id(config: dict[str, Any]) -> UUID:
    return UUID(config["configurable"]["thread_id"])


def retry_then_pass(fake: FakeLLMClient) -> None:
    _seed_full_run(fake)
    _fail_design_trace(fake)
    _seed_design(fake, DESIGN, feedback=FAILING_SUMMARY)


async def test_metrics_count_one_invocation_per_agent_on_a_clean_run() -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake)
    metrics = Metrics()

    _, _, final = await run(fake, metrics=metrics)

    snap = metrics.snapshot()
    assert invocations(metrics) == {"research": 1, "design": 1, "execute": 1, "analysis": 1}
    assert sum(invocations(metrics).values()) == len(final["reasoning_traces"])
    assert all(s["avg_consistency_score"] == 1.0 for s in snap.values())
    assert all(s["hitl_escalations"] == 0 and s["retries"] == 0 for s in snap.values())
    assert all(s["avg_latency_ms"] > 0 for s in snap.values())


async def test_metrics_count_retries_as_executions() -> None:
    fake = FakeLLMClient()
    retry_then_pass(fake)
    metrics = Metrics()

    _, _, final = await run(fake, metrics=metrics)

    design = metrics.snapshot()["design"]
    assert invocations(metrics) == {"research": 1, "design": 2, "execute": 1, "analysis": 1}
    assert sum(invocations(metrics).values()) == len(final["reasoning_traces"])
    assert design["retries"] == 1
    assert design["hitl_escalations"] == 0
    assert design["avg_consistency_score"] < 1.0


async def test_metrics_count_escalation_once_retries_are_exhausted() -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake)
    _fail_design_trace(fake)
    _fail_design_trace(fake, feedback=FAILING_SUMMARY)
    metrics = Metrics()

    graph, config, paused = await run(fake, metrics=metrics)
    assert "__interrupt__" in paused
    design = metrics.snapshot()["design"]
    assert design["invocations"] == 3  # 1 + max_retries
    assert design["retries"] == 2
    assert design["hitl_escalations"] == 1

    await graph.ainvoke(Command(resume={"action": "approve"}), config=config)
    assert invocations(metrics) == {"research": 1, "design": 3, "execute": 1, "analysis": 1}


async def test_metrics_record_permanent_errors_as_escalations() -> None:
    fake = FakeLLMClient()
    fake.register("research.hypothesis", build_hypothesis_prompt(QUESTION), '{"statement": 1}')
    metrics = Metrics()

    await run(fake, metrics=metrics)

    assert metrics.snapshot() == {
        "research": {
            "invocations": 1.0,
            "avg_latency_ms": pytest.approx(metrics.snapshot()["research"]["avg_latency_ms"]),
            "avg_consistency_score": 0.0,
            "hitl_escalations": 1.0,
            "retries": 0.0,
        }
    }


async def test_tracer_records_one_event_per_agent_execution(store: StateStore) -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake)

    _, config, final = await run(fake, store=store)

    events = await store.load_events(run_id(config))
    assert [e.node for e in events] == ["research", "design", "execute", "analysis"]
    for event, trace, verdict in zip(
        events, final["reasoning_traces"], final["verification_history"], strict=True
    ):
        assert event.run_id == run_id(config)
        assert event.attempt == 1
        assert event.latency_ms > 0
        assert event.trace_id == trace.id == verdict.trace_id
        assert event.consistency_score == verdict.consistency_score
        assert event.verdict_summary == verdict.summary
        assert event.decision == "pass"
        assert event.trigger is None
        assert event.hitl_reason is None
    assert [e.started_at for e in events] == sorted(e.started_at for e in events)
    assert await store.list_runs() == [(run_id(config), QUESTION)]


async def test_tracer_records_retries_with_attempt_and_feedback_cause(store: StateStore) -> None:
    fake = FakeLLMClient()
    retry_then_pass(fake)

    _, config, _ = await run(fake, store=store)

    events = await store.load_events(run_id(config))
    first, second = (e for e in events if e.node == "design")
    assert (first.attempt, first.decision, first.trigger) == (1, "retry", "verification")
    assert first.verdict_summary == FAILING_SUMMARY
    assert first.consistency_score is not None and first.consistency_score < 0.7
    assert (second.attempt, second.decision, second.trigger) == (2, "pass", None)
    assert len(events) == 5


async def test_tracer_records_policy_escalation(store: StateStore) -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake, EXPENSIVE)

    _, config, _ = await run(fake, store=store)

    events = await store.load_events(run_id(config))
    assert [(e.node, e.decision) for e in events] == [("research", "pass"), ("design", "escalate")]
    assert events[-1].trigger == "policy"
    assert events[-1].hitl_reason == "estimated cost $50.00 > $10.00"
    assert events[-1].consistency_score == 1.0


async def test_tracer_records_error_escalation_without_a_verdict(store: StateStore) -> None:
    fake = FakeLLMClient()
    fake.register("research.hypothesis", build_hypothesis_prompt(QUESTION), '{"statement": 1}')

    _, config, _ = await run(fake, store=store)

    (event,) = await store.load_events(run_id(config))
    assert event.decision == "escalate"
    assert event.trigger == "error"
    assert event.trace_id is None and event.consistency_score is None
    assert event.hitl_reason is not None and event.hitl_reason.startswith("ValidationError:")


async def test_tracer_persists_run_entities_once(store: StateStore) -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake)

    _, config, final = await run(fake, store=store)

    rid = run_id(config)
    assert await store.load_hypotheses(rid) == [HYPOTHESIS]
    assert await store.load_traces(rid) == final["reasoning_traces"]
    assert await store.load_verifications(rid) == final["verification_history"]
    assert await store.load_reports(rid) == [REPORT]


async def test_dashboard_renders_a_completed_run_from_the_store(store: StateStore) -> None:
    fake = FakeLLMClient()
    retry_then_pass(fake)
    _, config, final = await run(fake, store=store)

    view = await load_run(store, run_id(config))

    assert view.state["research_question"] == QUESTION
    assert view.state["reasoning_traces"] == final["reasoning_traces"]
    rows = render_event_table(view.events)
    assert rows[0] == EVENT_COLUMNS
    assert [r[0] for r in rows[1:]] == ["research", "design", "design", "execute", "analysis"]
    assert rows[2][4:6] == ["retry", "verification"]
    assert rows[3][1] == "2"


async def test_dashboard_script_renders_persisted_run(
    store: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    fake = FakeLLMClient()
    _seed_full_run(fake)
    _, config, _ = await run(fake, store=store)
    rid = run_id(config)
    url = f"sqlite+aiosqlite:///{tmp_path / 'store.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        app = AppTest.from_file(str(Path(__file__).parents[3] / "scripts" / "dashboard.py"))
        app.run(timeout=30)
    finally:
        get_settings.cache_clear()

    assert not app.exception
    assert app.title[0].value == "AutoLab — Run audit"
    text = " ".join(m.value for m in app.markdown)
    assert str(rid) in text and QUESTION in text
    events, traces = app.table
    assert events.value.shape[0] == 5  # header row + 4 agents
    assert traces.value.shape[0] > 4
