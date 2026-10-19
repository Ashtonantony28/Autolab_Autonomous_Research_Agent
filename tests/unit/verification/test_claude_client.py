"""`ClaudeLLMClient` against stubbed SDK objects: system prompts, routing, client reuse."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import anthropic
import pytest
from anthropic.lib._parse._transform import transform_schema
from anthropic.types import TextBlock
from pydantic import BaseModel

from autolab.agents.common import DESIGN_REPLY, HYPOTHESIS_REPLY, REPORT_REPLY, parse_trace
from autolab.state.models import (
    AnalysisReport,
    FormalizedStep,
    Interpretation,
    ReasoningStep,
)
from autolab.verification.faithfulness import FaithfulnessVerdict
from autolab.verification.llm import (
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT,
    SYSTEM_PROMPTS,
    ClaudeLLMClient,
    FakeLLMClient,
    StructuredOutputError,
    is_live,
    reply_schema,
    system_prompt,
)

FAMILIES = ("formalize", "faithfulness", "research", "design", "execute", "analysis")


class _StubMessages:
    """Records every `create`/`parse` call; `parse` returns `parsed`."""

    def __init__(self, parsed: Any = None, text: str = "ok") -> None:
        self.parsed = parsed
        self.text = text
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("create", kwargs))
        return SimpleNamespace(content=[TextBlock(type="text", text=self.text)])

    async def parse(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("parse", kwargs))
        return SimpleNamespace(parsed_output=self.parsed)


def _client(messages: _StubMessages) -> ClaudeLLMClient:
    return ClaudeLLMClient("k", client=SimpleNamespace(messages=messages))  # type: ignore[arg-type]


def test_one_distinct_system_prompt_per_purpose_family() -> None:
    assert set(SYSTEM_PROMPTS) == set(FAMILIES)
    assert len(set(SYSTEM_PROMPTS.values())) == len(FAMILIES)
    assert system_prompt("research.trace") == system_prompt("research.hypothesis")
    assert system_prompt("commonsense") == DEFAULT_SYSTEM_PROMPT
    for family in ("research", "design", "execute", "analysis"):
        assert "referenced_step_indices" in SYSTEM_PROMPTS[family]
    assert "back_translation" in SYSTEM_PROMPTS["faithfulness"]


@pytest.mark.parametrize("purpose", [*FAMILIES, "commonsense"])
async def test_call_structured_sends_the_purpose_system_prompt(purpose: str) -> None:
    messages = _StubMessages(FormalizedStep(assertion="P"))
    assert await _client(messages).call_structured(purpose, "prompt", FormalizedStep) == (
        FormalizedStep(assertion="P")
    )
    ((kind, kwargs),) = messages.calls
    assert kind == "parse" and kwargs["output_format"] is FormalizedStep
    assert kwargs["system"] == system_prompt(purpose)
    assert kwargs["messages"] == [{"role": "user", "content": "prompt"}]
    assert kwargs["model"] == "claude-sonnet-4-6"


async def test_plain_call_uses_create_with_system_prompt() -> None:
    messages = _StubMessages(text="hi")
    assert await _client(messages).call("commonsense", "p") == "hi"
    ((kind, kwargs),) = messages.calls
    assert kind == "create" and kwargs["system"] == DEFAULT_SYSTEM_PROMPT
    assert kwargs["messages"] == [{"role": "user", "content": "p"}]


def test_reply_schemas_cover_only_the_trace_purposes() -> None:
    # Agent payloads go through `call_structured` with content-only schemas.
    for purpose in ("research.hypothesis", "design.experiment", "analysis.report"):
        assert reply_schema(purpose) is None
    traces = {reply_schema(f"{a}.trace") for a in ("research", "design", "execute", "analysis")}
    assert len(traces) == 1 and None not in traces
    assert reply_schema("formalize") is None and reply_schema("commonsense") is None


async def test_trace_purpose_reply_parses_as_an_agent_trace() -> None:
    schema = reply_schema("design.trace")
    assert schema is not None
    steps = (ReasoningStep(index=0, claim="P", claim_type="conclusion"),)
    messages = _StubMessages(schema.model_validate({"steps": steps}))
    raw = await _client(messages).call("design.trace", "p")
    assert parse_trace("design", raw).steps == steps


@pytest.mark.parametrize(
    "schema",
    [
        FormalizedStep,
        FaithfulnessVerdict,
        HYPOTHESIS_REPLY,
        DESIGN_REPLY,
        REPORT_REPLY,
        reply_schema("x.trace"),
    ],
)
def test_schemas_transform_for_the_structured_output_api(schema: type[BaseModel]) -> None:
    out = transform_schema(schema)
    assert out["type"] == "object" and out["additionalProperties"] is False


async def test_one_async_anthropic_client_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    report = AnalysisReport(
        result_id=uuid4(),
        interpretations=(Interpretation(statement="i", rank=0, likelihood=0.9),),
        summary="s",
    )
    messages = _StubMessages(report, text="t")
    built: list[tuple[str, int]] = []

    def fake_ctor(*, api_key: str, max_retries: int) -> SimpleNamespace:
        built.append((api_key, max_retries))
        return SimpleNamespace(messages=messages)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", fake_ctor)
    client = ClaudeLLMClient("key")
    assert is_live(client)
    await client.call("commonsense", "a")
    await client.call("analysis.trace", "b")
    await client.call_structured("formalize", "c", AnalysisReport)
    assert built == [("key", 0)]  # one client, and the SDK never retries
    assert [kind for kind, _ in messages.calls] == ["create", "parse", "parse"]


async def test_missing_structured_output_names_the_stop_reason() -> None:
    class Refusing:
        async def parse(self, **_: Any) -> SimpleNamespace:
            return SimpleNamespace(parsed_output=None, stop_reason="refusal")

    client = ClaudeLLMClient("k", client=SimpleNamespace(messages=Refusing()))  # type: ignore[arg-type]
    with pytest.raises(StructuredOutputError, match="stop_reason='refusal'"):
        await client.call_structured("formalize", "p", FormalizedStep)
    assert issubclass(StructuredOutputError, RuntimeError)  # faithfulness still fails closed


def test_model_id_comes_from_one_constant() -> None:
    assert ClaudeLLMClient("k")._model == DEFAULT_MODEL


class _Wrapper:
    def __init__(self, inner: object) -> None:
        self._inner = inner


def test_is_live_follows_the_wrapper_contract() -> None:
    live = ClaudeLLMClient("k")
    assert is_live(_Wrapper(_Wrapper(live)))
    assert not is_live(_Wrapper(FakeLLMClient())) and not is_live(_Wrapper(None))
    opted_out = _Wrapper(live)
    opted_out.live = False  # type: ignore[attr-defined]  # a wrapper's own `live` wins
    assert not is_live(opted_out)
