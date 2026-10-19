"""Error handling: retry-with-backoff for transient failures, HITL for permanent ones."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any, Final, Protocol, TypeVar

import anthropic

from autolab.state.models import HITLRequest
from autolab.state.schema import AutoLabState
from autolab.verification.llm import LLMClient, LLMPurpose, SchemaT

T = TypeVar("T")

TRANSIENT_ERRORS: Final[tuple[type[BaseException], ...]] = (
    anthropic.APIConnectionError,  # includes APITimeoutError
    ConnectionError,
    TimeoutError,
)
"""Non-HTTP errors worth retrying. `APIStatusError`s are classified by status (`is_transient`)."""

TRANSIENT_STATUS_CODES: Final[frozenset[int]] = frozenset({408, 409, 429})
"""Retryable 4xx statuses (as in the SDK's `_should_retry`); every 5xx, including 529, is too."""

INITIAL_BACKOFF_S: Final[float] = 0.5
MAX_ATTEMPTS: Final[int] = 3
MAX_RETRY_AFTER_S: Final[float] = 60.0
"""Cap on a server-requested `retry-after` wait (the SDK's own cap)."""


def is_transient(exc: BaseException) -> bool:
    """True if retrying `exc` may succeed: a network error, 408/409/429 or any 5xx.

    An explicit `x-should-retry` response header overrides the status code.
    """
    if isinstance(exc, anthropic.APIStatusError):
        header = exc.response.headers.get("x-should-retry")
        if header in ("true", "false"):
            return bool(header == "true")
        return exc.status_code in TRANSIENT_STATUS_CODES or exc.status_code >= 500
    return isinstance(exc, TRANSIENT_ERRORS)


def retry_after_seconds(exc: BaseException) -> float | None:
    """The server's `retry-after-ms` / `retry-after` wait in seconds, capped at 60 s."""
    if not isinstance(exc, anthropic.APIStatusError):
        return None
    headers = exc.response.headers
    for name, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        try:
            seconds = float(headers[name]) / scale
        except (KeyError, ValueError):
            continue  # absent, or an HTTP date: our own backoff applies
        return min(max(seconds, 0.0), MAX_RETRY_AFTER_S)
    return None


async def with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = MAX_ATTEMPTS,
    initial_backoff_seconds: float = INITIAL_BACKOFF_S,
    backoff_multiplier: float = 2.0,
    retry_on: type[BaseException] | tuple[type[BaseException], ...] = Exception,
    retry_if: Callable[[BaseException], bool] = lambda _: True,
) -> T:
    """Invoke `operation` up to `max_attempts` times with full-jitter exponential backoff.

    An exception is retried if it is a `retry_on` instance and `retry_if(exc)`
    holds. Each wait is uniform in `[0, backoff]` but never shorter than the
    server's `retry-after` (`retry_after_seconds`). Re-raises the last
    exception if every attempt fails. Use only for transient errors.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    delay = initial_backoff_seconds
    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except retry_on as exc:
            if attempt == max_attempts or not retry_if(exc):
                raise
            await asyncio.sleep(max(random.uniform(0, delay), retry_after_seconds(exc) or 0.0))
            delay *= backoff_multiplier
    raise AssertionError("unreachable: the loop returns or re-raises")  # pragma: no cover


class RetryingLLMClient:
    """`LLMClient` decorator that retries transient errors (`is_transient`) with backoff.

    It is the only retry layer: `ClaudeLLMClient` builds its SDK client with
    `max_retries=0`. `is_live` sees through it via `_inner`.
    """

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
            retry_if=is_transient,
        )


class AgentNode(Protocol):
    """An async LangGraph node returning a partial `AutoLabState` update."""

    async def __call__(self, state: AutoLabState) -> dict[str, Any]: ...


def escalate_permanent_errors(name: str, node: AgentNode) -> AgentNode:
    """Wrap an agent node so a permanent error pauses at HITL instead of crashing the graph.

    Permanent errors are those a retry would repeat: `ValueError` (Pydantic's
    `ValidationError`, `FormalizationError`, the agents' input checks),
    `RuntimeError` (a refusal or empty structured output, see
    `StructuredOutputError`) and any `anthropic.APIError` that `is_transient`
    rejects (400, 401, 403, 404, 413, 422...). The request's payload holds
    `trigger="error"`, the error type and, for HTTP errors, the status code.
    Transient errors are retried inside the LLM client; once exhausted they
    propagate, and the checkpoint allows a resume.
    """

    async def guarded(state: AutoLabState) -> dict[str, Any]:
        try:
            return await node(state)
        except (ValueError, RuntimeError, anthropic.APIError) as exc:
            if isinstance(exc, anthropic.APIError) and is_transient(exc):
                raise
            payload: dict[str, Any] = {"trigger": "error", "error": type(exc).__name__}
            if isinstance(exc, anthropic.APIStatusError):
                payload["status_code"] = exc.status_code
            return {
                "current_agent": name,
                "confidence": 0.0,
                "hitl_pending": HITLRequest(
                    agent=name, reason=f"{type(exc).__name__}: {exc}", payload=payload
                ),
                "hitl_payload": None,
            }

    return guarded


__all__ = [
    "INITIAL_BACKOFF_S",
    "MAX_RETRY_AFTER_S",
    "TRANSIENT_ERRORS",
    "TRANSIENT_STATUS_CODES",
    "AgentNode",
    "RetryingLLMClient",
    "escalate_permanent_errors",
    "is_transient",
    "retry_after_seconds",
    "with_retries",
]
