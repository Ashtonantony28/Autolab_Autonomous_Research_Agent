"""AUDIT-3 repros: API and SDK consistency (see AUDIT-3.md).

Each test asserts the *correct* behaviour and is a strict xfail, so it
starts passing (and fails the suite) once the finding is fixed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from anthropic._exceptions import OverloadedError  # not re-exported from `anthropic`
from anthropic.lib._parse._transform import transform_schema
from pydantic import BaseModel

from autolab.agents.verification_monitor import AgentDecision, VerificationMonitor
from autolab.config import Settings
from autolab.orchestrator.graph import build_graph
from autolab.orchestrator.recovery import RetryingLLMClient
from autolab.state.checkpointer import InMemoryCheckpointer
from autolab.state.models import (
    AnalysisReport,
    ExperimentDesign,
    Hypothesis,
    ReasoningStep,
    StepVerdict,
    VerificationResult,
)
from autolab.state.schema import empty_state
from autolab.state.store import StateStore
from autolab.verification.confidence import consistency_score
from autolab.verification.formalizer import FormalizationError, Formalizer
from autolab.verification.llm import ClaudeLLMClient, LLMPurpose, SchemaT
from autolab.verification.vericot import Verifier


@pytest_asyncio.fixture
async def store() -> AsyncIterator[StateStore]:
    s = await StateStore.create("sqlite+aiosqlite:///:memory:")
    try:
        yield s
    finally:
        await s.dispose()


class _RefusingMessages:
    """`messages.parse` stub for a reply with no structured output (e.g. a refusal)."""

    async def parse(self, **_: Any) -> SimpleNamespace:
        return SimpleNamespace(parsed_output=None)


def _refusing_claude() -> ClaudeLLMClient:
    return ClaudeLLMClient("k", client=SimpleNamespace(messages=_RefusingMessages()))  # type: ignore[arg-type]


class _RefusingLLM:
    """An LLMClient whose every call fails like `ClaudeLLMClient` on a refusal."""

    async def call(self, purpose: LLMPurpose, prompt: str) -> str:
        raise RuntimeError(f"Claude returned no structured output for purpose={purpose!r}")

    async def call_structured(
        self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        raise RuntimeError(f"Claude returned no structured output for purpose={purpose!r}")


# --- #1 (high): the INVALID cap ignores Settings.pass_threshold ---------------


def test_refuted_trace_never_passes_under_configured_pass_threshold() -> None:
    cfg = Settings(pass_threshold=0.5)
    verdicts = [StepVerdict(step_index=i, verdict="VALID", reason="entailed") for i in range(9)]
    verdicts.append(StepVerdict(step_index=9, verdict="INVALID", reason="not_entailed"))
    result = VerificationResult(
        trace_id=uuid4(),
        step_verdicts=tuple(verdicts),
        consistency_score=consistency_score(verdicts),
    )
    monitor = VerificationMonitor(
        pass_threshold=cfg.pass_threshold, retry_threshold=cfg.retry_threshold
    )
    # ADR-0007: a refuted step can never be diluted into a pass. The agent gate
    # (`hitl_threshold=cfg.pass_threshold`) makes the same comparison.
    assert result.consistency_score < cfg.pass_threshold
    assert monitor.decide(result) is not AgentDecision.PASS


# --- #2 (high): non-ValueError LLM failures crash the graph -------------------


async def test_formalizer_turns_a_refusal_into_formalization_error() -> None:
    formalizer = Formalizer(_refusing_claude())
    step = ReasoningStep(index=0, claim="P holds", claim_type="premise")
    with pytest.raises(FormalizationError):
        await formalizer.formalize(step)


@pytest.mark.xfail(strict=True, reason="AUDIT-3 #2")
async def test_llm_refusal_pauses_at_hitl_instead_of_crashing_the_graph() -> None:
    graph = build_graph(llm=_RefusingLLM(), retry_backoff_seconds=0.0)
    config = {"configurable": {"thread_id": str(uuid4())}}
    final = await graph.ainvoke(empty_state("q"), config=config)
    assert "__interrupt__" in final


# --- #3 (high): the LLM authors entity ids and timestamps ---------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-3 #3")
@pytest.mark.parametrize("schema", [Hypothesis, ExperimentDesign, AnalysisReport])
def test_structured_output_schema_does_not_let_the_model_pick_ids(
    schema: type[BaseModel],
) -> None:
    sent = transform_schema(schema)  # what `messages.parse(output_format=schema)` sends
    assert "id" not in sent["properties"]
    assert "created_at" not in sent["properties"]


@pytest.mark.xfail(strict=True, reason="AUDIT-3 #3")
async def test_an_id_reused_by_another_run_does_not_move_this_runs_row(
    store: StateStore,
) -> None:
    # Two runs whose LLM replied with the same (model-chosen) hypothesis id.
    shared = uuid4()
    run_a, run_b = await store.create_run("qa"), await store.create_run("qb")
    await store.append(run_a, Hypothesis(id=shared, question="qa", statement="A", rationale="r"))
    await store.append(run_b, Hypothesis(id=shared, question="qb", statement="B", rationale="r"))
    assert [h.statement for h in await store.load_hypotheses(run_a)] == ["A"]


# --- #4 (medium): 529 Overloaded is not treated as transient ------------------


class _OverloadedOnce:
    def __init__(self) -> None:
        self.calls = 0

    async def call(self, purpose: LLMPurpose, prompt: str) -> str:
        self.calls += 1
        if self.calls == 1:
            response = httpx.Response(529, request=httpx.Request("POST", "https://api.test"))
            raise OverloadedError("overloaded", response=response, body=None)
        return "ok"

    async def call_structured(
        self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        raise NotImplementedError


@pytest.mark.xfail(strict=True, reason="AUDIT-3 #4")
async def test_overloaded_error_is_retried() -> None:
    inner = _OverloadedOnce()
    client = RetryingLLMClient(inner, initial_backoff_seconds=0.0)
    assert await client.call("research.trace", "p") == "ok"


# --- #6 (medium): the exported Checkpointer loses typed hitl_payload ----------


@pytest.mark.xfail(strict=True, reason="AUDIT-3 #6")
async def test_checkpointer_round_trips_withheld_payload_models() -> None:
    hypothesis = Hypothesis(question="q", statement="s", rationale="r")
    state = empty_state("q")
    state["hitl_payload"] = {"hypotheses": [hypothesis]}
    ckpt = InMemoryCheckpointer()
    await ckpt.put("t", state)
    restored = await ckpt.get("t")
    assert restored is not None
    payload = restored.get("hitl_payload") or {}
    assert payload["hypotheses"] == [hypothesis]


# --- #8 (low): faithfulness fails open behind any other wrapper ---------------


class _PassThrough:
    """A trivial logging-style LLMClient decorator."""

    def __init__(self, inner: ClaudeLLMClient) -> None:
        self._inner = inner

    async def call(self, purpose: LLMPurpose, prompt: str) -> str:
        return await self._inner.call(purpose, prompt)

    async def call_structured(
        self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        return await self._inner.call_structured(purpose, prompt, schema)


@pytest.mark.xfail(strict=True, reason="AUDIT-3 #8")
def test_faithfulness_stays_on_for_a_wrapped_live_client() -> None:
    verifier = Verifier(_PassThrough(_refusing_claude()))
    assert verifier._check_faithfulness is True
