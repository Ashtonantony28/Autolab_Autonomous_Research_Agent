"""Persistence round-trip tests against in-memory SQLite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio

from autolab.state.models import (
    Citation,
    ExperimentDesign,
    ExperimentParameter,
    ExperimentResult,
    Hypothesis,
    Measurement,
    ReasoningStep,
    ReasoningTrace,
    StepVerdict,
    VerificationResult,
)
from autolab.state.store import StateStore


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
