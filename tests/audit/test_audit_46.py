"""AUDIT-46 repros: performance under load, round 12 (SG37).

Each test asserts the correct behaviour and is a strict xfail until its finding is
fixed. Timing repros are read against the run's own measurements (a check's wall alone,
on the same executor), never absolute seconds.
"""

from __future__ import annotations

import asyncio
import time
from functools import partial
from typing import Final

import pytest

from autolab.verification.solver import SolverExecutor, Z3Solver
from scripts.audit_load_7 import EASY
from scripts.audit_load_9 import UNDECIDABLE

_EASY_SHARE: Final = 0.25
"""The largest share of one hard check's wall alone that an easy check may wait."""


# --- #1: a check on a lent place is frozen for its lender's whole run-on -----------------


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-46 #1")
async def test_an_easy_check_beside_a_due_lender_is_not_frozen_for_its_run_on() -> None:
    # A 1-CPU host's defaults: 1 slot, no slow place. One run makes back-to-back
    # runs-to-timeout checks; one short caller streams easy checks on the lent place.
    # SG37: once the lender has been frozen 1.5 timeouts it runs on and the easy check on
    # the lent place is frozen (SIGSTOP) for the lender's whole remaining budget, almost
    # one timeout. Observed (`AUDIT-46.md` #1): max easy wall 0.89-0.96 s beside 1.0 s
    # checks, 2.51 s beside 3.0 s ones; easy alone 2-3 ms. SG36: 0.03 s.
    timeout_s = 0.5
    executor = SolverExecutor(1, 0)
    hard = partial(Z3Solver(timeout_s).check_entailment, *UNDECIDABLE)
    easy = partial(Z3Solver().check_entailment, *EASY)
    stop = False
    walls: list[float] = []

    async def hog() -> None:
        while not stop:
            await executor.run(hard)

    async def short() -> None:
        while not stop:
            started = time.perf_counter()
            await executor.run(easy)
            walls.append(time.perf_counter() - started)

    tasks: list[asyncio.Task[None]] = []
    try:
        await executor.run(easy)  # a worker is up
        started = time.perf_counter()
        await asyncio.create_task(executor.run(hard))
        alone = time.perf_counter() - started
        tasks = [asyncio.create_task(hog())]
        await asyncio.sleep(0.2 * alone)  # the hog's check is stuck
        tasks.append(asyncio.create_task(short()))
        await asyncio.sleep(6 * alone)  # more than two lender cycles of 2.5 timeouts
    finally:
        stop = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        executor.shutdown()
    assert walls, "no easy check finished"
    worst = max(walls)
    assert worst <= _EASY_SHARE * alone, (
        f"an easy check waited {worst:.2f} s = {worst / alone:.2f} hard checks "
        f"({len(walls)} easy checks; hard alone {alone:.2f} s)"
    )
