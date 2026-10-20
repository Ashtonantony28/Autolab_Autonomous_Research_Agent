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


_TIMEOUT_SHARE: Final = 3.0
"""The timeout of `_timeout_case`, in multiples of the time until its entailment check
starts (~0.5 s here). That check runs ~11 s; on the assumption path its unpolled phase
alone takes ~10x the consistency check, so the bounds scale with the machine."""

_HOLES: Final = 9


def _pigeonhole(holes: int) -> tuple[str, str]:
    """Declarations and formula of `holes + 1` pigeons in `holes` holes: unsat, slow to refute."""
    pigeons = range(holes + 1)
    decls = "\n".join(f"(declare-const q{p}_{h} Bool)" for p in pigeons for h in range(holes))
    each = [f"(or {' '.join(f'q{p}_{h}' for h in range(holes))})" for p in pigeons]
    apart = [
        f"(not (and q{p}_{h} q{r}_{h}))"
        for h in range(holes)
        for p in pigeons
        for r in range(p + 1, holes + 1)
    ]
    return decls, f"(and {' '.join(each + apart)})"


def _timeout_case() -> tuple[list[SmtFormula], SmtFormula, float]:
    """`lia_chain(_POOL)` with a claim whose entailment check runs past its timeout.

    The claim `b0 > 0 or not PHP` is entailed, and Z3 must refute the pigeonhole
    formula to see it. Its consistency check is the chain's, measured here, so the
    `_SETUP_SHARE` guard lets the entailment check start (AUDIT-20 #3); that check polls the limit
    once set up, so the deadline lands inside it. On the assumption path the check
    first spends ~5 s in a phase Z3 does not interrupt.
    """
    premises, claim = lia_chain(_POOL)
    decls, php = _pigeonhole(_HOLES)
    hard = SmtFormula(f"{claim.declarations}\n{decls}", f"(or {claim.assertion} (not {php}))")
    until_s, _ = _phases_s(premises, claim)
    return premises, hard, _TIMEOUT_SHARE * until_s


@pytest.mark.perf
def test_a_check_returns_within_its_timeout() -> None:
    # The pre-SG19 add-based check honours the timeout; checking `claim!` as an assumption
    # runs into a phase Z3 does not interrupt, and returns `timeout` after ~3x the timeout.
    premises, claim, timeout_s = _timeout_case()
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
