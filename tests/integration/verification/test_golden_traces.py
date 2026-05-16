"""The 20 golden reasoning traces — the headline DoD check for Phase 2."""

from __future__ import annotations

import json

import pytest

from autolab.state.models import ReasoningStep, ReasoningTrace
from autolab.verification.formalizer import Vocabulary, prompt_for
from autolab.verification.llm import FakeLLMClient
from autolab.verification.vericot import Verifier
from tests.fixtures.golden_traces import ALL, GoldenTrace


def _build(golden: GoldenTrace) -> tuple[ReasoningTrace, FakeLLMClient]:
    steps: list[ReasoningStep] = []
    fake = FakeLLMClient()
    vocab = Vocabulary()
    for i, gs in enumerate(golden.steps):
        step = ReasoningStep(
            index=i,
            claim=gs.claim,
            claim_type=gs.claim_type,
            referenced_step_indices=gs.referenced_step_indices,
        )
        steps.append(step)
        fake.register(
            "formalize",
            prompt_for(step, vocab),
            json.dumps({"declarations": gs.declarations, "assertion": gs.assertion}),
        )
        next_vocab = Vocabulary(dict(vocab.declarations))
        next_vocab.merge(gs.declarations)
        vocab = next_vocab
    trace = ReasoningTrace(agent="golden", steps=tuple(steps))
    return trace, fake


@pytest.mark.parametrize("golden", ALL, ids=lambda g: g.id)
async def test_golden_trace(golden: GoldenTrace) -> None:
    trace, fake = _build(golden)
    result = await Verifier(fake).verify(trace)

    actual = tuple(sv.verdict for sv in result.step_verdicts)
    assert actual == golden.expected_step_verdicts, (
        f"{golden.id}: expected {golden.expected_step_verdicts}, got {actual} "
        f"(details: {[sv.details for sv in result.step_verdicts]})"
    )

    invalid_present = any(sv.verdict == "INVALID" for sv in result.step_verdicts)
    overall = "INVALID" if invalid_present else "VALID"
    assert overall == golden.expected_overall, f"{golden.id}: overall mismatch"
