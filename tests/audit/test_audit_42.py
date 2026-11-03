"""AUDIT-42 repros: performance under load, round 11 (SG36).

Each test asserts the correct behaviour and is a strict xfail until its finding is
fixed. Timing repros are read against the run's own measurements (a check's wall alone,
on the same executor), never absolute seconds. Measurements:
`uv run python -m scripts.audit_load_11`.
"""

from __future__ import annotations

import asyncio
import time
from functools import partial
from typing import Final

import pytest

from autolab.verification import solver as solver_mod
from autolab.verification.solver import SolverExecutor, Z3Solver
from scripts.audit_load_7 import EASY
from scripts.audit_load_9 import UNDECIDABLE
from scripts.audit_load_11 import ShortStream, until, watchdog_threads

_FACTOR: Final = 4.0
"""How many walls alone a check may take beside short callers before it counts as starved."""


# --- #1: a frozen lender stays frozen while short callers keep its lent place busy --------


@pytest.mark.perf
async def test_a_lending_checks_wall_stays_bounded_while_short_callers_stream() -> None:
    # 1 slot, no slow place. A runs-to-timeout check holds the slot and, once stuck, lends
    # its place; two short callers then keep that place busy. SG36 freezes the lender
    # (SIGSTOP) whenever a place is lent and excludes the pause from its budget, so it
    # stays frozen as long as the stream lasts: a 1.0 s check was not done after 10 s
    # (0 of 1; 1.00 s before SG36), `audit_load_11 lender`.
    timeout_s = 0.5
    executor = SolverExecutor(1, 0)
    hard = partial(Z3Solver(timeout_s).check_entailment, *UNDECIDABLE)

    async def timed() -> float:
        started = time.perf_counter()
        await executor.run(hard)
        return time.perf_counter() - started

    task: asyncio.Task[float] | None = None
    try:
        await executor.run(partial(Z3Solver().check_entailment, *EASY))  # a worker is up
        alone = await asyncio.create_task(timed())
        task = asyncio.create_task(timed())
        slots = executor._slots[asyncio.get_running_loop()]
        await until(lambda: slots.stuck == 1)
        async with ShortStream(executor, 2):
            done, _ = await asyncio.wait((task,), timeout=_FACTOR * alone)
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        executor.shutdown()
    assert done, f"a {alone:.2f} s check still running after {_FACTOR * alone:.2f} s"


# --- #2: paused newcomers each hold a worker process and a pool thread --------------------


async def test_a_burst_of_new_runs_holds_worker_processes_bounded_by_the_places() -> None:
    # 2 slots and 2 slow places; 32 new tasks each make one runs-to-timeout check at once.
    # Each newcomer is lent a place, runs `_HARD_SLICES`, and is paused (not stopped) with
    # its worker, so the pool starts a worker per paused newcomer: 64 such tasks peaked at
    # 48 worker processes (1.2 GB PSS) and 51 threads, against 6 (0.2 GB) before SG36.
    executor = SolverExecutor(2, 2)
    bound = solver_mod._IDLE_WORKERS + executor.workers + executor.slow_workers
    hard = partial(Z3Solver(1.0).check_entailment, *UNDECIDABLE)
    pool = solver_mod._workers()
    tasks = [asyncio.create_task(executor.run(hard)) for _ in range(32)]
    peak = 0
    try:
        while not all(t.done() for t in tasks) and peak <= bound:
            peak = max(peak, pool.live())
            await asyncio.sleep(0.01)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        executor.shutdown()
    assert peak <= bound, f"{peak} worker processes at once for {bound} (idle + places)"


# --- #3: a newcomer that gave way gives way again to every short caller --------------------


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-42 #3")
async def test_a_newcomer_that_gave_way_is_served_beside_a_stream_of_short_callers() -> None:
    # 1 slot, no slow place, its check stuck at 5 s; two short callers stream easy checks.
    # A new task's single 150 ms check (not hard) is lent the place and, having given way
    # once, gives way again as soon as any short caller queues, even with no time run:
    # it waited 2.3-7.5 s, giving way 110-400 times (`audit_load_11 giveway`; 4.6-5.2 s
    # before SG36, without giving way). SG36 claims 0.17 s for this newcomer.
    probe_s = 0.15
    executor = SolverExecutor(1, 0)
    probe = partial(Z3Solver(probe_s).check_entailment, *UNDECIDABLE)
    blocker_check = partial(Z3Solver(5.0).check_entailment, *UNDECIDABLE)

    async def timed() -> float:
        started = time.perf_counter()
        await executor.run(probe)
        return time.perf_counter() - started

    blocker: asyncio.Task[object] | None = None
    task: asyncio.Task[float] | None = None
    try:
        await executor.run(partial(Z3Solver().check_entailment, *EASY))
        alone = await asyncio.create_task(timed())
        blocker = asyncio.create_task(executor.run(blocker_check))
        slots = executor._slots[asyncio.get_running_loop()]
        await until(lambda: slots.stuck == 1)
        async with ShortStream(executor, 2):
            await asyncio.sleep(0.1)
            task = asyncio.create_task(timed())
            done, _ = await asyncio.wait((task,), timeout=_FACTOR * alone)
    finally:
        for t in (task, blocker):
            if t is not None:
                t.cancel()
                await asyncio.gather(t, return_exceptions=True)
        executor.shutdown()
    assert done, f"a {alone:.2f} s check still waiting after {_FACTOR * alone:.2f} s"


# --- #4: a watchdog thread per check --------------------------------------------------------


def test_checks_share_one_watchdog_thread() -> None:
    # SG36 starts and joins a `z3-watchdog` thread inside every check. Each start and join
    # is a hand-off between two threads, which waits for a CPU: measured in-process, it
    # adds 0.7-1.1 ms to an easy check idle and 5.6-9.6 ms under 4 CPU burners (median
    # 14-20 against 9-12 ms), `audit_load_11 watchdog`. A worker could keep one armed thread.
    started = watchdog_threads(20)
    assert started <= 1, f"{started} watchdog threads started for 20 checks"
