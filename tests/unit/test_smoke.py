"""Smoke tests: the empty skeleton imports and the CLI runs."""

from __future__ import annotations

import pytest

import autolab
from autolab import cli
from autolab.config import Settings, get_settings


def test_package_version_is_pinned() -> None:
    assert autolab.__version__ == "0.0.0"


def test_cli_main_prints_banner(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main([])
    captured = capsys.readouterr()
    assert "AutoLab v0.0.0" in captured.out
    assert "skeleton OK" in captured.out


def test_cli_run_without_api_key_exits_with_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as exc:
            cli.main(["run", "Why?"])
    finally:
        get_settings.cache_clear()
    assert exc.value.code == 2
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err


def test_settings_load_from_env(settings: Settings) -> None:
    assert settings.anthropic_api_key == "test-key"
    assert settings.database_url.startswith("sqlite+aiosqlite:")
    assert settings.run_live_tests is False
