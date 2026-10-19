"""Recovery: with_retries, the retrying LLM client, permanent-error escalation, triggers."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from autolab.orchestrator.checkpoints import contradiction_trigger, cost_trigger
from autolab.orchestrator.recovery import (
    RetryingLLMClient,
    escalate_permanent_errors,
    with_retries,
)
from autolab.state.models import ExperimentDesign, FormalizedStep, Hypothesis
from autolab.state.schema import AutoLabState, empty_state
from autolab.verification.llm import FakeLLMClient
from autolab.verification.vericot import Verifier


async def test_succeeds_on_first_try() -> None:
    calls = 0

    async def op() -> int:
        nonlocal calls
        calls += 1
        return 42

    assert await with_retries(op) == 42
    assert calls == 1


async def test_retries_until_success() -> None:
    calls = 0

    async def flaky() -> int:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("transient")
        return 7

    result = await with_retries(flaky, initial_backoff_seconds=0.0)
    assert result == 7
    assert calls == 3


async def test_reraises_after_max_attempts() -> None:
    calls = 0

    async def always_fail() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError(f"attempt {calls}")

    with pytest.raises(RuntimeError, match="attempt 3"):
        await with_retries(always_fail, max_attempts=3, initial_backoff_seconds=0.0)
    assert calls == 3


async def test_does_not_retry_excluded_exceptions() -> None:
    async def fail_with_value_error() -> None:
        raise ValueError("bad input")

    with pytest.raises(ValueError):
        await with_retries(
            fail_with_value_error,
            max_attempts=3,
            initial_backoff_seconds=0.0,
            retry_on=RuntimeError,  # ValueError not in this set
        )


async def test_max_attempts_zero_is_rejected() -> None:
    async def op() -> None: ...

    with pytest.raises(ValueError, match="max_attempts"):
        await with_retries(op, max_attempts=0)


class _Flaky(FakeLLMClient):
    """Times out on the first call only."""

    calls = 0

    async def call(self, purpose: str, prompt: str) -> str:
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("slow")
        return await super().call(purpose, prompt)


async def test_retrying_llm_client_retries_transient_errors_only() -> None:
    inner = _Flaky({("formalize", "p"): '{"assertion": "P"}'})
    llm = RetryingLLMClient(inner, initial_backoff_seconds=0.0)

    assert await llm.call_structured("formalize", "p", FormalizedStep) == FormalizedStep(
        assertion="P"
    )
    assert inner.calls == 2
    with pytest.raises(KeyError):  # not transient: raised on the first attempt
        await llm.call("missing", "p")
    assert inner.calls == 3


async def test_escalate_permanent_errors_turns_value_error_into_hitl_request() -> None:
    async def broken(state: AutoLabState) -> dict[str, Any]:
        raise ValueError("bad payload")

    update = await escalate_permanent_errors("design", broken)(empty_state("q"))

    assert update["current_agent"] == "design"
    assert update["hitl_payload"] is None
    pending = update["hitl_pending"]
    assert pending.agent == "design"
    assert pending.reason == "ValueError: bad payload"
    assert pending.payload == {"trigger": "error", "error": "ValueError"}


async def test_escalate_permanent_errors_lets_other_errors_propagate() -> None:
    async def down(state: AutoLabState) -> dict[str, Any]:
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        await escalate_permanent_errors("design", down)(empty_state("q"))


async def test_cost_trigger_is_strictly_above_thresholds() -> None:
    check = cost_trigger(10.0, 60.0)
    design = ExperimentDesign(
        hypothesis_id=uuid4(),
        protocol="p",
        estimated_cost_usd=10.0,
        estimated_duration_minutes=60.0,
    )
    assert await check(design, empty_state("q")) is None
    over = design.model_copy(update={"estimated_cost_usd": 10.01})
    assert await check(over, empty_state("q")) == "estimated cost $10.01 > $10.00"


async def test_contradiction_trigger_skips_check_without_prior_hypotheses() -> None:
    check = contradiction_trigger(Verifier(FakeLLMClient()))  # any LLM call would KeyError
    hypothesis = Hypothesis(question="q", statement="s", rationale="r")
    assert await check(hypothesis, empty_state("q")) is None
