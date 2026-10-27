"""Gate everything under `tests/live/` on `RUN_LIVE_TESTS=1` and an API key."""

from __future__ import annotations

from pathlib import Path

import pytest

from autolab.config import Settings
from autolab.orchestrator.recovery import RetryingLLMClient
from autolab.verification.llm import ClaudeLLMClient, LLMClient

_HERE = Path(__file__).parent


def _skip_reason() -> str | None:
    settings = Settings()
    if not settings.run_live_tests:
        return "live test: set RUN_LIVE_TESTS=1 to run"
    if not settings.anthropic_api_key:
        return "live test: ANTHROPIC_API_KEY is not set"
    return None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    reason = _skip_reason()
    for item in items:
        if _HERE in item.path.parents:
            item.add_marker(pytest.mark.live)
            if reason is not None:
                item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture
def api_key() -> str:
    return Settings().anthropic_api_key


@pytest.fixture
def claude(api_key: str) -> LLMClient:
    return RetryingLLMClient(ClaudeLLMClient(api_key))
