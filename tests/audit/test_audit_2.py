"""AUDIT-2: performance under load. Repros for the findings in `AUDIT-2.md`.

Every `xfail(strict=True)` test asserts the correct behaviour and fails today;
fixing the finding turns it into an XPASS, which fails the suite until the
marker is removed. Heavier measurements live in `scripts/audit_load.py`.
FakeLLMClient only; the retry tests use an in-process HTTP mock, not the network.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Final
from uuid import uuid4

import anthropic
import httpx
import pytest
import z3

from autolab.orchestrator.recovery import RetryingLLMClient
from autolab.state.store import StateStore
from autolab.verification import solver as solver_mod
from autolab.verification.llm import ClaudeLLMClient, FakeLLMClient, LLMPurpose
from autolab.verification.solver import SmtFormula, Z3Solver
from autolab.verification.vericot import Verifier
from scripts.audit_load import (
    NO_CUBES,
    POSITIVE,
    XYZ,
    LatencyLLM,
    chain,
    default_executor_workers,
    pathological,
    rate_limited_transport,
)
from tests.fixtures.golden_traces import build
from tests.integration.orchestrator.test_graph import HYPOTHESIS

_FAST_TIMEOUT_S: Final = 0.5


# --- #1 (high): Z3 checks saturate asyncio's default executor -----------------


@pytest.mark.xfail(strict=True, reason="AUDIT-2 #1")
async def test_slow_checks_do_not_starve_the_default_executor() -> None:
    # 2 x workers + 1 slow checks queue three rounds deep on the shared pool.
    n = 2 * default_executor_workers() + 1
    built = [pathological() for _ in range(n)]
    load = asyncio.gather(
        *(Verifier(f, solver_timeout_seconds=_FAST_TIMEOUT_S).verify(t) for t, f in built)
    )
    # Let every fast premise check (SG7) finish so the slow checks hold the pool.
    await asyncio.sleep(0.2)
    started = time.perf_counter()
    await asyncio.to_thread(lambda: None)  # stands in for getaddrinfo, aiofiles, ...
    waited = time.perf_counter() - started
    await load
    assert waited < 0.25


@pytest.mark.xfail(strict=True, reason="AUDIT-2 #1")
async def test_timeout_bounds_each_check_under_concurrency() -> None:
    n = 2 * default_executor_workers() + 1
    built = [pathological() for _ in range(n)]
    results = await asyncio.gather(
        *(Verifier(f, solver_timeout_seconds=_FAST_TIMEOUT_S).verify(t) for t, f in built)
    )
    assert all(r.step_verdicts[-1].reason == "timeout" for r in results)
    # SG2's guarantee: UNGROUNDED within solver_timeout_s + slack, and solver_ms
    # is the Z3 check's time, not time spent queued for a worker thread.
    worst_ms = max(r.step_verdicts[-1].solver_ms for r in results)
    assert worst_ms < (_FAST_TIMEOUT_S + 0.5) * 1000


# --- #2 (medium): StateStore upserts race --------------------------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-2 #2")
async def test_concurrent_upserts_of_one_entity_are_idempotent(tmp_path: Path) -> None:
    store = await StateStore.create(f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    try:
        run = await store.create_run("q")
        outs = await asyncio.gather(
            *(store.append(run, HYPOTHESIS) for _ in range(20)), return_exceptions=True
        )
        assert [o for o in outs if isinstance(o, BaseException)] == []
        assert await store.load_hypotheses(run) == [HYPOTHESIS]
    finally:
        await store.dispose()


@pytest.mark.xfail(strict=True, reason="AUDIT-2 #2")
async def test_concurrent_ensure_run_is_idempotent(tmp_path: Path) -> None:
    store = await StateStore.create(f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    try:
        run_id = uuid4()
        outs = await asyncio.gather(
            *(store.ensure_run(run_id, "q") for _ in range(10)), return_exceptions=True
        )
        assert [o for o in outs if isinstance(o, BaseException)] == []
        assert [r for r, _ in await store.list_runs()] == [run_id]
    finally:
        await store.dispose()


# --- #3 (medium): entity primary key ignores run_id ---------------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-2 #3")
async def test_same_entity_id_in_two_runs_stays_in_both(tmp_path: Path) -> None:
    store = await StateStore.create(f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    try:
        run_a = await store.create_run("a")
        run_b = await store.create_run("b")
        await store.append(run_a, HYPOTHESIS)
        await store.append(run_b, HYPOTHESIS)
        assert await store.load_hypotheses(run_a) == [HYPOTHESIS]
        assert await store.load_hypotheses(run_b) == [HYPOTHESIS]
    finally:
        await store.dispose()


# --- #4 (medium): retry layers compound, have no jitter, ignore retry-after ---


@pytest.mark.xfail(strict=True, reason="AUDIT-2 #4")
async def test_one_logical_call_makes_at_most_three_http_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits: list[float] = []
    http = httpx.AsyncClient(transport=rate_limited_transport(hits))
    real = anthropic.AsyncAnthropic

    def with_mock_transport(**kwargs: object) -> anthropic.AsyncAnthropic:
        return real(**kwargs, http_client=http)  # type: ignore[arg-type]

    # The production path: ClaudeLLMClient builds its own default AsyncAnthropic.
    monkeypatch.setattr(anthropic, "AsyncAnthropic", with_mock_transport)
    client = RetryingLLMClient(ClaudeLLMClient("sk-audit"), initial_backoff_seconds=0.001)
    try:
        with pytest.raises(anthropic.RateLimitError):
            await client.call("audit", "hi")
    finally:
        await http.aclose()
    assert len(hits) <= 3


class _AlwaysFails(FakeLLMClient):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc
        self.starts: list[float] = []

    async def call(self, purpose: LLMPurpose, prompt: str) -> str:
        self.starts.append(time.perf_counter())
        raise self.exc


@pytest.mark.xfail(strict=True, reason="AUDIT-2 #4")
async def test_concurrent_retries_are_jittered() -> None:
    backoff_s = 0.1
    fakes = [_AlwaysFails(ConnectionError("reset")) for _ in range(20)]
    await asyncio.gather(
        *(RetryingLLMClient(f, initial_backoff_seconds=backoff_s).call("p", "q") for f in fakes),
        return_exceptions=True,
    )
    second = [f.starts[1] for f in fakes]
    assert max(second) - min(second) >= 0.1 * backoff_s


@pytest.mark.xfail(strict=True, reason="AUDIT-2 #4")
async def test_rate_limit_retry_after_is_honoured() -> None:
    retry_after_s = 1.0
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, headers={"retry-after": str(retry_after_s)}, request=request)
    fake = _AlwaysFails(anthropic.RateLimitError("slow down", response=response, body=None))
    client = RetryingLLMClient(fake, max_attempts=2, initial_backoff_seconds=0.05)
    with pytest.raises(anthropic.RateLimitError):
        await client.call("p", "q")
    assert fake.starts[1] - fake.starts[0] >= retry_after_s


# --- #5 (medium): the verifier's LLM calls are strictly sequential -------------


@pytest.mark.xfail(strict=True, reason="AUDIT-2 #5")
async def test_ten_step_trace_overlaps_its_llm_calls() -> None:
    delay_s = 0.15
    trace, fake = build(chain(10, cite=True))
    llm = LatencyLLM(fake, delay_s)
    started = time.perf_counter()
    result = await Verifier(llm, check_faithfulness=True).verify(trace)
    wall = time.perf_counter() - started
    assert result.consistency_score == 1.0
    assert llm.calls == 20  # formalize + faithfulness per step
    # Faithfulness of step N does not gate formalizing step N+1 (the vocabulary
    # is extended before the judge runs), so the judge calls can overlap.
    assert wall < 0.6 * llm.calls * delay_s


# --- #6 (low): cancelling verify() leaves Z3 running ---------------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-2 #6")
async def test_cancelled_verify_releases_its_worker_thread() -> None:
    tasks = []
    for _ in range(default_executor_workers()):
        trace, fake = pathological()
        tasks.append(asyncio.create_task(Verifier(fake, solver_timeout_seconds=3.0).verify(trace)))
    await asyncio.sleep(0.2)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    started = time.perf_counter()
    await asyncio.to_thread(lambda: None)
    assert time.perf_counter() - started < 0.5


# --- #7 (low): uncited premise pool re-scans the whole vocabulary per premise --


@pytest.mark.xfail(strict=True, reason="AUDIT-2 #7")
async def test_solver_document_work_is_linear_in_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    n = 100
    scanned: list[int] = []
    original = solver_mod._build_smt2

    def counting(premises: list[SmtFormula], claim: SmtFormula) -> str:
        scanned.append(sum(len(p.declarations.splitlines()) for p in (*premises, claim)))
        return original(premises, claim)

    monkeypatch.setattr(solver_mod, "_build_smt2", counting)
    trace, fake = build(chain(n, cite=False))
    result = await Verifier(fake).verify(trace)
    assert result.consistency_score == 1.0
    symbols = n // 2 + 1
    # The last check has ~n premises over ~n/2 symbols: its declaration block
    # should be built from the vocabulary once, not once per premise.
    assert scanned[-1] <= 4 * symbols


# --- What held up: SG2's untested shared deadline --------------------------------


async def test_both_checks_share_one_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow consistency check leaves the entailment check only the remaining budget."""
    timeout_s, first_s = 1.0, 0.6
    real_check = z3.Solver.check
    calls: list[float] = []

    def slow_first(self: z3.Solver, *args: object) -> object:
        if not calls:
            time.sleep(first_s)
        started = time.perf_counter()
        out = real_check(self, *args)
        calls.append(time.perf_counter() - started)
        return out

    monkeypatch.setattr(z3.Solver, "check", slow_first)
    started = time.perf_counter()
    out = Z3Solver(timeout_seconds=timeout_s).check_entailment(
        [SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES)
    )
    elapsed = time.perf_counter() - started
    assert (out.verdict, out.reason) == ("UNGROUNDED", "timeout")
    assert len(calls) == 2
    assert calls[1] < timeout_s - first_s + 0.1
    assert elapsed < timeout_s + 0.2
