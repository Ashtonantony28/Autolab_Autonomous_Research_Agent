"""Persistence round-trip tests against in-memory SQLite."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from autolab.monitoring.events import TraceEvent
from autolab.state.models import (
    AnalysisReport,
    Citation,
    ExperimentDesign,
    ExperimentParameter,
    ExperimentResult,
    Hypothesis,
    Interpretation,
    Measurement,
    ReasoningStep,
    ReasoningTrace,
    StepVerdict,
    VerificationResult,
)
from autolab.state.store import (
    _ROW_FOR_MODEL,
    HypothesisRow,
    RunRow,
    StateStore,
    upsert_statement,
)


@pytest_asyncio.fixture
async def store() -> AsyncIterator[StateStore]:
    s = await StateStore.create("sqlite+aiosqlite:///:memory:")
    try:
        yield s
    finally:
        await s.dispose()


@pytest.mark.asyncio
async def test_round_trip_hypothesis(store: StateStore) -> None:
    run_id = await store.create_run("why does X?")
    h = Hypothesis(
        question="why does X?",
        statement="X because Y",
        rationale="Z",
        citations=(Citation(source="arxiv", identifier="1234.5678", title="paper"),),
        confidence=0.6,
    )
    await store.append(run_id, h)
    loaded = await store.load_hypotheses(run_id)
    assert loaded == [h]


@pytest.mark.asyncio
async def test_round_trip_full_run(store: StateStore) -> None:
    run_id = await store.create_run("Q")
    h = Hypothesis(question="Q", statement="S", rationale="R")
    d = ExperimentDesign(
        hypothesis_id=h.id,
        protocol="P",
        parameters=(ExperimentParameter(name="t", value=20.0, unit="C"),),
        estimated_cost_usd=5.0,
        estimated_duration_minutes=10.0,
    )
    r = ExperimentResult(
        design_id=d.id,
        status="success",
        measurements=(Measurement(name="m", value=1.0),),
    )
    t = ReasoningTrace(
        agent="design",
        steps=(
            ReasoningStep(index=0, claim="c0", claim_type="premise"),
            ReasoningStep(
                index=1, claim="c1", claim_type="inference", referenced_step_indices=(0,)
            ),
        ),
    )
    v = VerificationResult(
        trace_id=t.id,
        step_verdicts=(
            StepVerdict(step_index=0, verdict="VALID"),
            StepVerdict(step_index=1, verdict="VALID"),
        ),
        consistency_score=1.0,
    )

    await store.append(run_id, h)
    await store.append(run_id, d)
    await store.append(run_id, r)
    await store.append(run_id, t)
    await store.append(run_id, v)

    assert await store.load_hypotheses(run_id) == [h]
    assert await store.load_traces(run_id) == [t]
    assert await store.load_verifications(run_id) == [v]


@pytest.mark.asyncio
async def test_append_rejects_unmapped_model(store: StateStore) -> None:
    run_id = await store.create_run("Q")

    class Bogus:  # pragma: no cover - intentionally unsupported
        pass

    with pytest.raises(TypeError):
        await store.append(run_id, Bogus())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_multiple_runs_are_isolated(store: StateStore) -> None:
    run_a = await store.create_run("A?")
    run_b = await store.create_run("B?")
    h_a = Hypothesis(question="A?", statement="a", rationale="r")
    h_b = Hypothesis(question="B?", statement="b", rationale="r")
    await store.append(run_a, h_a)
    await store.append(run_b, h_b)
    assert await store.load_hypotheses(run_a) == [h_a]
    assert await store.load_hypotheses(run_b) == [h_b]
    # Unrelated run id returns empty.
    assert await store.load_hypotheses(uuid4()) == []


async def test_create_run_with_given_id_and_ensure_run_is_idempotent(store: StateStore) -> None:
    rid = uuid4()
    assert await store.create_run("Q?", run_id=rid) == rid
    await store.ensure_run(rid, "ignored")
    other = uuid4()
    await store.ensure_run(other, "R?")
    await store.ensure_run(other, "R?")

    assert sorted(await store.list_runs()) == sorted([(rid, "Q?"), (other, "R?")])


async def test_list_runs_is_newest_first(store: StateStore) -> None:
    first = await store.create_run("first")
    second = await store.create_run("second")

    assert [r for r, _ in await store.list_runs()] == [second, first]


async def test_append_is_idempotent_across_store_instances(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'store.db'}"
    t = ReasoningTrace(agent="a", steps=(ReasoningStep(index=0, claim="c", claim_type="premise"),))
    v = VerificationResult(
        trace_id=t.id,
        step_verdicts=(StepVerdict(step_index=0, verdict="VALID"),),
        consistency_score=1.0,
    )
    rid = uuid4()
    for _ in range(2):  # a restart: fresh store, same rows re-recorded
        s = await StateStore.create(url)
        try:
            await s.ensure_run(rid, "Q")
            await s.append(rid, t)
            await s.append(rid, v)
            await s.append(rid, v)
        finally:
            await s.dispose()

    s = await StateStore.create(url)
    try:
        assert await s.load_traces(rid) == [t]
        assert await s.load_verifications(rid) == [v]
    finally:
        await s.dispose()


async def test_round_trip_reports_and_events_in_order(store: StateStore) -> None:
    rid = await store.create_run("Q")
    report = AnalysisReport(
        result_id=uuid4(),
        interpretations=(Interpretation(statement="x", rank=0, likelihood=0.5),),
        summary="s",
    )
    events = [
        TraceEvent(run_id=rid, node=n, attempt=1, latency_ms=1.0, decision="pass")
        for n in ("research", "design", "execute")
    ]
    await store.append(rid, report)
    for e in reversed(events):
        await store.append(rid, e)

    assert await store.load_reports(rid) == [report]
    assert await store.load_events(rid) == events  # by started_at, not insertion
    assert await store.load_events(uuid4()) == []


# --- SG9: (run_id, id) keys and race-free upserts ------------------------------

_T = ReasoningTrace(agent="a", steps=(ReasoningStep(index=0, claim="c", claim_type="premise"),))
SHARED_ID_ENTITIES = [
    Hypothesis(question="Q", statement="S", rationale="R"),
    ExperimentDesign(
        hypothesis_id=uuid4(), protocol="P", estimated_cost_usd=1.0, estimated_duration_minutes=1.0
    ),
    ExperimentResult(design_id=uuid4(), status="success"),
    _T,
    VerificationResult(
        trace_id=_T.id,
        step_verdicts=(StepVerdict(step_index=0, verdict="VALID"),),
        consistency_score=1.0,
    ),
    AnalysisReport(
        result_id=uuid4(),
        interpretations=(Interpretation(statement="x", rank=0, likelihood=0.5),),
        summary="s",
    ),
    TraceEvent(run_id=uuid4(), node="research", attempt=1, latency_ms=1.0, decision="pass"),
]


async def _rows(store: StateStore, run_id: UUID, model: BaseModel) -> list[dict[str, Any]]:
    row_cls = _ROW_FOR_MODEL[type(model)]
    async with store.session() as session:
        rows = await session.scalars(select(row_cls).where(row_cls.run_id == str(run_id)))
        return [r.payload for r in rows]


@pytest_asyncio.fixture
async def file_store(tmp_path: Path) -> AsyncIterator[StateStore]:
    s = await StateStore.create(f"sqlite+aiosqlite:///{tmp_path / 'store.db'}")
    try:
        yield s
    finally:
        await s.dispose()


@pytest.mark.parametrize("entity", SHARED_ID_ENTITIES, ids=lambda m: type(m).__name__)
async def test_one_id_in_two_runs_is_two_rows(store: StateStore, entity: BaseModel) -> None:
    run_a, run_b = await store.create_run("a"), await store.create_run("b")
    await store.append(run_a, entity)
    await store.append(run_b, entity)

    expected = [entity.model_dump(mode="json")]
    assert await _rows(store, run_a, entity) == expected
    assert await _rows(store, run_b, entity) == expected


async def test_rewriting_an_id_in_one_run_leaves_the_other_run_untouched(store: StateStore) -> None:
    h = Hypothesis(question="Q", statement="first", rationale="R")
    run_a, run_b = await store.create_run("a"), await store.create_run("b")
    await store.append(run_a, h)
    await store.append(run_b, h)
    updated = h.model_copy(update={"statement": "second"})
    await store.append(run_b, updated)

    assert await store.load_hypotheses(run_a) == [h]
    assert await store.load_hypotheses(run_b) == [updated]  # updated in place, not duplicated


async def test_concurrent_appends_across_runs_are_race_free(file_store: StateStore) -> None:
    runs = [uuid4() for _ in range(5)]
    hypotheses = [Hypothesis(question="Q", statement=f"s{i}", rationale="R") for i in range(5)]
    writes = [(r, h) for r in runs for h in hypotheses]  # 25 distinct keys, each written twice

    outs = await asyncio.gather(
        *(file_store.ensure_run(r, "Q") for r in runs for _ in range(10)),
        *(file_store.append(r, h) for r, h in writes * 2),
        return_exceptions=True,
    )

    assert [o for o in outs if isinstance(o, BaseException)] == []
    assert sorted(r for r, _ in await file_store.list_runs()) == sorted(runs)
    for r in runs:
        loaded = await file_store.load_hypotheses(r)
        assert sorted(h.statement for h in loaded) == sorted(h.statement for h in hypotheses)


async def test_concurrent_upserts_of_one_key_store_one_row(file_store: StateStore) -> None:
    run = await file_store.create_run("Q")
    h = Hypothesis(question="Q", statement="S", rationale="R")
    await asyncio.gather(*(file_store.append(run, h) for _ in range(50)))
    assert await file_store.load_hypotheses(run) == [h]


async def test_ensure_run_never_overwrites_an_existing_run(store: StateStore) -> None:
    rid = await store.create_run("original")
    await asyncio.gather(*(store.ensure_run(rid, f"other {i}") for i in range(20)))
    assert await store.list_runs() == [(rid, "original")]


DIALECTS = [sqlite.dialect(), postgresql.dialect()]  # type: ignore[no-untyped-call]


@pytest.mark.parametrize("dialect", DIALECTS, ids=lambda d: d.name)
def test_upserts_compile_to_on_conflict_for_each_dialect(dialect: Any) -> None:
    values: dict[str, Any] = {"id": "i", "run_id": "r", "created_at": None, "payload": {}}
    entity = str(upsert_statement(dialect.name, HypothesisRow, values).compile(dialect=dialect))
    run_values = {"id": "r", "research_question": "q", "created_at": None}
    run = str(upsert_statement(dialect.name, RunRow, run_values).compile(dialect=dialect))

    assert "ON CONFLICT (run_id, id) DO UPDATE SET created_at" in entity
    assert "payload = excluded.payload" in entity
    assert "ON CONFLICT (id) DO NOTHING" in run


def test_upsert_rejects_a_dialect_without_on_conflict() -> None:
    with pytest.raises(ValueError, match="unsupported database dialect 'mysql'"):
        upsert_statement("mysql", RunRow, {"id": "r"})


def test_entity_tables_are_keyed_by_run_and_id() -> None:
    for row_cls in set(_ROW_FOR_MODEL.values()):
        assert [c.name for c in row_cls.__table__.primary_key] == ["run_id", "id"]
