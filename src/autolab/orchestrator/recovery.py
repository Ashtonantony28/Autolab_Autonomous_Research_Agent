"""Retry-with-backoff for transient agent failures."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    initial_backoff_seconds: float = 0.5,
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


__all__ = ["with_retries"]
