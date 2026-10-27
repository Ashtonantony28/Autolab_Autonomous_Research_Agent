"""`autolab run` pauses to disk and `autolab resume` finishes it in a later invocation."""

from __future__ import annotations

import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from autolab import cli
from autolab.config import get_settings
from autolab.orchestrator import graph as graph_module
from autolab.verification.llm import FakeLLMClient
from tests.integration.orchestrator.test_graph import (
    DESIGN,
    EXPENSIVE,
    QUESTION,
    RESULT,
    FixedResultSimulator,
    _seed_design,
    _seed_full_run,
)


def autolab(argv: list[str]) -> None:
    """`cli.main` on a worker thread, as a fresh process would: its `asyncio.run`
    then never meets the loop pytest-asyncio left on the main thread."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(cli.main, argv).result()


RESUME_HINT = re.compile(r"autolab resume (\S+) approve\|retry\|abort")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[FakeLLMClient]:
    """A seeded fake behind `autolab`, with the store in `tmp_path`."""
    llm = FakeLLMClient()
    _seed_full_run(llm, EXPENSIVE)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'store.db'}")
    monkeypatch.setattr(cli, "_make_llm", lambda _key: llm)
    monkeypatch.setattr(graph_module, "LabSimulator", lambda: FixedResultSimulator(RESULT))
    get_settings.cache_clear()
    yield llm
    get_settings.cache_clear()


def run_until_paused(ckpt: Path, capsys: pytest.CaptureFixture[str]) -> str:
    autolab(["run", QUESTION, "--checkpoints", str(ckpt)])
    out = capsys.readouterr().out
    assert "Paused for human review (design): estimated cost $50.00 > $10.00" in out
    match = RESUME_HINT.search(out)
    assert match is not None
    return match.group(1)


def test_run_then_resume_approve_completes(
    fake: FakeLLMClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ckpt = tmp_path / "ckpt.db"
    thread_id = run_until_paused(ckpt, capsys)

    autolab(["resume", thread_id, "approve", "--checkpoints", str(ckpt)])

    out = capsys.readouterr().out
    assert f"Run {thread_id} complete. confidence=1.00, hypotheses=1" in out


def test_resume_retry_passes_the_note_as_feedback(
    fake: FakeLLMClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ckpt = tmp_path / "ckpt.db"
    thread_id = run_until_paused(ckpt, capsys)
    note = "find a protocol under $10"
    _seed_design(fake, DESIGN, feedback=note)
    _seed_full_run(fake)  # downstream prompts for the cheaper design

    autolab(["resume", thread_id, "retry", "--note", note, "--checkpoints", str(ckpt)])

    assert f"Run {thread_id} complete." in capsys.readouterr().out


def test_resume_abort_ends_and_a_second_resume_is_rejected(
    fake: FakeLLMClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ckpt = tmp_path / "ckpt.db"
    thread_id = run_until_paused(ckpt, capsys)
    autolab(["resume", thread_id, "abort", "--checkpoints", str(ckpt)])
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        autolab(["resume", thread_id, "approve", "--checkpoints", str(ckpt)])

    assert exc.value.code == 2
    assert f"no paused run {thread_id}" in capsys.readouterr().err


def test_resume_unknown_thread_exits_2(
    fake: FakeLLMClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        autolab(["resume", "nope", "approve", "--checkpoints", str(tmp_path / "ckpt.db")])

    assert exc.value.code == 2
    assert "no paused run nope" in capsys.readouterr().err


def test_resume_rejects_unknown_action(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        autolab(["resume", "t", "skip"])

    assert exc.value.code == 2
    assert "invalid choice: 'skip'" in capsys.readouterr().err


def test_resume_without_api_key_exits_with_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as exc:
            autolab(["resume", "t", "approve"])
    finally:
        get_settings.cache_clear()

    assert exc.value.code == 2
    assert "autolab resume: ANTHROPIC_API_KEY is not set" in capsys.readouterr().err
