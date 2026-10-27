"""AUDIT-11 repros: API/SDK consistency, round 3 (see AUDIT-11.md).

Every test asserts the *correct* behaviour and is a strict xfail, so it
starts to XPASS, and fails the suite, once its finding is fixed. No network:
every LLM is a seeded `FakeLLMClient`.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.types import Command

from autolab import cli
from autolab.agents.analysis import build_analysis_trace_prompt
from autolab.agents.common import design_facts, measurement_facts
from autolab.config import Settings, get_settings
from autolab.monitoring.tracer import RunTracer
from autolab.orchestrator import checkpoints
from autolab.orchestrator import graph as graph_module
from autolab.orchestrator.graph import build_graph
from autolab.state.models import ExperimentDesign, ExperimentParameter, HITLResolution, Hypothesis
from autolab.state.schema import empty_state
from autolab.state.store import StateStore
from autolab.verification.llm import FakeLLMClient
from tests.fixtures.agent_helpers import register_failing_trace
from tests.integration.orchestrator.test_graph import (
    EXPENSIVE,
    HYPOTHESIS,
    QUESTION,
    REPORT,
    RESULT,
    FixedResultSimulator,
    _seed_contradiction_check,
    _seed_full_run,
)

ROOT = Path(__file__).resolve().parents[2]
THREAD = UUID("00000000-0000-4000-8000-000000000111")


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[pytest.MonkeyPatch]:
    """API key, a `tmp_path` store, a fixed thread id and a fixed simulator."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'store.db'}")
    monkeypatch.setattr(cli, "uuid4", lambda: THREAD)
    monkeypatch.setattr(graph_module, "LabSimulator", lambda: FixedResultSimulator(RESULT))
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


async def autolab(argv: list[str]) -> None:
    """`cli.main` on a worker thread, so its `asyncio.run` never meets the test's loop."""
    await asyncio.to_thread(cli.main, argv)


def use_llm(env: pytest.MonkeyPatch, design: ExperimentDesign | None = None) -> FakeLLMClient:
    llm = FakeLLMClient()
    if design is None:
        _seed_full_run(llm)
    else:
        _seed_full_run(llm, design)
    env.setattr(cli, "_make_llm", lambda _key: llm)
    return llm


# --- #1 (high): an approved Analysis escalation never reaches the store -------


async def test_an_approved_analysis_report_is_persisted(tmp_path: Path) -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake)
    register_failing_trace(  # replaces the passing analysis trace
        fake,
        agent="analysis",
        trace_purpose="analysis.trace",
        trace_prompt=build_analysis_trace_prompt(RESULT, REPORT),
        conclusion=REPORT.interpretations[0].statement,
        facts=measurement_facts(RESULT),
    )
    store = await StateStore.create(f"sqlite+aiosqlite:///{tmp_path / 'store.db'}")
    try:
        graph = build_graph(
            llm=fake,
            simulator=FixedResultSimulator(RESULT),
            settings=Settings(max_retries=0),
            retry_backoff_seconds=0,
            tracer=RunTracer(store),
        )
        run = uuid4()
        config: dict[str, Any] = {"configurable": {"thread_id": str(run)}}
        paused = await graph.ainvoke(empty_state(QUESTION), config=config)
        assert paused["__interrupt__"][0].value["agent"] == "analysis"

        final = await graph.ainvoke(Command(resume=HITLResolution(action="approve")), config=config)

        assert len(final["analysis_reports"]) == 1
        # Today: [] -- the dashboard shows a finished run with no analysis.
        assert await store.load_reports(run) == final["analysis_reports"]
    finally:
        await store.dispose()


# --- #2 (medium): pytest-timeout cannot end a hung CLI test -------------------

_HUNG_CLI_TEST = """
import asyncio

from autolab import cli
from tests.unit.test_cli import autolab


def test_hung_cli(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    async def hang(*_args, **_kwargs):
        await asyncio.Event().wait()  # a run that never finishes

    monkeypatch.setattr(cli, "_make_llm", lambda _key: object())
    monkeypatch.setattr(cli, "_drive", hang)
    autolab(["run", "q"])
"""


@pytest.mark.xfail(strict=True, reason="AUDIT-11 #2")
def test_a_hung_cli_test_fails_and_is_named(tmp_path: Path) -> None:
    test_file = tmp_path / "test_hung_cli.py"
    test_file.write_text(textwrap.dedent(_HUNG_CLI_TEST))
    argv = [sys.executable, "-m", "pytest", "-c", str(ROOT / "pyproject.toml")]
    argv += ["--rootdir", str(ROOT), "-p", "no:cacheprovider", "-q", "--timeout=1"]
    try:
        proc = subprocess.run(
            [*argv, str(test_file)], cwd=ROOT, capture_output=True, text=True, timeout=12
        )
    except subprocess.TimeoutExpired:
        # Today: the timeout fires, then `with ThreadPoolExecutor` waits for the
        # hung worker forever; the session never reports the test.
        pytest.fail("pytest did not exit within 12 s of a 1 s per-test timeout")
    assert proc.returncode == 1
    assert "FAILED" in proc.stdout
    assert "test_hung_cli" in proc.stdout


# --- #3 (medium): an aborted run is reported as complete ----------------------


async def test_resume_abort_does_not_report_a_completed_run(
    cli_env: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ckpt = str(tmp_path / "ckpt.db")
    use_llm(cli_env, EXPENSIVE)
    await autolab(["run", QUESTION, "--checkpoints", ckpt])
    assert "Paused for human review (design)" in capsys.readouterr().out

    await autolab(["resume", str(THREAD), "abort", "--checkpoints", ckpt])

    out = capsys.readouterr().out
    # Today: "Run ... complete. confidence=1.00, hypotheses=1", exit status 0.
    assert "complete" not in out
    assert "abort" in out.lower()


# --- #4 (medium): the contradiction trigger is unreachable from `autolab run` -


@pytest.mark.xfail(strict=True, reason="AUDIT-11 #4")
async def test_a_hypothesis_contradicting_a_stored_one_pauses(
    cli_env: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prior = Hypothesis(question=QUESTION, statement="Y plays no role in X", rationale="r")
    store = await StateStore.create(f"sqlite+aiosqlite:///{tmp_path / 'store.db'}")
    try:
        earlier = uuid4()
        await store.ensure_run(earlier, QUESTION)
        await store.append(earlier, prior)
    finally:
        await store.dispose()
    llm = use_llm(cli_env)
    _seed_contradiction_check(llm, prior, "(not hyp_Y)")  # prior and HYPOTHESIS are unsat

    await autolab(["run", QUESTION, "--checkpoints", str(tmp_path / "ckpt.db")])

    # Today: research starts from `empty_state`, so `prior` is always [] and the run completes.
    out = capsys.readouterr().out
    assert "Paused for human review (research): hypothesis contradicts" in out


# --- #5 (low): the printed thread id of a run that failed to start ------------


@pytest.mark.xfail(strict=True, reason="AUDIT-11 #5")
async def test_a_printed_thread_id_is_resumable(
    cli_env: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ckpt = str(tmp_path / "ckpt.db")
    use_llm(cli_env)
    cli_env.setenv("DATABASE_URL", "postgresql+psycopg://autolab:autolab@localhost:5432/autolab")
    get_settings.cache_clear()
    with pytest.raises(ModuleNotFoundError):
        await autolab(["run", QUESTION, "--checkpoints", ckpt])
    announced = f"Run {THREAD} started." in capsys.readouterr().out

    cli_env.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'store.db'}")
    get_settings.cache_clear()
    try:
        await autolab(["resume", str(THREAD), "--checkpoints", ckpt])
        resumed = f"Run {THREAD} complete." in capsys.readouterr().out
    except SystemExit:
        resumed = False  # Today: "no paused run ... (it is finished or unknown)"

    assert resumed or not announced


# --- #6 (low): distinct design parameters can share one fact symbol ----------


def test_distinct_parameters_get_distinct_fact_symbols() -> None:
    design = ExperimentDesign(
        hypothesis_id=HYPOTHESIS.id,
        protocol="incubate",
        estimated_cost_usd=1.0,
        estimated_duration_minutes=5.0,
        parameters=(
            ExperimentParameter(name="temp-C", value=4.0),
            ExperimentParameter(name="temp C", value=37.0),
        ),
    )
    facts = design_facts(design)

    # Before SG17: both became `param_temp_C`, so the facts asserted 4.0 and 37.0 for one
    # symbol and every step of an honest trace is `premises_inconsistent`.
    assert len({f.declarations for f in facts}) == len(facts)


# --- #7 (low): `hitl_node` is a dead, policy-free export ----------------------


def test_no_policy_free_hitl_node_is_exported() -> None:
    # Today: `checkpoints.hitl_node` (no policies) is exported but unused by
    # `build_graph`; wiring it silently drops the AUDIT-5 #3 policy re-check.
    assert "hitl_node" not in checkpoints.__all__
    assert not hasattr(checkpoints, "hitl_node")
