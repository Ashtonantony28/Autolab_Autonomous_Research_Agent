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

from autolab.verification import solver as solver_mod
from autolab.verification.solver import SmtFormula, SolverExecutor, Z3Solver
from scripts.audit_load_4 import fast_trace_under_slow_runs, lia_chain, timed_check

_OVERRUN_LIMIT: Final = 1.5
"""A check may outlive its timeout by scheduling noise, not by multiples of it."""


# --- #1 (high): the assumption-based entailment check outlives its timeout ---------------


_POOL: Final = 1600
"""1599 linear integer premises (an uncited numeric pool): consistency ~0.45 s, then
the `claim!` assumption check spends ~5 s in a phase Z3 does not interrupt."""

_HEADROOM: Final = 3.0
"""Timeouts are this multiple of the measured consistency check, so the deadline always
falls in the assumption check, however loaded the machine is."""


def _consistency_s(premises: list[SmtFormula], claim: SmtFormula) -> float:
    """Wall s of the check's first (premise consistency) `Solver.check`, worst of two."""
    times = []
    for _ in range(2):
        solver = z3.Solver(ctx=z3.Context())
        solver.from_string(solver_mod._build_smt2(premises, claim))
        started = time.perf_counter()
        assert solver.check() == z3.sat
        times.append(time.perf_counter() - started)
    return max(times)


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-14 #1")
def test_a_check_returns_within_its_timeout() -> None:
    # The pre-SG19 add-based check honours the timeout; checking `claim!` as an assumption
    # runs into a phase Z3 does not interrupt, and returns `timeout` after ~4x the timeout.
    premises, claim = lia_chain(_POOL)
    timeout_s = _HEADROOM * _consistency_s(premises, claim)
    reason, wall_s = timed_check(premises, claim, timeout_s)
    assert reason == "timeout"
    assert wall_s / timeout_s <= _OVERRUN_LIMIT


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-14 #1")
async def test_cancelling_a_check_frees_its_worker_promptly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 30 s check, cancelled once its assumption check is well under way. The re-send
    # thread interrupts it every 5 ms, but the worker stays busy for another few seconds.
    premises, claim = lia_chain(_POOL)
    settle_s = _HEADROOM * _consistency_s(premises, claim)
    assuming = threading.Event()
    real_check = z3.Solver.check

    def check(self: z3.Solver, *assumptions: z3.ExprRef) -> z3.CheckSatResult:
        if assumptions:
            assuming.set()
        return real_check(self, *assumptions)

    monkeypatch.setattr(z3.Solver, "check", check)
    ex = SolverExecutor(1)
    try:
        task = asyncio.create_task(
            ex.run(partial(Z3Solver(30.0).check_entailment, premises, claim))
        )
        assert await asyncio.to_thread(assuming.wait, 60.0)
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
@pytest.mark.xfail(strict=True, reason="AUDIT-14 #2")
async def test_hard_runs_do_not_starve_an_easy_verify() -> None:
    # Two runs whose checks each run to a 0.5 s timeout hold both workers of the default
    # pool. An easy 10-step verify (0.05 s alone) then waits behind a hard check for
    # every one of its checks: ~3 s. On 8 workers it took 0.2 s.
    timeout_s = 0.5
    _, loaded = await fast_trace_under_slow_runs(workers=2, slow_runs=2, timeout_s=timeout_s)
    assert loaded < timeout_s
