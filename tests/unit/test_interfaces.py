"""SG0 shared-interface contracts (PLAN.md)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from autolab.config import (
    MAX_RETRIES,
    PASS_THRESHOLD,
    RETRY_THRESHOLD,
    STEP_BUDGET_MS,
    Settings,
)
from autolab.state.checkpointer import InMemoryCheckpointer
from autolab.state.models import (
    AnalysisReport,
    FormalizedStep,
    HITLResolution,
    Interpretation,
    ReasoningStep,
    ReasoningTrace,
    StepVerdict,
    VerificationContext,
)
from autolab.state.schema import empty_state
from autolab.verification.formalizer import Vocabulary, prompt_for
from autolab.verification.llm import ClaudeLLMClient, FakeLLMClient, LLMClient
from autolab.verification.vericot import Verifier


def test_formalized_step_roundtrips_and_rejects_empty_assertion() -> None:
    f = FormalizedStep(declarations="(declare-const P Bool)", assertion="P")
    assert FormalizedStep.model_validate_json(f.model_dump_json()) == f
    with pytest.raises(ValidationError):
        FormalizedStep(assertion="  ")


def test_verification_context_roundtrips() -> None:
    ctx = VerificationContext(
        facts=(FormalizedStep(declarations="(declare-const x Int)", assertion="(> x 0)"),),
        payload_claim="x is positive",
    )
    assert VerificationContext.model_validate_json(ctx.model_dump_json()) == ctx
    assert VerificationContext().facts == ()


def test_step_verdict_reason_and_timing_fields() -> None:
    v = StepVerdict(step_index=0, verdict="INVALID", reason="premises_inconsistent", solver_ms=1.5)
    assert StepVerdict.model_validate_json(v.model_dump_json()) == v
    assert StepVerdict(step_index=0, verdict="VALID").reason is None
    with pytest.raises(ValidationError):
        StepVerdict(step_index=0, verdict="VALID", reason="vibes")
    with pytest.raises(ValidationError):
        StepVerdict(step_index=0, verdict="VALID", solver_ms=-1.0)


def test_hitl_resolution_actions() -> None:
    for action in ("approve", "retry", "abort"):
        r = HITLResolution.model_validate({"action": action})
        assert r.action == action and r.note == ""
    with pytest.raises(ValidationError):
        HITLResolution.model_validate({"action": "ignore"})


def test_settings_threshold_defaults(settings: Settings) -> None:
    assert settings.pass_threshold == PASS_THRESHOLD == 0.7
    assert settings.retry_threshold == RETRY_THRESHOLD == 0.4
    assert settings.max_retries == MAX_RETRIES
    assert settings.hitl_cost_usd == 10.0
    assert settings.hitl_duration_min == 60.0
    assert settings.solver_timeout_s == 5.0
    assert settings.step_budget_ms == STEP_BUDGET_MS


def test_settings_reject_out_of_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PASS_THRESHOLD", "1.5")
    with pytest.raises(ValidationError):
        Settings()


def test_thresholds_have_one_source() -> None:
    from autolab.agents import base, verification_monitor
    from autolab.orchestrator import routing

    assert base.HITL_THRESHOLD is PASS_THRESHOLD
    assert routing.PASS_THRESHOLD is PASS_THRESHOLD
    assert verification_monitor.PASS_THRESHOLD is PASS_THRESHOLD
    assert verification_monitor.RETRY_THRESHOLD is RETRY_THRESHOLD


def test_empty_state_has_new_fields() -> None:
    s = empty_state("q")
    assert s["analysis_reports"] == []
    assert s["hitl_payload"] is None
    assert s["retry_count"] == 0
    assert s["feedback"] == ""


@pytest.mark.asyncio
async def test_checkpointer_roundtrips_new_fields() -> None:
    ckpt = InMemoryCheckpointer()
    state = empty_state("q")
    report = AnalysisReport.model_validate(
        {
            "result_id": "00000000-0000-0000-0000-000000000001",
            "interpretations": [Interpretation(statement="s", rank=0, likelihood=0.5)],
            "summary": "ok",
        }
    )
    state["analysis_reports"] = [report]
    state["hitl_payload"] = {"kind": "ExperimentDesign", "data": {"protocol": "p"}}
    state["retry_count"] = 2
    state["feedback"] = "step 1 not entailed"
    await ckpt.put("t", state)
    restored = await ckpt.get("t")
    assert restored is not None
    assert restored["analysis_reports"] == [report]
    assert restored["hitl_payload"] == state["hitl_payload"]
    assert restored["retry_count"] == 2
    assert restored["feedback"] == "step 1 not entailed"


@pytest.mark.asyncio
async def test_fake_call_structured_validates_against_schema() -> None:
    fake = FakeLLMClient()
    fake.register("formalize", "p", json.dumps({"declarations": "", "assertion": "P"}))
    fake.register("formalize", "bad", json.dumps({"declarations": ""}))
    assert isinstance(fake, LLMClient)
    out = await fake.call_structured("formalize", "p", FormalizedStep)
    assert out == FormalizedStep(assertion="P")
    with pytest.raises(ValidationError):
        await fake.call_structured("formalize", "bad", FormalizedStep)
    with pytest.raises(KeyError):
        await fake.call_structured("formalize", "missing", FormalizedStep)


class _StubMessages:
    def __init__(self, parsed: Any) -> None:
        self.parsed = parsed
        self.kwargs: dict[str, Any] = {}

    async def parse(self, **kwargs: Any) -> SimpleNamespace:
        self.kwargs = kwargs
        return SimpleNamespace(parsed_output=self.parsed)


@pytest.mark.asyncio
async def test_claude_call_structured_uses_output_format() -> None:
    messages = _StubMessages(FormalizedStep(assertion="P"))
    client = ClaudeLLMClient("k", client=SimpleNamespace(messages=messages))  # type: ignore[arg-type]
    assert isinstance(client, LLMClient)
    out = await client.call_structured("formalize", "prompt", FormalizedStep)
    assert out == FormalizedStep(assertion="P")
    assert messages.kwargs["output_format"] is FormalizedStep
    assert messages.kwargs["model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_claude_call_structured_raises_without_parsed_output() -> None:
    client = ClaudeLLMClient("k", client=SimpleNamespace(messages=_StubMessages(None)))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="no structured output"):
        await client.call_structured("formalize", "prompt", FormalizedStep)


def _register(
    fake: FakeLLMClient, step: ReasoningStep, vocab: Vocabulary, decls: str, a: str
) -> None:
    fake.register(
        "formalize", prompt_for(step, vocab), json.dumps({"declarations": decls, "assertion": a})
    )


@pytest.mark.asyncio
async def test_verify_uses_context_facts_as_premises() -> None:
    fact = FormalizedStep(declarations="(declare-const x Int)", assertion="(> x 0)")
    step = ReasoningStep(index=0, claim="x + 1 > 1", claim_type="conclusion")
    trace = ReasoningTrace(agent="t", steps=(step,))
    fake = FakeLLMClient()
    vocab = Vocabulary()
    vocab.merge(fact.declarations)
    _register(fake, step, vocab, "", "(> (+ x 1) 1)")

    result = await Verifier(fake).verify(trace, context=VerificationContext(facts=(fact,)))
    (v,) = result.step_verdicts
    assert v.verdict == "VALID" and v.reason == "entailed" and v.solver_ms > 0

    no_ctx = FakeLLMClient()
    _register(no_ctx, step, Vocabulary(), "(declare-const x Int)", "(> (+ x 1) 1)")
    (u,) = (await Verifier(no_ctx).verify(trace)).step_verdicts
    assert u.verdict == "UNGROUNDED" and u.reason == "no_premises"


@pytest.mark.asyncio
async def test_verify_populates_reasons() -> None:
    steps = (
        ReasoningStep(index=0, claim="P", claim_type="premise"),
        ReasoningStep(index=1, claim="Q", claim_type="conclusion"),
        ReasoningStep(index=2, claim="garbage", claim_type="conclusion"),
    )
    fake = FakeLLMClient()
    vocab = Vocabulary()
    decls = "(declare-const P Bool)\n(declare-const Q Bool)"
    _register(fake, steps[0], vocab, decls, "P")
    vocab.merge(decls)
    _register(fake, steps[1], vocab, "", "Q")
    fake.register("formalize", prompt_for(steps[2], vocab), "not json")

    result = await Verifier(fake).verify(ReasoningTrace(agent="t", steps=steps))
    assert [(v.verdict, v.reason) for v in result.step_verdicts] == [
        ("VALID", "assumed"),
        ("INVALID", "not_entailed"),
        ("UNGROUNDED", "formalization_failed"),
    ]


class _StubCreate:
    def __init__(self, content: list[Any]) -> None:
        self.content = content

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(content=self.content)


@pytest.mark.asyncio
async def test_claude_call_returns_first_text_block() -> None:
    from anthropic.types import TextBlock

    stub = _StubCreate([TextBlock(type="text", text="hi")])
    client = ClaudeLLMClient("k", client=SimpleNamespace(messages=stub))  # type: ignore[arg-type]
    assert await client.call("formalize", "p") == "hi"
    empty = ClaudeLLMClient("k", client=SimpleNamespace(messages=_StubCreate([])))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="no text content"):
        await empty.call("formalize", "p")


def test_claude_client_is_built_once_lazily() -> None:
    client = ClaudeLLMClient("k")
    assert client._get_client() is client._get_client()
