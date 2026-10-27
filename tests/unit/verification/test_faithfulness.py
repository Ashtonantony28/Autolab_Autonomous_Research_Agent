"""Faithfulness check: a judge back-translates each step's SMT and must accept it."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from autolab.config import PASS_THRESHOLD
from autolab.orchestrator.recovery import RetryingLLMClient
from autolab.state.models import ReasoningTrace
from autolab.verification.faithfulness import faithfulness_prompt
from autolab.verification.formalizer import FormalizationError, Formalizer
from autolab.verification.llm import FakeLLMClient, LLMPurpose, SchemaT, is_live
from autolab.verification.vericot import Verifier
from tests.fixtures.golden_traces import ALL, VALID, GoldenTrace, build

MODUS_PONENS = VALID[0]


class _Recording:
    """Wraps a `FakeLLMClient`, recording purposes; can fail the judge call."""

    def __init__(
        self, inner: FakeLLMClient, *, live: bool = False, judge_error: bool = False
    ) -> None:
        self.inner = inner
        self.live = live
        self.judge_error = judge_error
        self.purposes: list[LLMPurpose] = []

    async def call(self, purpose: LLMPurpose, prompt: str) -> str:
        self.purposes.append(purpose)
        return await self.inner.call(purpose, prompt)

    async def call_structured(
        self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        self.purposes.append(purpose)
        if self.judge_error and purpose == "faithfulness":
            raise RuntimeError("Claude returned no structured output")
        return await self.inner.call_structured(purpose, prompt, schema)


def _verdict(faithful: bool) -> dict[str, Any]:
    return {"back_translation": "x", "faithful": faithful, "explanation": "checked"}


async def _judged(
    golden: GoldenTrace, reply: Callable[[int], str]
) -> tuple[ReasoningTrace, FakeLLMClient]:
    """`build(golden)` plus a judge reply for every step that formalizes."""
    trace, fake = build(golden)
    formalizer = Formalizer(fake)
    for step in trace.steps:
        try:
            formula = await formalizer.formalize(step)
        except FormalizationError:
            continue
        fake.register("faithfulness", faithfulness_prompt(step, formula), reply(step.index))
    return trace, fake


@pytest.mark.parametrize("golden", ALL, ids=lambda g: g.id)
async def test_faithful_judge_leaves_every_golden_verdict_unchanged(golden: GoldenTrace) -> None:
    trace, fake = await _judged(golden, lambda _: json.dumps(_verdict(True)))
    on = await Verifier(fake, check_faithfulness=True).verify(trace)
    off = await Verifier(fake).verify(trace)
    assert [(v.verdict, v.reason) for v in on.step_verdicts] == [
        (v.verdict, v.reason) for v in off.step_verdicts
    ]
    assert on.consistency_score == off.consistency_score


async def test_unfaithful_step_is_ungrounded_and_does_not_support_later_steps() -> None:
    trace, fake = await _judged(MODUS_PONENS, lambda i: json.dumps(_verdict(i != 1)))
    result = await Verifier(fake, check_faithfulness=True).verify(trace)
    p, implication, q = result.step_verdicts
    assert (p.verdict, p.reason) == ("VALID", "assumed")
    assert (implication.verdict, implication.reason) == ("UNGROUNDED", "formalization_failed")
    assert "unfaithful formalization" in implication.details
    assert MODUS_PONENS.steps[1].claim in implication.details
    assert (q.verdict, q.reason) == ("INVALID", "not_entailed")
    assert result.consistency_score < PASS_THRESHOLD


@pytest.mark.parametrize("judge_error", [False, True], ids=["malformed", "no_output"])
async def test_judge_error_fails_closed(judge_error: bool) -> None:
    trace, fake = await _judged(MODUS_PONENS, lambda _: "not json")
    llm = _Recording(fake, judge_error=judge_error)
    result = await Verifier(llm, check_faithfulness=True).verify(trace)
    assert all(
        (v.verdict, v.reason) == ("UNGROUNDED", "formalization_failed")
        for v in result.step_verdicts
    )
    assert all("faithfulness judge failed" in v.details for v in result.step_verdicts)


async def test_flag_off_makes_no_judge_call() -> None:
    trace, fake = build(MODUS_PONENS)
    llm = _Recording(fake)
    result = await Verifier(llm).verify(trace)
    assert set(llm.purposes) == {"formalize"}
    assert all(v.verdict == "VALID" for v in result.step_verdicts)

    live = _Recording(fake, live=True)
    await Verifier(live, check_faithfulness=False).verify(trace)
    assert set(live.purposes) == {"formalize"}


async def test_check_defaults_on_for_a_live_client_even_when_wrapped() -> None:
    trace, fake = await _judged(MODUS_PONENS, lambda _: json.dumps(_verdict(True)))
    live = _Recording(fake, live=True)
    wrapped = RetryingLLMClient(live)
    assert is_live(wrapped) and not is_live(RetryingLLMClient(fake)) and not is_live(fake)
    result = await Verifier(wrapped).verify(trace)
    assert live.purposes.count("faithfulness") == len(trace.steps)
    assert result.consistency_score == 1.0
