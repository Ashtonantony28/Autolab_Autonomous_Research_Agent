"""A run paused at HITL survives a restart: fresh graph + checkpointer on the same SQLite file."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.serde import _msgpack
from langgraph.types import Command

from autolab.agents.analysis import build_analysis_prompt, build_analysis_trace_prompt
from autolab.agents.common import measurement_facts, result_claim
from autolab.agents.execute import build_execute_trace_prompt
from autolab.monitoring.tracer import RunTracer
from autolab.orchestrator.graph import build_graph
from autolab.state.checkpointer import sqlite_saver
from autolab.state.models import HITLResolution
from autolab.state.schema import empty_state
from autolab.state.store import StateStore
from autolab.verification.llm import FakeLLMClient
from tests.fixtures.agent_helpers import register_trivial_trace
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


def seed_downstream(fake: FakeLLMClient) -> None:
    """Only what execute and analysis need: research and design must not re-run."""
    register_trivial_trace(
        fake,
        agent="execute",
        trace_purpose="execute.trace",
        trace_prompt=build_execute_trace_prompt(EXPENSIVE, RESULT),
        conclusion=result_claim(RESULT),
        facts=measurement_facts(RESULT),
    )
    fake.register("analysis.report", build_analysis_prompt(RESULT), REPORT.model_dump_json())
    register_trivial_trace(
        fake,
        agent="analysis",
        trace_purpose="analysis.trace",
        trace_prompt=build_analysis_trace_prompt(RESULT, REPORT),
        conclusion=REPORT.interpretations[0].statement,
        facts=measurement_facts(RESULT),
    )


async def run_in_fresh_process(
    db: Path, fake: FakeLLMClient, graph_input: Any, thread_id: str, store_url: str | None = None
) -> dict[str, Any]:
    """Build a new checkpointer, store and graph, run once, and drop them all."""
    store = await StateStore.create(store_url) if store_url else None
    try:
        async with sqlite_saver(db) as saver:
            graph = build_graph(
                llm=fake,
                simulator=FixedResultSimulator(RESULT),
                checkpointer=saver,
                retry_backoff_seconds=0,
                tracer=RunTracer(store) if store else None,
            )
            config = {"configurable": {"thread_id": thread_id}}
            if not isinstance(graph_input, dict):
                assert (await graph.aget_state(config)).interrupts  # paused on disk
            result: dict[str, Any] = await graph.ainvoke(graph_input, config=config)
            return result
    finally:
        if store:
            await store.dispose()


async def pause_at_design(db: Path, store_url: str | None = None) -> str:
    fake = FakeLLMClient()
    _seed_full_run(fake, EXPENSIVE)
    thread_id = str(uuid4())
    paused = await run_in_fresh_process(db, fake, empty_state(QUESTION), thread_id, store_url)
    assert paused["hitl_pending"].agent == "design"
    assert paused["hitl_payload"] == {"current_experiment": EXPENSIVE}
    return thread_id


def agents(state: dict[str, Any]) -> list[str]:
    return [t.agent for t in state["reasoning_traces"]]


@pytest.mark.parametrize("strict", [False, True])
async def test_approve_after_restart_resumes_to_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    # Strict msgpack drops unlisted types, so this also checks the allowlist.
    monkeypatch.setattr(_msgpack, "STRICT_MSGPACK_ENABLED", strict)
    db = tmp_path / "ckpt.db"
    thread_id = await pause_at_design(db)

    fake = FakeLLMClient()
    seed_downstream(fake)
    final = await run_in_fresh_process(
        db, fake, Command(resume=HITLResolution(action="approve")), thread_id
    )

    assert "__interrupt__" not in final
    assert final["hypotheses"] == [HYPOTHESIS]
    assert final["results"] == [RESULT]
    assert final["analysis_reports"] == [REPORT]
    assert final["hitl_pending"] is None
    assert agents(final) == ["research", "design", "execute", "analysis"]


async def test_retry_after_restart_reruns_the_paused_agent(tmp_path: Path) -> None:
    db = tmp_path / "ckpt.db"
    thread_id = await pause_at_design(db)
    note = "find a protocol under $10"

    fake = FakeLLMClient()
    _seed_full_run(fake)
    _seed_design(fake, DESIGN, feedback=note)
    final = await run_in_fresh_process(
        db, fake, Command(resume={"action": "retry", "note": note}), thread_id
    )

    assert final["analysis_reports"] == [REPORT]
    assert agents(final) == ["research", "design", "design", "execute", "analysis"]


async def test_abort_after_restart_ends_the_run(tmp_path: Path) -> None:
    db = tmp_path / "ckpt.db"
    thread_id = await pause_at_design(db)

    final = await run_in_fresh_process(
        db, FakeLLMClient(), Command(resume={"action": "abort"}), thread_id
    )

    assert "__interrupt__" not in final
    assert final["results"] == []
    assert final["current_agent"] == "hitl"


async def test_threads_are_isolated_in_one_checkpoint_file(tmp_path: Path) -> None:
    db = tmp_path / "ckpt.db"
    paused = await pause_at_design(db)
    fake = FakeLLMClient()
    _seed_full_run(fake)
    other = await run_in_fresh_process(db, fake, empty_state(QUESTION), str(uuid4()))

    assert other["analysis_reports"] == [REPORT]
    async with sqlite_saver(db) as saver:
        graph = build_graph(llm=FakeLLMClient(), checkpointer=saver)
        snapshot = await graph.aget_state({"configurable": {"thread_id": paused}})
    assert snapshot.next == ("hitl",)


async def test_traces_across_restart_are_persisted_once(tmp_path: Path) -> None:
    db = tmp_path / "ckpt.db"
    store_url = f"sqlite+aiosqlite:///{tmp_path / 'store.db'}"
    thread_id = await pause_at_design(db, store_url)

    fake = FakeLLMClient()
    seed_downstream(fake)
    await run_in_fresh_process(
        db, fake, Command(resume={"action": "approve"}), thread_id, store_url
    )

    store = await StateStore.create(store_url)
    try:
        run_id = (await store.list_runs())[0][0]
        events = await store.load_events(run_id)
        traces = await store.load_traces(run_id)
        assert str(run_id) == thread_id
        assert [(e.node, e.decision) for e in events] == [
            ("research", "pass"),
            ("design", "escalate"),
            ("execute", "pass"),
            ("analysis", "pass"),
        ]
        assert [t.agent for t in traces] == ["research", "design", "execute", "analysis"]
        assert await store.load_hypotheses(run_id) == [HYPOTHESIS]
        assert len(await store.load_verifications(run_id)) == 4
        assert await store.load_reports(run_id) == [REPORT]
    finally:
        await store.dispose()


async def test_unknown_thread_has_no_pending_interrupt(tmp_path: Path) -> None:
    async with sqlite_saver(tmp_path / "ckpt.db") as saver:
        graph = build_graph(llm=FakeLLMClient(), checkpointer=saver)
        snapshot = await graph.aget_state({"configurable": {"thread_id": str(uuid4())}})
    assert not snapshot.interrupts
    assert snapshot.next == ()
