"""Tests for the vocabulary-growing formalizer."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from autolab.state.models import ClaimType, FormalizedStep, ReasoningStep
from autolab.verification.formalizer import (
    FormalizationError,
    Formalizer,
    Vocabulary,
    prompt_for,
)
from autolab.verification.llm import FakeLLMClient, SchemaT


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
    with pytest.raises(FormalizationError, match="assertion"):
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


async def test_formalizer_rejects_schema_violation() -> None:
    step = _step(0)
    fake = FakeLLMClient()
    fake.register(
        "formalize", prompt_for(step, Vocabulary()), json.dumps({"declarations": "", "x": 1})
    )
    with pytest.raises(FormalizationError):
        await Formalizer(fake).formalize(step)


async def test_formalizer_uses_structured_call() -> None:
    calls: list[tuple[str, type[BaseModel]]] = []

    class _Recording(FakeLLMClient):
        async def call(self, purpose: str, prompt: str) -> str:
            raise AssertionError("formalizer must use call_structured")

        async def call_structured(
            self, purpose: str, prompt: str, schema: type[SchemaT]
        ) -> SchemaT:
            calls.append((purpose, schema))
            return schema.model_validate(
                {"declarations": "(declare-const p Bool)", "assertion": "p"}
            )

    out = await Formalizer(_Recording()).formalize(_step(0))
    assert out.assertion == "p"
    assert calls == [("formalize", FormalizedStep)]


async def test_unparseable_step_leaves_vocabulary_unchanged() -> None:
    step = _step(0)
    fake = FakeLLMClient()
    fake.register(
        "formalize",
        prompt_for(step, Vocabulary()),
        json.dumps({"declarations": "(declare-const q Bool)", "assertion": "(and q"}),
    )
    f = Formalizer(fake)
    with pytest.raises(FormalizationError, match="invalid SMT"):
        await f.formalize(step)
    assert f.vocabulary.declarations == {}


async def test_conflicting_step_leaves_vocabulary_unchanged() -> None:
    step = _step(1)
    vocab = {"p": "(declare-const p Bool)"}
    fake = FakeLLMClient()
    fake.register(
        "formalize",
        prompt_for(step, Vocabulary(dict(vocab))),
        json.dumps(
            {"declarations": "(declare-const q Bool)\n(declare-const p Int)", "assertion": "q"}
        ),
    )
    f = Formalizer(fake)
    f.vocabulary = Vocabulary(dict(vocab))
    with pytest.raises(FormalizationError, match="re-declared"):
        await f.formalize(step)
    assert f.vocabulary.declarations == vocab


def test_merge_is_atomic() -> None:
    v = Vocabulary({"p": "(declare-const p Bool)"})
    with pytest.raises(FormalizationError):
        v.merge("(declare-const q Bool)\n(declare-const p Int)")
    assert v.declarations == {"p": "(declare-const p Bool)"}
    with pytest.raises(FormalizationError):
        v.merge("(declare-const r Bool)\ngarbage")
    assert v.declarations == {"p": "(declare-const p Bool)"}


def test_merged_returns_copy() -> None:
    v = Vocabulary({"p": "(declare-const p Bool)"})
    w = v.merged("(declare-const q Bool)")
    assert set(w.declarations) == {"p", "q"}
    assert set(v.declarations) == {"p"}


@pytest.mark.parametrize(
    "bad", ["declare-const p Bool", "(declare-const)", "(define-fun p () Bool true)"]
)
def test_merge_rejects_malformed_declarations(bad: str) -> None:
    v = Vocabulary()
    with pytest.raises(FormalizationError, match="unrecognized declaration"):
        v.merge(f"(declare-const ok Bool)\n\n{bad}")
    assert v.declarations == {}
