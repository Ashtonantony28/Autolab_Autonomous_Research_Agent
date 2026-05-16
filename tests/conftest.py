"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from autolab.config import Settings, get_settings


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Yield a fresh Settings with a dummy API key, isolated from real env."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    get_settings.cache_clear()
    try:
        yield get_settings()
    finally:
        get_settings.cache_clear()
