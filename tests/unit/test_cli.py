"""`autolab run` and `autolab resume` around a crash, a completed run and a pause."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from autolab import cli
from autolab.config import get_settings
from autolab.orchestrator import graph as graph_module
from autolab.state.models import ExperimentDesign
from tests.integration.orchestrator.test_graph import (
    DESIGN,
    EXPENSIVE,
    QUESTION,
    RESULT,
    FixedResultSimulator,
    FlakyLLMClient,
    _seed_full_run,
)

THREAD = UUID("00000000-0000-4000-8000-000000000013")


def autolab(argv: list[str]) -> None:
    """`cli.main` on a worker thread, so its `asyncio.run` never meets pytest's loop."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(cli.main, argv).result()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[pytest.MonkeyPatch]:
    """API key, a `tmp_path` store, a fixed thread id and a fixed simulator."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'store.db'}")
    monkeypatch.setattr(cli, "uuid4", lambda: THREAD)
    monkeypatch.setattr(graph_module, "LabSimulator", lambda: FixedResultSimulator(RESULT))
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def use_llm(
    env: pytest.MonkeyPatch, design: ExperimentDesign = DESIGN, failures: int = 0
) -> FlakyLLMClient:
    """A seeded fake behind `autolab` whose first `failures` design calls fail."""
    llm = FlakyLLMClient("design.experiment", failures=failures)
    _seed_full_run(llm, design)
    env.setattr(cli, "_make_llm", lambda _key: llm)
    return llm


def test_run_prints_the_thread_id_before_a_crash(
    env: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def crash(*_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError("outage")

    use_llm(env)
    env.setattr(cli, "_drive", crash)

    with pytest.raises(ConnectionError):
        autolab(["run", QUESTION, "--checkpoints", str(tmp_path / "ckpt.db")])

    assert capsys.readouterr().out == f"Run {THREAD} started.\n"


def test_resume_without_action_continues_a_crashed_run(
    env: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ckpt = str(tmp_path / "ckpt.db")
    use_llm(env, failures=3)
    with pytest.raises(ConnectionError):
        autolab(["run", QUESTION, "--checkpoints", ckpt])
    assert f"Run {THREAD} started." in capsys.readouterr().out

    autolab(["resume", str(THREAD), "--checkpoints", ckpt])

    assert f"Run {THREAD} complete. confidence=1.00, hypotheses=1" in capsys.readouterr().out


def test_resume_of_a_completed_run_exits_2(
    env: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ckpt = str(tmp_path / "ckpt.db")
    use_llm(env)
    autolab(["run", QUESTION, "--checkpoints", ckpt])
    assert f"Run {THREAD} complete." in capsys.readouterr().out

    for argv in (["resume", str(THREAD)], ["resume", str(THREAD), "approve"]):
        with pytest.raises(SystemExit) as exc:
            autolab([*argv, "--checkpoints", ckpt])
        assert exc.value.code == 2
        assert f"no paused run {THREAD}" in capsys.readouterr().err


def test_resume_of_an_unknown_run_exits_2(
    env: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    use_llm(env)
    with pytest.raises(SystemExit) as exc:
        autolab(["resume", str(uuid4()), "--checkpoints", str(tmp_path / "ckpt.db")])

    assert exc.value.code == 2
    assert "(it is finished or unknown)" in capsys.readouterr().err


def test_resume_of_a_paused_run_requires_an_action(
    env: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ckpt = str(tmp_path / "ckpt.db")
    use_llm(env, EXPENSIVE)
    autolab(["run", QUESTION, "--checkpoints", ckpt])
    assert "Paused for human review (design)" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:
        autolab(["resume", str(THREAD), "--checkpoints", ckpt])

    assert exc.value.code == 2
    assert "is paused for human review; give approve|retry|abort" in capsys.readouterr().err
    autolab(["resume", str(THREAD), "approve", "--checkpoints", ckpt])
    assert f"Run {THREAD} complete." in capsys.readouterr().out
