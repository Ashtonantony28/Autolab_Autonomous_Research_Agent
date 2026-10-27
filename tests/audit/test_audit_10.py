"""AUDIT-10: performance under load, round 3. Repros for the findings in `AUDIT-10.md`.

Every `xfail(strict=True)` test asserts the correct behaviour and fails today;
fixing the finding turns it into an XPASS, which fails the suite until the
marker is removed. Wall-clock repros carry `perf`. Heavier measurements live in
`scripts/audit_load_3.py`. FakeLLMClient and stubs only; nothing touches the network.
"""

from __future__ import annotations

from typing import Final

import pytest

from autolab.config import SOLVER_TIMEOUT_S
from autolab.verification import solver as solver_mod
from autolab.verification.solver import SolverExecutor, Z3Solver
from scripts.audit_load_2 import P95, pct
from scripts.audit_load_3 import (
    CHAIN_CLAIM,
    CHAIN_PREMISES,
    concurrent_solver_ms,
    contexts_created,
    held_contexts_report,
    printer_peak_concurrency,
    s08_suite_probe,
)

_POOL_ROUNDS: Final = 3
_HELD_THREADS: Final = 16
_MB_PER_IDLE_THREAD: Final = 2.0
"""Generous allowance for what one idle thread may keep after formalizing (a context is ~16 MB)."""


# --- #1 (high): the 8-thread solver pool convoys on the GIL -----------------------------


@pytest.mark.perf
async def test_the_default_solver_pool_does_not_inflate_solver_ms_under_concurrency() -> None:
    # 12 concurrent 30-step verifies, once on a 1-worker pool and once on the default
    # pool (8 workers on 4 CPUs). Every z3py call drops and re-takes the GIL, so the
    # workers convoy on it: no extra throughput, and each check's solver_ms grows 10x
    # (loaded machine) to 100x (quiet machine).
    # Serial and pooled alternate within a round, so both see the same background load.
    # The best of up to `_POOL_ROUNDS` rounds counts.
    ratios: list[float] = []
    while len(ratios) < _POOL_ROUNDS and (not ratios or min(ratios) > 2):
        serial = SolverExecutor(1)
        try:
            pair = (serial, solver_mod.default_executor())
            order = pair if len(ratios) % 2 == 0 else pair[::-1]
            p95 = {ex: pct((await concurrent_solver_ms(ex, 12, 30))[0], P95) for ex in order}
        finally:
            serial.shutdown()
        ratios.append(p95[pair[1]] / p95[serial])
    assert min(ratios) <= 2


# --- #2 (medium): pytest-timeout cannot stop a Z3 call on the main thread ------------


@pytest.mark.perf
def test_a_hung_main_thread_solver_check_fails_within_the_per_test_limit() -> None:
    # AUDIT-8 H1's mutant S08 (no solver time limit) on a sync test whose Z3 check runs on
    # the main thread, with --timeout=1. The signal-based timeout cannot interrupt the C
    # call, so the child pytest never finishes; it is killed after 8 s with no test named.
    # Since SG29 the check runs in a worker process and the main thread waits on a pipe,
    # which the signal interrupts.
    wall, killed, out = s08_suite_probe(limit_s=1.0, kill_after_s=8.0)
    assert not killed
    assert "test_hard_premise_consistency_check_times_out" in out
    assert wall < 6.0


# --- #3 (medium): SG11 keeps a ~16 MB z3.Context alive per thread ---------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-10 #3")
def test_threads_that_have_formalized_do_not_each_keep_a_z3_context() -> None:
    # In a child interpreter: 16 idle threads grow RSS by ~0.3 MB; 16 threads that each
    # formalized one step and then went idle grow it by ~265 MB, one context per thread.
    control, formalized = held_contexts_report(_HELD_THREADS)
    assert formalized - control < _HELD_THREADS * _MB_PER_IDLE_THREAD


# --- #4 (low): every check builds and frees a fresh z3.Context -------------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-10 #4")
def test_a_trivial_check_is_not_dominated_by_creating_a_context() -> None:
    # Every check builds and frees its own context; reusing one makes the same parse and
    # two checks about twice as fast. Counted, not timed: since SG29 a ratio of times
    # mixes in worker timing and passes on fast runners (AUDIT-28 #8).
    solver = Z3Solver(SOLVER_TIMEOUT_S)
    premises = list(CHAIN_PREMISES)
    assert (
        contexts_created(lambda: [solver._check_here(premises, CHAIN_CLAIM) for _ in range(3)]) <= 1
    )


# --- #5 (low): counter-models go through z3's shared printer on every worker ----------


async def test_counter_models_are_never_printed_on_two_threads_at_once() -> None:
    # z3's pretty-printer keeps its state in module globals. 16 INVALID checks on 8
    # workers put 8 threads inside it at once; the strings are discarded by the verifier.
    assert await printer_peak_concurrency(checks=16, workers=8) <= 1
