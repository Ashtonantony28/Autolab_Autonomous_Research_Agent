"""Smoke tests: the empty skeleton imports and the CLI runs."""

from __future__ import annotations

import pytest

import autolab
from autolab import cli
from autolab.config import Settings


def test_package_version_is_pinned() -> None:
    assert autolab.__version__ == "0.0.0"


def test_cli_main_prints_banner(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main([])
    captured = capsys.readouterr()
    assert "AutoLab v0.0.0" in captured.out
    assert "skeleton OK" in captured.out


def test_settings_load_from_env(settings: Settings) -> None:
    assert settings.anthropic_api_key == "test-key"
    assert settings.database_url.startswith("sqlite+aiosqlite:")
    assert settings.run_live_tests is False
