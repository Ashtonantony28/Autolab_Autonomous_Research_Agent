"""RunTracer end-to-end against an in-memory SQLite store."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio

from autolab.monitoring.tracer import RunTracer
from autolab.state.models import (
    ExperimentDesign,
    ExperimentResult,
    Hypothesis,
    Measurement,
    ReasoningStep,
    ReasoningTrace,
    StepVerdict,
    VerificationResult,
)
from autolab.state.schema import empty_state
from autolab.state.store import StateStore


@pytest_asyncio.fixture
async def store() -> AsyncIterator[StateStore]:
    s = await StateStore.create("sqlite+aiosqlite:///:memory:")
    try:
        yield s
    finally:
        await s.dispose()


@pytest.mark.asyncio
async def test_tracer_persists_each_entity_exactly_once(store: StateStore) -> None:
    run_id = await store.create_run("Q?")
    tracer = RunTracer(store)

    h = Hypothesis(question="Q?", statement="S", rationale="R")
    d = ExperimentDesign(
        hypothesis_id=h.id,
        protocol="P",
        estimated_cost_usd=1.0,
        estimated_duration_minutes=1.0,
    )
    r = ExperimentResult(
        design_id=d.id, status="success", measurements=(Measurement(name="m", value=1.0),)
    )
    t = ReasoningTrace(
        agent="research",
        steps=(ReasoningStep(index=0, claim="c", claim_type="premise"),),
    )
    v = VerificationResult(
        trace_id=t.id,
        step_verdicts=(StepVerdict(step_index=0, verdict="VALID"),),
        consistency_score=1.0,
    )

    state = empty_state("Q?")
    state["hypotheses"] = [h]
    state["current_experiment"] = d
    state["results"] = [r]
    state["reasoning_traces"] = [t]
    state["verification_history"] = [v]

    appended_first = await tracer.record_state(run_id, state)
    appended_second = await tracer.record_state(run_id, state)  # idempotent

    assert appended_first == 5  # h, d, r, t, v
    assert appended_second == 0

    assert await store.load_hypotheses(run_id) == [h]
    assert await store.load_traces(run_id) == [t]
    assert await store.load_verifications(run_id) == [v]


@pytest.mark.asyncio
async def test_tracer_records_new_entities_only(store: StateStore) -> None:
    run_id = await store.create_run("Q?")
    tracer = RunTracer(store)

    state = empty_state("Q?")
    h1 = Hypothesis(question="Q?", statement="S1", rationale="R")
    state["hypotheses"] = [h1]
    assert await tracer.record_state(run_id, state) == 1

    h2 = Hypothesis(question="Q?", statement="S2", rationale="R")
    state["hypotheses"] = [h1, h2]
    assert await tracer.record_state(run_id, state) == 1

    assert sorted(h.statement for h in await store.load_hypotheses(run_id)) == ["S1", "S2"]


@pytest.mark.asyncio
async def test_tracer_isolates_runs(store: StateStore) -> None:
    run_a = await store.create_run("A?")
    run_b = await store.create_run("B?")
    tracer = RunTracer(store)

    state_a = empty_state("A?")
    state_a["hypotheses"] = [Hypothesis(question="A?", statement="SA", rationale="R")]
    state_b = empty_state("B?")
    state_b["hypotheses"] = [Hypothesis(question="B?", statement="SB", rationale="R")]

    await tracer.record_state(run_a, state_a)
    await tracer.record_state(run_b, state_b)

    a_loaded = await store.load_hypotheses(run_a)
    b_loaded = await store.load_hypotheses(run_b)
    assert [h.statement for h in a_loaded] == ["SA"]
    assert [h.statement for h in b_loaded] == ["SB"]
    assert run_a != run_b
    _ = uuid4()  # silence unused import warning
