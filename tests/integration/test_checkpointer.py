"""Checkpointer resume-after-crash tests."""

from __future__ import annotations

import pytest

from autolab.state.checkpointer import Checkpointer, InMemoryCheckpointer
from autolab.state.models import (
    Hypothesis,
    ReasoningStep,
    ReasoningTrace,
    StepVerdict,
    VerificationResult,
)
from autolab.state.schema import empty_state


@pytest.mark.asyncio
async def test_in_memory_checkpointer_roundtrips_state() -> None:
    ckpt: Checkpointer = InMemoryCheckpointer()
    state = empty_state("why X?")
    h = Hypothesis(question="why X?", statement="because Y", rationale="z")
    state["hypotheses"] = [h]
    state["confidence"] = 0.7
    state["current_agent"] = "research"

    await ckpt.put("thread-1", state)

    # Simulate process crash by dropping the in-memory state.
    del state

    restored = await ckpt.get("thread-1")
    assert restored is not None
    assert restored["research_question"] == "why X?"
    assert restored["hypotheses"] == [h]
    assert restored["confidence"] == 0.7
    assert restored["current_agent"] == "research"


@pytest.mark.asyncio
async def test_in_memory_checkpointer_returns_none_for_unknown_thread() -> None:
    ckpt = InMemoryCheckpointer()
    assert await ckpt.get("nope") is None


@pytest.mark.asyncio
async def test_in_memory_checkpointer_handles_full_state() -> None:
    ckpt = InMemoryCheckpointer()
    state = empty_state("Q")
    trace = ReasoningTrace(
        agent="analysis",
        steps=(ReasoningStep(index=0, claim="c", claim_type="conclusion"),),
    )
    verdict = VerificationResult(
        trace_id=trace.id,
        step_verdicts=(StepVerdict(step_index=0, verdict="VALID"),),
        consistency_score=1.0,
    )
    state["reasoning_traces"] = [trace]
    state["verification_history"] = [verdict]

    await ckpt.put("t", state)
    restored = await ckpt.get("t")

    assert restored is not None
    assert restored["reasoning_traces"] == [trace]
    assert restored["verification_history"] == [verdict]
