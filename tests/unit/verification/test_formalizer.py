"""Tests for the vocabulary-growing formalizer."""

from __future__ import annotations

import json

import pytest

from autolab.state.models import ClaimType, ReasoningStep
from autolab.verification.formalizer import (
    FormalizationError,
    Formalizer,
    Vocabulary,
    prompt_for,
)
from autolab.verification.llm import FakeLLMClient


def _step(index: int, claim: str = "claim", claim_type: ClaimType = "inference") -> ReasoningStep:
    return ReasoningStep(index=index, claim=claim, claim_type=claim_type)


async def test_formalizer_returns_formula_and_accumulates_vocab() -> None:
    step0 = _step(0, "p is true", "premise")
    step1 = _step(1, "q follows from p", "inference")

    fake = FakeLLMClient()
    voc_empty = Vocabulary()
    fake.register(
        "formalize",
        prompt_for(step0, voc_empty),
        json.dumps({"declarations": "(declare-const p Bool)", "assertion": "p"}),
    )
    voc_one = Vocabulary({"p": "(declare-const p Bool)"})
    fake.register(
        "formalize",
        prompt_for(step1, voc_one),
        json.dumps({"declarations": "(declare-const q Bool)", "assertion": "(=> p q)"}),
    )

    f = Formalizer(fake)
    out0 = await f.formalize(step0)
    out1 = await f.formalize(step1)

    assert "p Bool" in out0.declarations
    assert out0.assertion == "p"
    # Vocabulary grew across calls and is included in step1's declarations.
    assert "p Bool" in out1.declarations
    assert "q Bool" in out1.declarations
    assert set(f.vocabulary.declarations.keys()) == {"p", "q"}


async def test_formalizer_rejects_non_json() -> None:
    step = _step(0)
    fake = FakeLLMClient()
    fake.register("formalize", prompt_for(step, Vocabulary()), "not json at all")
    with pytest.raises(FormalizationError, match="JSON"):
        await Formalizer(fake).formalize(step)


async def test_formalizer_rejects_empty_assertion() -> None:
    step = _step(0)
    fake = FakeLLMClient()
    fake.register(
        "formalize",
        prompt_for(step, Vocabulary()),
        json.dumps({"declarations": "", "assertion": "   "}),
    )
    with pytest.raises(FormalizationError, match="empty assertion"):
        await Formalizer(fake).formalize(step)


async def test_formalizer_rejects_non_object() -> None:
    step = _step(0)
    fake = FakeLLMClient()
    fake.register("formalize", prompt_for(step, Vocabulary()), json.dumps(["array"]))
    with pytest.raises(FormalizationError, match="object"):
        await Formalizer(fake).formalize(step)


async def test_formalizer_rejects_unknown_decl_form() -> None:
    step = _step(0)
    fake = FakeLLMClient()
    fake.register(
        "formalize",
        prompt_for(step, Vocabulary()),
        json.dumps({"declarations": "garbage line", "assertion": "true"}),
    )
    with pytest.raises(FormalizationError, match="unrecognized declaration"):
        await Formalizer(fake).formalize(step)


async def test_formalizer_rejects_conflicting_redeclaration() -> None:
    step0 = _step(0)
    step1 = _step(1)
    fake = FakeLLMClient()
    fake.register(
        "formalize",
        prompt_for(step0, Vocabulary()),
        json.dumps({"declarations": "(declare-const p Bool)", "assertion": "p"}),
    )
    fake.register(
        "formalize",
        prompt_for(step1, Vocabulary({"p": "(declare-const p Bool)"})),
        json.dumps({"declarations": "(declare-const p Int)", "assertion": "(> p 0)"}),
    )
    f = Formalizer(fake)
    await f.formalize(step0)
    with pytest.raises(FormalizationError, match="re-declared"):
        await f.formalize(step1)


async def test_formalizer_rejects_invalid_smt() -> None:
    step = _step(0)
    fake = FakeLLMClient()
    fake.register(
        "formalize",
        prompt_for(step, Vocabulary()),
        json.dumps({"declarations": "(declare-const p Bool)", "assertion": "(this is broken"}),
    )
    with pytest.raises(FormalizationError, match="invalid SMT"):
        await Formalizer(fake).formalize(step)
