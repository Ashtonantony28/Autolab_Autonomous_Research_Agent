"""Verifier payload binding: the trace must conclude `context.payload_claim`."""

from __future__ import annotations

from autolab.state.models import (
    ClaimType,
    FormalizedStep,
    ReasoningStep,
    ReasoningTrace,
    VerificationContext,
)
from autolab.verification.llm import FakeLLMClient
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import register_trace
from tests.fixtures.golden_traces import GoldenStep

_P = GoldenStep("P is true", "premise", "(declare-const P Bool)", "P")
_X = FormalizedStep(declarations="(declare-const x Real)", assertion="(= x 2.0)")


def _registered(
    *steps: GoldenStep, facts: tuple[FormalizedStep, ...] = ()
) -> tuple[ReasoningTrace, FakeLLMClient]:
    fake = FakeLLMClient()
    trace = register_trace(
        fake, agent="t", trace_purpose="t.trace", trace_prompt="", steps=steps, facts=facts
    )
    return trace, fake


def _unregistered(*steps: tuple[str, ClaimType]) -> ReasoningTrace:
    return ReasoningTrace(
        agent="t",
        steps=tuple(
            ReasoningStep(index=i, claim=c, claim_type=t) for i, (c, t) in enumerate(steps)
        ),
    )


async def test_matching_conclusion_verifies_normally() -> None:
    trace, fake = _registered(
        _P,
        GoldenStep("P implies Q", "premise", "(declare-const Q Bool)", "(=> P Q)"),
        GoldenStep("So Q holds", "conclusion", "", "Q", (0, 1)),
    )

    result = await Verifier(fake).verify(
        trace, context=VerificationContext(payload_claim="  so  q\tHOLDS ")
    )

    assert [v.reason for v in result.step_verdicts] == ["assumed", "assumed", "entailed"]
    assert result.consistency_score == 1.0


async def test_mismatched_conclusion_marks_every_step_ungrounded_without_formalizing() -> None:
    trace = _unregistered(("P is true", "premise"), ("So P holds", "conclusion"))

    # An empty FakeLLMClient raises KeyError on any formalize call.
    result = await Verifier(FakeLLMClient()).verify(
        trace, context=VerificationContext(payload_claim="So Q holds")
    )

    assert [(v.step_index, v.verdict, v.reason) for v in result.step_verdicts] == [
        (0, "UNGROUNDED", "no_premises"),
        (1, "UNGROUNDED", "no_premises"),
    ]
    assert result.consistency_score == 0.0
    assert "So Q holds" in result.summary
    assert "0 valid, 0 invalid, 2 ungrounded" in result.summary


async def test_near_match_is_not_accepted() -> None:
    trace = _unregistered(("P is true", "premise"), ("So P holds.", "conclusion"))

    result = await Verifier(FakeLLMClient()).verify(
        trace, context=VerificationContext(payload_claim="So P holds")
    )

    assert result.consistency_score == 0.0


async def test_last_step_must_be_a_conclusion() -> None:
    trace = _unregistered(("P is true", "premise"), ("So P holds", "inference"))

    result = await Verifier(FakeLLMClient()).verify(
        trace, context=VerificationContext(payload_claim="So P holds")
    )

    assert all(v.reason == "no_premises" for v in result.step_verdicts)
    assert result.consistency_score == 0.0


async def test_matching_conclusion_must_be_the_last_step() -> None:
    trace = _unregistered(
        ("P is true", "premise"), ("So P holds", "conclusion"), ("Also R", "inference")
    )

    result = await Verifier(FakeLLMClient()).verify(
        trace, context=VerificationContext(payload_claim="So P holds")
    )

    assert len(result.step_verdicts) == 3
    assert result.consistency_score == 0.0


async def test_empty_trace_cannot_conclude_a_payload() -> None:
    trace = ReasoningTrace(agent="t", steps=())

    result = await Verifier(FakeLLMClient()).verify(
        trace, context=VerificationContext(payload_claim="anything")
    )

    assert result.step_verdicts == ()
    assert result.consistency_score == 0.0


async def test_bound_conclusion_still_needs_entailment() -> None:
    trace, fake = _registered(
        GoldenStep("P is true", "premise", "(declare-const P Bool)\n(declare-const Q Bool)", "P"),
        GoldenStep("So Q holds", "conclusion", "", "Q", (0,)),
    )

    result = await Verifier(fake).verify(
        trace, context=VerificationContext(payload_claim="So Q holds")
    )

    assert result.step_verdicts[1].verdict == "INVALID"
    assert result.step_verdicts[1].reason == "not_entailed"


async def test_bound_conclusion_checked_against_facts() -> None:
    entailed, fake = _registered(GoldenStep("x is 2", "conclusion", "", "(> x 1.0)"), facts=(_X,))
    refuted, fake2 = _registered(GoldenStep("x is 2", "conclusion", "", "(< x 1.0)"), facts=(_X,))
    ctx = VerificationContext(facts=(_X,), payload_claim="x is 2")

    assert (await Verifier(fake).verify(entailed, context=ctx)).consistency_score == 1.0
    assert (await Verifier(fake2).verify(refuted, context=ctx)).step_verdicts[
        0
    ].verdict == "INVALID"


async def test_none_payload_claim_leaves_verification_unchanged() -> None:
    trace, fake = _registered(_P, GoldenStep("unrelated words", "conclusion", "", "P", (0,)))
    verifier = Verifier(fake)

    without = await verifier.verify(trace)
    with_none = await verifier.verify(trace, context=VerificationContext(payload_claim=None))

    assert [(v.verdict, v.reason) for v in with_none.step_verdicts] == [
        (v.verdict, v.reason) for v in without.step_verdicts
    ]
    assert with_none.consistency_score == without.consistency_score == 1.0
    assert with_none.summary == without.summary
