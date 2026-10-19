"""AUDIT-7 repros: API/SDK consistency, round 2 (see AUDIT-7.md).

Every test asserts the *correct* behaviour and is a strict xfail, so it
starts to XPASS, and fails the suite, once its finding is fixed. No network:
the SDK test drives a real `AsyncAnthropic` through an in-process transport.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import anthropic
import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine

from autolab import cli
from autolab.agents.common import REPORT_REPLY
from autolab.config import get_settings
from autolab.orchestrator import graph as graph_module
from autolab.state.models import ExperimentDesign, ExperimentParameter, FormalizedStep, Hypothesis
from autolab.state.store import StateStore
from autolab.tools.lab_hardware import LabSimulator
from autolab.verification.llm import (
    ClaudeLLMClient,
    FakeLLMClient,
    LLMPurpose,
    StructuredOutputError,
)
from autolab.verification.solver import SolverExecutor
from tests.integration.orchestrator.test_graph import (
    QUESTION,
    RESULT,
    FixedResultSimulator,
    _seed_full_run,
)

# --- #1 (high): a run killed by an exhausted transient error cannot resume ----


class _Outage(FakeLLMClient):
    """A seeded fake that fails every call with a connection error while `down`."""

    down = True

    async def call(self, purpose: LLMPurpose, prompt: str) -> str:
        if self.down:
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.test"))
        return await super().call(purpose, prompt)


def _autolab(argv: list[str]) -> None:
    """`cli.main` on a worker thread, as `tests/integration/test_cli_resume.py` does."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(cli.main, argv).result()


@pytest.mark.xfail(strict=True, reason="AUDIT-7 #1")
def test_run_killed_by_a_transient_outage_can_be_resumed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    llm = _Outage()
    _seed_full_run(llm)
    thread_id = uuid4()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'store.db'}")
    monkeypatch.setattr(cli, "_make_llm", lambda _key: llm)
    monkeypatch.setattr(cli, "uuid4", lambda: thread_id)
    monkeypatch.setattr(graph_module, "LabSimulator", lambda: FixedResultSimulator(RESULT))
    get_settings.cache_clear()
    ckpt = str(tmp_path / "ckpt.db")
    try:
        # Three connection errors in a row exhaust `RetryingLLMClient`; the run dies.
        with pytest.raises(anthropic.APIConnectionError):
            _autolab(["run", QUESTION, "--checkpoints", ckpt])
        # `recovery.escalate_permanent_errors`: "the checkpoint allows a resume".
        llm.down = False
        try:
            _autolab(["resume", str(thread_id), "retry", "--checkpoints", ckpt])
        except SystemExit as exc:
            pytest.fail(f"autolab resume exited {exc.code}: {capsys.readouterr().err}")
    finally:
        get_settings.cache_clear()


# --- #2 (medium): an SG6-era database rejects every write ---------------------

_SG6_SCHEMA = """
CREATE TABLE runs (id VARCHAR(36) PRIMARY KEY, research_question VARCHAR, created_at DATETIME);
CREATE TABLE hypotheses (
    id VARCHAR(36) PRIMARY KEY,
    run_id VARCHAR(36) REFERENCES runs(id),
    created_at DATETIME,
    payload JSON
);
"""


@pytest.mark.xfail(strict=True, reason="AUDIT-7 #2")
async def test_store_on_an_sg6_database_still_persists_entities(tmp_path: Path) -> None:
    # `autolab.db` written before SG9: the entity key is `id` alone.
    path = tmp_path / "autolab.db"
    with sqlite3.connect(path) as con:
        con.executescript(_SG6_SCHEMA)
    store = await StateStore.create(f"sqlite+aiosqlite:///{path}")
    try:
        run = uuid4()
        await store.ensure_run(run, "q")
        await store.append(run, Hypothesis(question="q", statement="s", rationale="r"))
        assert [h.statement for h in await store.load_hypotheses(run)] == ["s"]
    finally:
        await store.dispose()


# --- #3 (medium): the documented Postgres URL has no driver -------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-7 #3")
async def test_documented_postgres_url_has_an_installed_driver() -> None:
    # `.env.example` and `store.py` name `postgresql+psycopg` as the production URL.
    engine = create_async_engine("postgresql+psycopg://autolab:autolab@localhost:5432/autolab")
    await engine.dispose()


# --- #4 (medium): max_tokens / refusal replies are not StructuredOutputError ---


def _message(text: str, stop_reason: str) -> dict[str, object]:
    return {
        "id": "msg_audit7",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


@pytest.mark.xfail(strict=True, reason="AUDIT-7 #4")
@pytest.mark.parametrize(
    ("text", "stop_reason"),
    [
        ('{"declarations": "(declare-const p Bool)", "asser', "max_tokens"),
        ("I can't help with that.", "refusal"),
    ],
    ids=["max_tokens", "refusal"],
)
async def test_unusable_reply_raises_structured_output_error_naming_stop_reason(
    text: str, stop_reason: str
) -> None:
    body = json.dumps(_message(text, stop_reason)).encode()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sdk = anthropic.AsyncAnthropic(api_key="sk-audit", max_retries=0, http_client=http)
    client = ClaudeLLMClient("sk-audit", client=sdk)
    try:
        with pytest.raises(StructuredOutputError, match=stop_reason):
            await client.call_structured("formalize", "p", FormalizedStep)
    finally:
        await http.aclose()


# --- #5 (low): content_schema drops the domain model's validators -------------


@pytest.mark.xfail(strict=True, reason="AUDIT-7 #5")
@pytest.mark.parametrize(
    "interpretations",
    [[], [{"statement": "a", "rank": 1, "likelihood": 0.5}]],
    ids=["empty", "ranks-not-dense"],
)
def test_report_reply_enforces_the_report_validators(interpretations: list[object]) -> None:
    # `AnalysisReport` rejects both; the schema sent to the model accepts both.
    with pytest.raises(ValidationError):
        REPORT_REPLY.model_validate({"interpretations": interpretations, "summary": "s"})


# --- #6 (low): SolverExecutor shutdown cancels a caller that was not cancelled -


@pytest.mark.xfail(strict=True, reason="AUDIT-7 #6")
def test_shutdown_does_not_turn_a_queued_check_into_a_cancellation() -> None:
    executor = SolverExecutor(1)
    running = threading.Event()
    outcomes: dict[str, object] = {}

    def slow() -> str:
        running.set()
        time.sleep(0.3)
        return "slow"

    def caller(name: str, fn: object) -> None:
        async def go() -> object:
            return await executor.run(fn)  # type: ignore[arg-type]

        try:
            outcomes[name] = asyncio.run(go())
        except BaseException as exc:  # recorded for the assertion below
            outcomes[name] = exc

    a = threading.Thread(target=caller, args=("a", slow))
    a.start()
    assert running.wait(2.0)
    # A second event loop has its own semaphore, so its check is queued in the pool.
    b = threading.Thread(target=caller, args=("b", lambda: "quick"))
    b.start()
    deadline = time.monotonic() + 2.0
    while executor._pool._work_queue.qsize() < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert executor._pool._work_queue.qsize() >= 1
    executor.shutdown()
    a.join()
    b.join()
    assert not isinstance(outcomes["b"], asyncio.CancelledError)


# --- #7 (low): the simulator averages boolean parameters as numbers -----------


@pytest.mark.xfail(strict=True, reason="AUDIT-7 #7")
def test_simulator_ignores_boolean_parameters_in_the_baseline() -> None:
    design = ExperimentDesign(
        hypothesis_id=uuid4(),
        protocol="plate read",
        parameters=(
            ExperimentParameter(name="temperature", value=10.0),
            ExperimentParameter(name="stirred", value=True),
        ),
        estimated_cost_usd=1.0,
        estimated_duration_minutes=1.0,
    )
    result = LabSimulator(seed=0).run(design)
    assert result.measurements[0].value == 10.0
