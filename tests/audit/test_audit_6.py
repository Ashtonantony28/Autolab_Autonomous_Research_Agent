"""AUDIT-6: performance under load, round 2. Repros for the findings in `AUDIT-6.md`.

Every `xfail(strict=True)` test asserts the correct behaviour and fails today;
fixing the finding turns it into an XPASS, which fails the suite until the
marker is removed. Heavier measurements live in `scripts/audit_load_2.py`.
FakeLLMClient and stubs only; nothing touches the network.
"""

from __future__ import annotations

import asyncio
import gc
import sqlite3
import threading
import time
import tracemalloc
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Final, ParamSpec, TypeVar
from uuid import uuid4

import pytest

from autolab.orchestrator import recovery
from autolab.orchestrator.graph import build_graph
from autolab.orchestrator.recovery import RetryingLLMClient
from autolab.state.schema import empty_state
from autolab.state.store import StateStore
from autolab.verification import solver as solver_mod
from autolab.verification.llm import FakeLLMClient
from autolab.verification.solver import SolverExecutor, _Interrupt
from autolab.verification.vericot import Verifier
from scripts import bench_verifier
from scripts.audit_load import chain
from scripts.audit_load_2 import (
    CountingExecutor,
    JudgeLLM,
    RateLimited,
    slow_check,
    stress_returncode,
)
from tests.fixtures.golden_traces import build
from tests.integration.orchestrator.test_graph import (
    HYPOTHESIS,
    QUESTION,
    RESULT,
    FixedResultSimulator,
    _seed_full_run,
)

_WAIT_S: Final = 60.0
"""Upper bound on waiting for a repro's precondition; reached only if the setup hangs."""

_LOCK_HOLD_S: Final = 5.5
"""Longer than the 5 s SQLite busy timeout the store's driver used by default."""

P = ParamSpec("P")
T = TypeVar("T")

P = ParamSpec("P")
T = TypeVar("T")


# --- #1 (critical): verifies on two threads crash the interpreter -------------


def test_verifies_on_several_threads_do_not_crash_the_interpreter() -> None:
    # 4 threads, each with its own event loop, verify 5 traces of 20 steps.
    # Today the formalizer's parse check shares Z3's global context across
    # threads and the child interpreter dies with SIGSEGV (-11).
    codes = [stress_returncode(4, 5, timeout_s=20.0) for _ in range(2)]
    assert codes == [0, 0]


# --- #2 (medium): SQLite store fails a run instead of waiting for a writer ----


async def test_store_append_waits_out_a_concurrent_writer(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    store = await StateStore.create(f"sqlite+aiosqlite:///{path}")
    try:
        run = await store.create_run("q")
        locked = threading.Event()

        def other_writer() -> None:
            # Another process's write transaction, held a little past the busy timeout.
            conn = sqlite3.connect(path, isolation_level=None)
            conn.execute("BEGIN IMMEDIATE")
            locked.set()
            time.sleep(_LOCK_HOLD_S)
            conn.execute("COMMIT")
            conn.close()

        holder = threading.Thread(target=other_writer)
        holder.start()
        await asyncio.to_thread(locked.wait)
        try:
            await store.append(run, HYPOTHESIS)
        finally:
            await asyncio.to_thread(holder.join)
        assert len(await store.load_hypotheses(run)) == 1
    finally:
        await store.dispose()


# --- #3 (medium): retry-after waits are not jittered ---------------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-6 #3")
async def test_retry_after_waits_are_jittered(monkeypatch: pytest.MonkeyPatch) -> None:
    retry_after_s = 0.2
    # Each client's waits are recorded instead of slept, so the spread of attempt 2's
    # start is that of the first waits, whatever the event loop's scheduling delay.
    waits: dict[asyncio.Task[object] | None, list[float]] = defaultdict(list)

    async def record(delay: float) -> None:
        waits[asyncio.current_task()].append(delay)

    monkeypatch.setattr(recovery, "asyncio", SimpleNamespace(sleep=record))
    await asyncio.gather(
        *(
            asyncio.ensure_future(
                RetryingLLMClient(
                    RateLimited([], str(retry_after_s)), initial_backoff_seconds=0.05
                ).call("p", "q")
            )
            for _ in range(20)
        ),
        return_exceptions=True,
    )
    firsts = [w[0] for w in waits.values()]
    assert len(firsts) == 20
    spread_ms = (max(firsts) - min(firsts)) * 1000
    # 20 clients rate-limited together should not all retry in the same instant.
    assert spread_ms >= 0.1 * retry_after_s * 1000


# --- #4 (medium): faithfulness judges fan out without a bound -------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-6 #4")
async def test_judges_in_flight_per_verify_are_bounded() -> None:
    trace, fake = build(chain(100, cite=True))
    llm = JudgeLLM(fake, formalize_s=0.002, judge_s=0.5)
    result = await Verifier(llm, check_faithfulness=True).verify(trace)
    assert result.consistency_score == 1.0
    assert llm.peak <= 10


# --- #5 (low): a failed judge is noticed only after every step is formalized ---


@pytest.mark.xfail(strict=True, reason="AUDIT-6 #5")
async def test_failed_judge_stops_formalization() -> None:
    trace, fake = build(chain(30, cite=True))
    llm = JudgeLLM(fake, formalize_s=0.03, judge_s=0.03, fail_judge_on="a0 holds")
    with pytest.raises(ConnectionError):
        await Verifier(llm, check_faithfulness=True).verify(trace)
    # The judge of step 0 fails during step 1's formalization; at most one more starts.
    assert llm.formalize_after_failure <= 2


# --- #6 (low): the bench leaves out SG7's premise checks ------------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-6 #6")
async def test_bench_times_every_solver_check(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = CountingExecutor()
    monkeypatch.setattr(solver_mod, "_default_executor", executor)
    try:
        report = await bench_verifier.run(1)
    finally:
        executor.shutdown()
    # Premise steps run a consistency check (SG7) but are reported as "assumed".
    assert len(report.step_ms) == executor.checks


# --- #7 (low): executor shutdown cancels callers on other event loops ---------


class _SubmitCountingPool(ThreadPoolExecutor):
    """A pool that signals each submission, so a test can wait until both callers are in."""

    def __init__(self, workers: int) -> None:
        super().__init__(max_workers=workers, thread_name_prefix="z3")
        self.submitted = threading.Semaphore(0)

    def submit(self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        future = super().submit(fn, *args, **kwargs)
        self.submitted.release()
        return future


class _ObservedExecutor(SolverExecutor):
    """A `SolverExecutor` that signals once a check is running on its worker."""

    def __init__(self) -> None:
        super().__init__(1)
        self._pool.shutdown()
        self._pool = self.pool = _SubmitCountingPool(1)
        self.running = threading.Event()

    def _call(self, fn: Callable[[], T], interrupt: _Interrupt) -> tuple[T, float]:
        def observed() -> T:
            self.running.set()
            return fn()

        return super()._call(observed, interrupt)


@pytest.mark.xfail(strict=True, reason="AUDIT-6 #7")
async def test_shutdown_does_not_cancel_callers_on_other_loops() -> None:
    executor = _ObservedExecutor()
    outcomes: list[str] = []

    def caller() -> None:
        async def main() -> None:
            try:
                outcomes.append(await slow_check(executor, 3.0))
            except BaseException as exc:  # recorded: the assertion is about its type
                outcomes.append(type(exc).__name__)

        asyncio.run(main())

    # Each loop's semaphore admits one check: one runs, the other waits in the pool.
    threads = [threading.Thread(target=caller) for _ in range(2)]
    for t in threads:
        t.start()
    for _ in threads:
        assert await asyncio.to_thread(executor.pool.submitted.acquire, timeout=_WAIT_S)
    assert await asyncio.to_thread(executor.running.wait, _WAIT_S)
    await asyncio.to_thread(executor.shutdown)
    for t in threads:
        await asyncio.to_thread(t.join)
    assert len(outcomes) == 2
    # Neither task was cancelled, so neither should see CancelledError.
    assert "CancelledError" not in outcomes


# --- #8 (low): a reused graph grows with every completed run -------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-6 #8")
async def test_reused_graph_memory_does_not_grow_per_run() -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake)
    graph = build_graph(llm=fake, simulator=FixedResultSimulator(RESULT), retry_backoff_seconds=0)

    async def one_run() -> None:
        config = {"configurable": {"thread_id": str(uuid4())}}
        await graph.ainvoke(empty_state(QUESTION), config=config)

    for _ in range(3):
        await one_run()
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        for _ in range(15):
            await one_run()
        gc.collect()
        grown = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    # Measured: about 66 KB per completed run, all held by the in-process saver.
    assert grown < 256 * 1024
