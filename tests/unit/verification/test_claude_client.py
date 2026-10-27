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

from autolab.agents.common import parse_trace
from autolab.state.models import (
    AnalysisReport,
    ExperimentDesign,
    FormalizedStep,
    Hypothesis,
    Interpretation,
    ReasoningStep,
)
from autolab.verification.faithfulness import FaithfulnessVerdict
from autolab.verification.llm import (
    DEFAULT_SYSTEM_PROMPT,
    SYSTEM_PROMPTS,
    ClaudeLLMClient,
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


def test_reply_schemas_cover_the_agent_json_purposes() -> None:
    assert reply_schema("research.hypothesis") is Hypothesis
    assert reply_schema("design.experiment") is ExperimentDesign
    assert reply_schema("analysis.report") is AnalysisReport
    traces = {reply_schema(f"{a}.trace") for a in ("research", "design", "execute", "analysis")}
    assert len(traces) == 1 and None not in traces
    assert reply_schema("formalize") is None and reply_schema("commonsense") is None


async def test_json_purpose_call_goes_through_structured_output() -> None:
    hypothesis = Hypothesis(question="q", statement="s", rationale="r")
    messages = _StubMessages(hypothesis)
    raw = await _client(messages).call("research.hypothesis", "p")
    assert Hypothesis.model_validate_json(raw) == hypothesis
    ((kind, kwargs),) = messages.calls
    assert kind == "parse" and kwargs["output_format"] is Hypothesis
    assert kwargs["system"] == SYSTEM_PROMPTS["research"]


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
        Hypothesis,
        ExperimentDesign,
        AnalysisReport,
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
    built: list[str] = []

    def fake_ctor(*, api_key: str) -> SimpleNamespace:
        built.append(api_key)
        return SimpleNamespace(messages=messages)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", fake_ctor)
    client = ClaudeLLMClient("key")
    assert is_live(client)
    await client.call("commonsense", "a")
    await client.call("analysis.report", "b")
    await client.call_structured("formalize", "c", AnalysisReport)
    assert built == ["key"]
    assert [kind for kind, _ in messages.calls] == ["create", "parse", "parse"]
