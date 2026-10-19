"""End-to-end Verifier behaviour using FakeLLMClient."""

from __future__ import annotations

import json

from autolab.config import PASS_THRESHOLD
from autolab.state.models import (
    ClaimType,
    FormalizedStep,
    ReasoningStep,
    ReasoningTrace,
    VerificationContext,
)
from autolab.verification.formalizer import Vocabulary, prompt_for
from autolab.verification.llm import FakeLLMClient
from autolab.verification.vericot import Verifier


def _register_formalization(
    fake: FakeLLMClient,
    step: ReasoningStep,
    vocab: Vocabulary,
    *,
    declarations: str,
    assertion: str,
) -> Vocabulary:
    fake.register(
        "formalize",
        prompt_for(step, vocab),
        json.dumps({"declarations": declarations, "assertion": assertion}),
    )
    next_vocab = Vocabulary(dict(vocab.declarations))
    next_vocab.merge(declarations)
    return next_vocab


async def test_modus_ponens_three_steps_all_valid() -> None:
    s0 = ReasoningStep(index=0, claim="P is true", claim_type="premise")
    s1 = ReasoningStep(
        index=1, claim="P implies Q", claim_type="premise", referenced_step_indices=()
    )
    s2 = ReasoningStep(
        index=2, claim="Therefore Q", claim_type="conclusion", referenced_step_indices=(0, 1)
    )
    trace = ReasoningTrace(agent="research", steps=(s0, s1, s2))

    fake = FakeLLMClient()
    v = Vocabulary()
    v = _register_formalization(fake, s0, v, declarations="(declare-const P Bool)", assertion="P")
    v = _register_formalization(
        fake, s1, v, declarations="(declare-const Q Bool)", assertion="(=> P Q)"
    )
    v = _register_formalization(fake, s2, v, declarations="", assertion="Q")

    result = await Verifier(fake).verify(trace)

    assert [sv.verdict for sv in result.step_verdicts] == ["VALID", "VALID", "VALID"]
    assert result.consistency_score == 1.0


async def test_invalid_inference_is_caught() -> None:
    s0 = ReasoningStep(index=0, claim="P is true", claim_type="premise")
    s1 = ReasoningStep(
        index=1,
        claim="Therefore Q is true",
        claim_type="conclusion",
        referenced_step_indices=(0,),
    )
    trace = ReasoningTrace(agent="research", steps=(s0, s1))

    fake = FakeLLMClient()
    v = Vocabulary()
    v = _register_formalization(fake, s0, v, declarations="(declare-const P Bool)", assertion="P")
    v = _register_formalization(fake, s1, v, declarations="(declare-const Q Bool)", assertion="Q")

    result = await Verifier(fake).verify(trace)

    assert result.step_verdicts[0].verdict == "VALID"  # premise
    assert result.step_verdicts[1].verdict == "INVALID"
    assert result.consistency_score < PASS_THRESHOLD


async def test_malformed_formalization_yields_ungrounded() -> None:
    s0 = ReasoningStep(index=0, claim="something", claim_type="conclusion")
    trace = ReasoningTrace(agent="x", steps=(s0,))

    fake = FakeLLMClient()
    fake.register("formalize", prompt_for(s0, Vocabulary()), "this is not json")

    result = await Verifier(fake).verify(trace)
    assert result.step_verdicts[0].verdict == "UNGROUNDED"
    assert result.consistency_score == 0.5  # one UNGROUNDED out of one step


async def test_single_premise_conclusion_unrelated() -> None:
    # Premise about P, conclusion about unrelated Q -> INVALID.
    s0 = ReasoningStep(index=0, claim="P", claim_type="premise")
    s1 = ReasoningStep(index=1, claim="Q", claim_type="conclusion")
    trace = ReasoningTrace(agent="x", steps=(s0, s1))

    fake = FakeLLMClient()
    v = Vocabulary()
    v = _register_formalization(fake, s0, v, declarations="(declare-const P Bool)", assertion="P")
    v = _register_formalization(fake, s1, v, declarations="(declare-const Q Bool)", assertion="Q")

    result = await Verifier(fake).verify(trace)
    assert result.step_verdicts[1].verdict == "INVALID"


_Spec = tuple[ClaimType, str, str, tuple[int, ...]]


def _trace(specs: list[_Spec], seed_decls: str = "") -> tuple[ReasoningTrace, FakeLLMClient]:
    fake = FakeLLMClient()
    v = Vocabulary().merged(seed_decls)
    steps = []
    for i, (kind, decls, assertion, refs) in enumerate(specs):
        step = ReasoningStep(
            index=i, claim=f"step {i}", claim_type=kind, referenced_step_indices=refs
        )
        steps.append(step)
        v = _register_formalization(fake, step, v, declarations=decls, assertion=assertion)
    return ReasoningTrace(agent="x", steps=tuple(steps)), fake


async def _reasons(specs: list[_Spec], context: VerificationContext | None = None) -> list[str]:
    seed = "\n".join(f.declarations for f in context.facts) if context else ""
    trace, fake = _trace(specs, seed)
    result = await Verifier(fake).verify(trace, context=context)
    return [f"{sv.verdict}:{sv.reason}" for sv in result.step_verdicts]


_PQ = "(declare-const P Bool)\n(declare-const Q Bool)"


async def test_contradictory_premises_do_not_entail_anything() -> None:
    out = await _reasons(
        [("premise", _PQ, "P", ()), ("premise", "", "(not P)", ()), ("conclusion", "", "Q", ())]
    )
    assert out[2] == "INVALID:premises_inconsistent"


async def test_invalid_step_is_excluded_from_later_pools() -> None:
    out = await _reasons(
        [("premise", _PQ, "P", ()), ("inference", "", "Q", ()), ("conclusion", "", "Q", ())]
    )
    assert out == ["VALID:assumed", "INVALID:not_entailed", "INVALID:not_entailed"]


async def test_ungrounded_step_is_excluded_from_later_pools() -> None:
    # Step 0 has no premises (UNGROUNDED); step 2 must not be able to lean on it.
    out = await _reasons(
        [("inference", _PQ, "Q", ()), ("premise", "", "P", ()), ("conclusion", "", "Q", ())]
    )
    assert out == ["UNGROUNDED:no_premises", "VALID:assumed", "INVALID:not_entailed"]


async def test_references_restrict_premises() -> None:
    decls = _PQ + "\n(declare-const R Bool)"
    specs: list[_Spec] = [
        ("premise", decls, "P", ()),
        ("premise", "", "(=> P Q)", ()),
        ("premise", "", "R", ()),
        ("conclusion", "", "Q", (2,)),
    ]
    assert (await _reasons(specs))[3] == "INVALID:not_entailed"
    specs[3] = ("conclusion", "", "Q", (0, 1))
    assert (await _reasons(specs))[3] == "VALID:entailed"


async def test_references_include_context_facts() -> None:
    fact = FormalizedStep(declarations="(declare-const F Bool)", assertion="F")
    out = await _reasons(
        [("premise", _PQ, "P", ()), ("conclusion", "", "(and P F)", (0,))],
        context=VerificationContext(facts=(fact,)),
    )
    assert out[1] == "VALID:entailed"


async def test_citing_only_invalid_steps_is_ungrounded() -> None:
    out = await _reasons(
        [("premise", _PQ, "P", ()), ("inference", "", "Q", (0,)), ("conclusion", "", "Q", (1,))]
    )
    assert out[2] == "UNGROUNDED:no_premises"


async def test_diluted_invalid_trace_cannot_pass() -> None:
    decls = _PQ + "\n(declare-const R Bool)\n(declare-const S Bool)"
    trace, fake = _trace(
        [
            ("premise", decls, "P", ()),
            ("premise", "", "R", ()),
            ("premise", "", "S", ()),
            ("inference", "", "Q", ()),
            ("conclusion", "", "P", ()),
        ]
    )
    result = await Verifier(fake).verify(trace)
    assert [sv.verdict for sv in result.step_verdicts].count("INVALID") == 1
    assert result.consistency_score < PASS_THRESHOLD
