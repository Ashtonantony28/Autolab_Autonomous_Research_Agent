"""AUDIT-10: performance under load, round 3. Repros for the findings in `AUDIT-10.md`.

Every `xfail(strict=True)` test asserts the correct behaviour and fails today;
fixing the finding turns it into an XPASS, which fails the suite until the
marker is removed. Wall-clock repros carry `perf`. Heavier measurements live in
`scripts/audit_load_3.py`. FakeLLMClient and stubs only; nothing touches the network.
"""

from __future__ import annotations

from typing import Final

import pytest

from scripts.audit_load_3 import (
    context_overhead_ratio,
    held_contexts_report,
    pool_inflation,
    printer_peak_concurrency,
    s08_suite_probe,
)

_HELD_THREADS: Final = 16
_MB_PER_IDLE_THREAD: Final = 2.0
"""Generous allowance for what one idle thread may keep after formalizing (a context is ~16 MB)."""


# --- #1 (high): the 8-thread solver pool convoys on the GIL -----------------------------


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-10 #1")
async def test_the_default_solver_pool_does_not_inflate_solver_ms_under_concurrency() -> None:
    # 12 concurrent 30-step verifies, once on a 1-worker pool and once on the default
    # pool (8 workers on 4 CPUs). Every z3py call drops and re-takes the GIL, so the
    # workers convoy on it: no extra throughput, and each check's solver_ms grows 10x
    # (loaded machine) to 100x (quiet machine).
    pooled_p95, serial_p95 = await pool_inflation(verifies=12, steps=30)
    assert pooled_p95 <= 2 * serial_p95


# --- #2 (medium): pytest-timeout cannot stop a Z3 call on the main thread ------------


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-10 #2")
def test_a_hung_main_thread_solver_check_fails_within_the_per_test_limit() -> None:
    # AUDIT-8 H1's mutant S08 (no solver time limit) on a sync test whose Z3 check runs on
    # the main thread, with --timeout=1. The signal-based timeout cannot interrupt the C
    # call, so the child pytest never finishes; it is killed after 8 s with no test named.
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


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-10 #4")
def test_a_trivial_check_is_not_dominated_by_creating_a_context() -> None:
    # The same parse and two checks on one reused context take about a third of the time
    # `Z3Solver.check_entailment` takes (ratio ~3.0, measured interleaved on one thread).
    assert context_overhead_ratio(rounds=150) < 1.5


# --- #5 (low): counter-models go through z3's shared printer on every worker ----------


@pytest.mark.xfail(strict=True, reason="AUDIT-10 #5")
async def test_counter_models_are_never_printed_on_two_threads_at_once() -> None:
    # z3's pretty-printer keeps its state in module globals. 16 INVALID checks on 8
    # workers put 8 threads inside it at once; the strings are discarded by the verifier.
    assert await printer_peak_concurrency(checks=16, workers=8) <= 1
