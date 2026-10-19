"""Error handling: retry-with-backoff for transient failures, HITL for permanent ones."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Final, Protocol, TypeVar

import anthropic

from autolab.state.models import HITLRequest
from autolab.state.schema import AutoLabState
from autolab.verification.llm import LLMClient, LLMPurpose, SchemaT

T = TypeVar("T")

TRANSIENT_ERRORS: Final[tuple[type[BaseException], ...]] = (
    anthropic.APIConnectionError,  # includes APITimeoutError
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    ConnectionError,
    TimeoutError,
)
"""Errors worth retrying. Anything else (e.g. `ValidationError`) fails the same way again."""

INITIAL_BACKOFF_S: Final[float] = 0.5
MAX_ATTEMPTS: Final[int] = 3


async def with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = MAX_ATTEMPTS,
    initial_backoff_seconds: float = INITIAL_BACKOFF_S,
    backoff_multiplier: float = 2.0,
    retry_on: type[BaseException] | tuple[type[BaseException], ...] = Exception,
) -> T:
    """Invoke `operation` up to `max_attempts` times with exponential backoff.

    Re-raises the original exception if every attempt fails. Use only for
    transient errors (network timeout, rate limit) — *don't* retry on
    validation errors or anything else that would just fail the same way.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    delay = initial_backoff_seconds
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except retry_on as exc:
            last_exc = exc
            if attempt == max_attempts:
                raise
            await asyncio.sleep(delay)
            delay *= backoff_multiplier
    # Unreachable: loop either returns or re-raises.
    assert last_exc is not None
    raise last_exc


class RetryingLLMClient:
    """`LLMClient` decorator that retries `TRANSIENT_ERRORS` with backoff."""

    def __init__(
        self,
        inner: LLMClient,
        *,
        max_attempts: int = MAX_ATTEMPTS,
        initial_backoff_seconds: float = INITIAL_BACKOFF_S,
    ) -> None:
        self._inner = inner
        self._max_attempts = max_attempts
        self._backoff = initial_backoff_seconds

    async def call(self, purpose: LLMPurpose, prompt: str) -> str:
        """`inner.call`, retried on transient errors."""
        return await self._retry(lambda: self._inner.call(purpose, prompt))

    async def call_structured(
        self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        """`inner.call_structured`, retried on transient errors."""
        return await self._retry(lambda: self._inner.call_structured(purpose, prompt, schema))

    async def _retry(self, operation: Callable[[], Awaitable[T]]) -> T:
        return await with_retries(
            operation,
            max_attempts=self._max_attempts,
            initial_backoff_seconds=self._backoff,
            retry_on=TRANSIENT_ERRORS,
        )


class AgentNode(Protocol):
    """An async LangGraph node returning a partial `AutoLabState` update."""

    async def __call__(self, state: AutoLabState) -> dict[str, Any]: ...


def escalate_permanent_errors(name: str, node: AgentNode) -> AgentNode:
    """Wrap an agent node so a permanent error pauses at HITL instead of crashing the graph.

    `ValueError` covers Pydantic's `ValidationError` (malformed LLM output),
    `FormalizationError` and the agents' own input checks: retrying the same
    call would fail the same way. Transient errors are retried inside the LLM
    client; once exhausted they propagate, and the checkpoint allows a resume.
    """

    async def guarded(state: AutoLabState) -> dict[str, Any]:
        try:
            return await node(state)
        except ValueError as exc:
            return {
                "current_agent": name,
                "confidence": 0.0,
                "hitl_pending": HITLRequest(
                    agent=name,
                    reason=f"{type(exc).__name__}: {exc}",
                    payload={"trigger": "error"},
                ),
                "hitl_payload": None,
            }

    return guarded


__all__ = [
    "INITIAL_BACKOFF_S",
    "TRANSIENT_ERRORS",
    "AgentNode",
    "RetryingLLMClient",
    "escalate_permanent_errors",
    "with_retries",
]
