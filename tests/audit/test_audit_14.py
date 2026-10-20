"""AUDIT-14: performance under load, round 4. Repros for the findings in `AUDIT-14.md`.

Every `xfail(strict=True)` test asserts the correct behaviour and fails today;
fixing the finding turns it into an XPASS, which fails the suite until the
marker is removed. Wall-clock repros carry `perf`. Heavier measurements live in
`scripts/audit_load_4.py`. FakeLLMClient and stubs only; nothing touches the network.
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import suppress
from functools import partial
from typing import Final

import pytest
import z3

from autolab.verification.solver import SmtFormula, SolverExecutor, Z3Solver
from scripts.audit_load_4 import fast_trace_under_slow_runs, lia_chain, timed_check

_OVERRUN_LIMIT: Final = 1.5
"""A check may outlive its timeout by scheduling noise, not by multiples of it."""


# --- #1 (high): the assumption-based entailment check outlives its timeout ---------------


_POOL: Final = 1600
"""1599 linear integer premises (an uncited numeric pool): consistency ~0.45 s, then
the `claim!` assumption check spent ~5 s in a phase Z3 does not interrupt."""

_INTO_ENTAILMENT: Final = 0.25
"""Deadlines and cancels land this fraction of the way into the measured entailment check."""


def _phases_s(premises: list[SmtFormula], claim: SmtFormula) -> tuple[float, float]:
    """(s until the entailment check starts, s it runs) in `check_entailment`, best of two."""
    real_check = z3.Solver.check
    until, entail = [], []
    stamps: list[float] = []

    def check(self: z3.Solver, *assumptions: z3.ExprRef) -> z3.CheckSatResult:
        stamps.append(time.perf_counter())
        try:
            return real_check(self, *assumptions)
        finally:
            stamps.append(time.perf_counter())

    z3.Solver.check = check
    try:
        for _ in range(2):
            stamps.clear()
            started = time.perf_counter()
            Z3Solver(30.0).check_entailment(premises, claim)
            assert len(stamps) == 4  # premise consistency first, then the entailment check
            until.append(stamps[2] - started)
            entail.append(stamps[3] - stamps[2])
    finally:
        z3.Solver.check = real_check
    return min(until), min(entail)


@pytest.mark.perf
def test_a_check_returns_within_its_timeout() -> None:
    # The pre-SG19 add-based check honours the timeout; checking `claim!` as an assumption
    # runs into a phase Z3 does not interrupt, and returns `timeout` after ~4x the timeout.
    # The deadline falls inside the entailment check, measured on this machine.
    premises, claim = lia_chain(_POOL)
    until_s, entail_s = _phases_s(premises, claim)
    timeout_s = until_s + _INTO_ENTAILMENT * entail_s
    reason, wall_s = timed_check(premises, claim, timeout_s)
    assert reason == "timeout"
    assert wall_s / timeout_s <= _OVERRUN_LIMIT


@pytest.mark.perf
async def test_cancelling_a_check_frees_its_worker_promptly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 30 s check, cancelled once its entailment check is under way. On the assumption
    # path the re-send thread interrupted it every 5 ms, but the worker stayed busy for
    # another few seconds.
    premises, claim = lia_chain(_POOL)
    settle_s = _INTO_ENTAILMENT * _phases_s(premises, claim)[1]
    entailing = threading.Event()
    real_check = z3.Solver.check
    checks = [0]

    def check(self: z3.Solver, *assumptions: z3.ExprRef) -> z3.CheckSatResult:
        checks[0] += 1
        if checks[0] == 2:  # premise consistency first, then the entailment check
            entailing.set()
        return real_check(self, *assumptions)

    monkeypatch.setattr(z3.Solver, "check", check)
    ex = SolverExecutor(1)
    try:
        task = asyncio.create_task(
            ex.run(partial(Z3Solver(30.0).check_entailment, premises, claim))
        )
        assert await asyncio.to_thread(entailing.wait, 60.0)
        await asyncio.sleep(settle_s)
        started = time.perf_counter()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await ex.run(lambda: None)
        freed_s = time.perf_counter() - started
    finally:
        ex.shutdown()
    assert freed_s < 0.5


# --- #2 (high): two hard runs starve every other run of the 2-worker pool ----------------


@pytest.mark.perf
async def test_hard_runs_do_not_starve_an_easy_verify() -> None:
    # Two runs whose checks each run to a 0.5 s timeout hold both workers of the default
    # pool. An easy 10-step verify (0.05 s alone) then waits behind a hard check for
    # every one of its checks: ~3 s. On 8 workers it took 0.2 s.
    timeout_s = 0.5
    _, loaded = await fast_trace_under_slow_runs(workers=2, slow_runs=2, timeout_s=timeout_s)
    assert loaded < timeout_s
