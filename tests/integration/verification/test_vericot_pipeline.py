"""End-to-end Verifier behaviour using FakeLLMClient."""

from __future__ import annotations

import json

from autolab.state.models import ReasoningStep, ReasoningTrace
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
    assert result.consistency_score == 0.5


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
