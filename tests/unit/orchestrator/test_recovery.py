"""with_retries semantics."""

from __future__ import annotations

import pytest

from autolab.orchestrator.recovery import with_retries


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
