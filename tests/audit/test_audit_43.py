"""AUDIT-43 repros: API/SDK consistency, round 11 (see AUDIT-43.md).

Every xfail asserts the *correct* behaviour and is strict, so it starts to
XPASS, and fails the suite, once its finding is fixed. No network and no LLM.
Waits are bounded by polls and joins, never by wall-clock assertions.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Final

import pytest

from autolab.verification import solver as solver_mod
from autolab.verification import solver_workers
from autolab.verification.solver import SolverExecutor, Z3Solver
from scripts.audit_load_9 import UNDECIDABLE

_ROOT: Final = Path(__file__).resolve().parents[2]
_JOIN_S: Final = 30.0
"""Bounds a wait; a passing or failing run never waits this long."""
_POLL_S: Final = 0.01
_LONG_S: Final = 30.0
"""A check timeout no test reaches: every check here is cancelled."""
_NEWCOMERS: Final = 3

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="SIGSTOP")


async def _until(condition: Callable[[], bool]) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _JOIN_S
    while not condition():
        assert loop.time() < deadline, "condition never held"
        await asyncio.sleep(_POLL_S)


# --- #1 (medium): SG36 is "done" with its own verification failing --------------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-43 #1")
def test_a_sub_goal_is_done_only_once_its_verification_is_green() -> None:
    # PLAN.md Rules: "Stop and report if a task fails its verification 3 times". SG36's
    # Verify list has "`make test` green 5 times under 4 CPU burners"; its Result says that
    # failed "in 1-2 of 5 runs, three rounds in a row" and was not repeated, yet the row is
    # "done (PR pending)" and the Log says "SG36 done".
    plan = (_ROOT / "PLAN.md").read_text()
    row = next(ln for ln in plan.splitlines() if ln.startswith("| SG36 |"))
    status = row.rstrip(" |").rsplit("|", 1)[1].strip()
    result = next(ln for ln in plan.splitlines() if ln.startswith("Result: done (ADR 0009, SG36"))
    unmet = re.search(r"failed in \S+ of 5 runs, three rounds in a row", result)
    assert unmet is not None  # the Result records the failing Verify item
    # Today: 'done (PR pending); the 5 loaded runs are not yet green (see Result)'.
    assert not status.startswith("done"), status


# --- #2 (medium): every frozen newcomer keeps a worker process and a pool thread ------------


class _Snapshot:
    """What an executor holds once `_NEWCOMERS` newcomers wait, frozen or stopped, for a slot."""

    def __init__(self, executor: SolverExecutor) -> None:
        running = list(executor._running)
        workers = [i._target for i in running]
        self.in_flight = len(running)
        self.frozen = sum(i.frozen() for i in running)
        self.threads = executor._pool._max_workers
        stopped = sum(
            isinstance(w, solver_workers.Worker) and w._paused_at is not None for w in workers
        )
        self.z3_running = self.in_flight - stopped
        """Calls in flight whose worker process is not stopped (SIGSTOP)."""


async def _frozen_newcomers(executor: SolverExecutor) -> _Snapshot:
    """A hard check stuck in the pool's one slot, then `_NEWCOMERS` hard newcomers: each is
    lent the place, shows it is hard, gives way and waits for a slot."""
    check = partial(Z3Solver(_LONG_S).check_entailment, *UNDECIDABLE)
    tasks = [asyncio.create_task(executor.run(check))]
    slots = executor._slot()
    try:
        await _until(lambda: slots.stuck == 1)
        tasks += [asyncio.create_task(executor.run(check)) for _ in range(_NEWCOMERS)]
        await _until(lambda: sum(not w.new for w in slots._waiters) == _NEWCOMERS)
        return _Snapshot(executor)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@linux_only
async def test_worker_processes_in_use_stay_within_the_pools_places() -> None:
    # Before SG36 a newcomer that gave way was killed, so at most 2 x workers + slow_workers
    # checks held a worker (what `_IDLE_WORKERS` is sized for). SG36 kept each one frozen
    # with its worker process (about 61 MB RSS here) and a pool thread: in flight 4,
    # threads 4 (1 slot, 0 slow, 3 newcomers). Since SG37 at most `workers` wait frozen;
    # the others are stopped, as before SG36.
    executor = SolverExecutor(1, 0)
    try:
        got = await _frozen_newcomers(executor)
    finally:
        executor.shutdown()
    assert (got.frozen, got.z3_running) == (executor.workers, 1)  # the CPU bound itself holds
    bound = 2 * executor.workers + executor.slow_workers
    assert got.in_flight <= bound and got.threads <= bound, (got.in_flight, got.threads)


# --- #3 (low): a lender that ends with its place still lent frees its slot ------------------


class _Target:
    def interrupt(self) -> None:
        pass

    def pause(self) -> None:
        pass

    def resume(self) -> None:
        pass


async def test_checks_never_outnumber_the_slots_once_a_lender_ends() -> None:
    # `_Slots`: "While a place is lent, one lending slot's check is frozen, so ... Z3 checks
    # never outnumber slots and slow places". A lender that ends (here: cancelled) while
    # its place is still lent gives back its slot at once; the next caller takes it and
    # runs beside the lent check, with no lender left to freeze.
    slots = solver_mod._Slots(1)
    lender = solver_mod._Interrupt()
    assert lender.attach(_Target())
    assert await slots.acquire(short=False) is False
    assert slots.stick(False, lender)
    assert await slots.acquire(short=True) is True  # e.g. a short caller's hard check
    assert lender.frozen()
    slots.unstick(lender)  # `run`'s finally for the cancelled lender ...
    slots.release(False)  # ... gives back its slot
    later = asyncio.ensure_future(slots.acquire(short=False))
    await asyncio.sleep(0)
    running = slots.held + slots.lent - sum(i.frozen() for i in slots._lenders)
    later.cancel()
    await asyncio.gather(later, return_exceptions=True)
    # Today: 2, a new slot check beside the lent one on a 1-slot pool.
    assert running <= slots.workers, (slots.held, slots.lent, running)


# --- #4 (low): without SIGSTOP a newcomer that gives way keeps running ----------------------


@linux_only
async def test_without_sigstop_the_pre_sg36_bound_still_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR 0009 SG36: "elsewhere nothing is paused and the bound is not held". Simulated by
    # `_PAUSABLE = False`. Before SG36 a newcomer that gave way was killed, so at most
    # 2 x workers + slow_workers Z3 checks ran. Now it is frozen in name only and keeps
    # running while the next newcomer is lent its place. Observed: 4 running on 1 slot.
    # Since SG37 none is frozen there: each is stopped and made again, as before SG36.
    monkeypatch.setattr(solver_workers, "_PAUSABLE", False)
    executor = SolverExecutor(1, 0)
    try:
        got = await _frozen_newcomers(executor)
    finally:
        executor.shutdown()
    assert got.frozen == 0  # stopped, not frozen in name only
    assert got.z3_running <= 2 * executor.workers + executor.slow_workers, got.z3_running
