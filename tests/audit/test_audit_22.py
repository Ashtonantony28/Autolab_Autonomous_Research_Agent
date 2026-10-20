"""AUDIT-22: performance under load, round 6. Repros for the findings in `AUDIT-22.md`.

Every `xfail(strict=True)` test asserts the correct behaviour and fails today;
fixing the finding turns it into an XPASS, which fails the suite until the
marker is removed. Wall-clock repros carry `perf` and are calibrated against the
machine's own measurements (ratios, shares of an uninterrupted run, the
interpreter's GIL switch interval, a fake LLM's latency), not absolute seconds.
Ordering waits on conditions; cancels land at a share of the check's own run
time. Heavier measurements live in `scripts/audit_load_6.py`.
FakeLLMClient and stubs only; nothing touches the network.
"""

from __future__ import annotations

import sys
from typing import Final

import pytest

from scripts.audit_load_4 import timed_check
from scripts.audit_load_5 import int_function
from scripts.audit_load_6 import (
    chars_parsed_to_merge,
    conjunction_pool,
    design_attempt_s,
    easy_check_after_cancelling_slow_ones,
    entailment_beside_python,
    freed_after_cancel_s,
    gil_overhead_s,
    newcomer_wait_s,
    sizes_under_quota,
    untimed_s,
)

# --- #1 (critical): the bounded probe still parses quadratically many characters ----------

_WIDE: Final = 1024
_PARSE_SHARE: Final = 64
"""A probe linear in the declaration parses a few dozen times its text; today ~17 000x."""


@pytest.mark.xfail(strict=True, reason="AUDIT-22 #1")
def test_probing_a_wide_declaration_parses_text_linear_in_it() -> None:
    # One 4 KB reply line declares a 1024-argument Int function. SG24 made the number of
    # probe parses linear (2k + 2), but each probe declares and applies all k arguments,
    # so the probe hands Z3 72 MB and blocks the event loop for 11-13 s (52 s at 2048
    # arguments, 8 KB). Stops counting once the budget is spent.
    decl = int_function(_WIDE)
    budget = _PARSE_SHARE * len(decl)
    parsed = chars_parsed_to_merge(decl, budget)
    assert parsed <= budget, f"parsed more than {budget} chars for a {len(decl)}-char line"


# --- #2 (high): long pools pay one GIL hand-off per premise -------------------------------

_GIL_POOL: Final = 200
_GIL_WAITS: Final = 40
"""A check that makes a fixed number of C calls waits for the GIL a few dozen times at most."""


@pytest.mark.perf
def test_a_long_premise_check_waits_for_the_gil_a_bounded_number_of_times() -> None:
    # A Python thread runs beside the check, as the event loop does while it formalizes
    # other runs. Each `Z3_solver_check` and `Z3_solver_assert` of SG27's loading drops
    # and retakes the GIL; with another thread running Python, each retake waits up to
    # the switch interval (5 ms). 200 premises: ~2.5 s instead of ~30 ms; at 100 premises
    # (one call) the overhead is ~40 ms. Bounded in switch intervals, so CPU speed cancels.
    quiet, busy = gil_overhead_s(_GIL_POOL)
    limit = _GIL_WAITS * sys.getswitchinterval()
    assert busy - quiet <= limit, f"quiet {quiet * 1000:.0f} ms, beside Python {busy:.2f} s"


@pytest.mark.perf
def test_an_entailment_beside_python_work_reaches_its_quiet_verdict() -> None:
    # 400 Boolean premises entail the claim in ~40 ms alone. With a timeout of 20x that,
    # the same check beside a Python thread returns `timeout` (UNGROUNDED): the step's
    # verdict depends on what else the process is doing.
    quiet, beside, alone_s, timeout_s = entailment_beside_python(400, 20.0)
    assert quiet == "entailed"
    assert beside == "entailed", f"{beside} at {timeout_s:.2f} s (alone {alone_s * 1000:.0f} ms)"


# --- #3 (high): pools of few, long premises still run one unpolled simplex -----------------

_BOUNDS: Final = 1000
_CONJUNCTIONS: Final = 50
_SHARE: Final = 5
_OVERRUN_LIMIT: Final = 1.5


@pytest.mark.perf
def test_a_pool_of_long_real_premises_returns_within_its_timeout() -> None:
    # The 999 Real bounds of AUDIT-18 #3, stated by 50 steps of 20 bounds each: 50
    # premises, so SG27 loads them in one call and Z3's simplex runs without polling.
    # A timeout at a fifth of the check's own run time is overrun ~5x (20x at 1600 bounds).
    premises, claim = conjunction_pool(_BOUNDS, _CONJUNCTIONS)
    alone_s = untimed_s(premises, claim)
    timeout_s = alone_s / _SHARE
    reason, wall_s = timed_check(premises, claim, timeout_s)
    assert reason == "timeout"
    assert wall_s / timeout_s <= _OVERRUN_LIMIT, f"{wall_s:.2f} s of {timeout_s:.2f} s"


@pytest.mark.perf
async def test_cancelling_a_check_of_long_real_premises_frees_its_worker_promptly() -> None:
    # Same pool, cancelled a tenth of the way in: the worker stays busy until the
    # simplex ends by itself.
    premises, claim = conjunction_pool(_BOUNDS, _CONJUNCTIONS)
    alone_s = untimed_s(premises, claim)
    freed_s = await freed_after_cancel_s(premises, claim, alone_s / (2 * _SHARE))
    assert freed_s < alone_s / _SHARE, f"freed after {freed_s:.2f} s (alone {alone_s:.2f} s)"


# --- #4 (high): a new run's first check waits until no short caller is left ----------------

_BUSY_RUNS: Final = 3
_CHECK_S: Final = 0.02
_DURATION_S: Final = 2.0


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-22 #4")
async def test_a_new_runs_first_check_is_not_starved_by_short_callers() -> None:
    # 2 slots, 3 runs making back-to-back 20 ms checks for 2 s. A new run's first check
    # is not "short" (its task has no last check), and `_Slots._wake` serves every short
    # waiter first, so it waits until the busy runs stop: ~1.9 s instead of ~0.05 s
    # (one check per run ahead of it). The checks sleep, so CPU speed does not matter.
    took, left, _ = await newcomer_wait_s(_BUSY_RUNS, _CHECK_S, _DURATION_S)
    assert took < left / 4, f"waited {took:.2f} s; the busy runs had {left:.2f} s left"


# --- #5 (medium): a cancelled slow check still holds its thread ---------------------------


@pytest.mark.perf
async def test_cancelling_a_slow_check_does_not_hold_up_the_next_one() -> None:
    # 1 worker, 1 slow place. A check Z3 cannot interrupt (#3's pool) goes slow, so the
    # pool has a thread for the next caller; then its caller is cancelled. `run` gives
    # back the slow place and the thread budget at once, the pool retires the spare
    # thread, and the next easy check queues behind the uninterruptible one.
    premises, claim = conjunction_pool(_BOUNDS, _CONJUNCTIONS)
    alone_s = untimed_s(premises, claim)
    took = await easy_check_after_cancelling_slow_ones(
        premises, claim, after_s=alone_s / (2 * _SHARE), workers=1
    )
    assert took < alone_s / _SHARE, f"easy check {took:.2f} s (hard check alone {alone_s:.2f} s)"


# --- #6 (medium): the solver is sized from the affinity, not the CPU quota -----------------


def test_the_solver_is_sized_to_a_one_cpu_quota() -> None:
    # A child whose cgroup files (v2 `cpu.max`, v1 `cpu.cfs_quota_us`) allow one CPU, on
    # this 4-CPU host: `docker --cpus=1` or a Kubernetes CPU limit. The affinity still
    # lists every host CPU, so the pool takes 2 slots and 2 slow places: AUDIT-18 #5's
    # oversubscription, which turns decidable checks into timeouts.
    sizes = sizes_under_quota(1)
    assert sizes["workers"] + sizes["slow"] <= 1, sizes


# --- #7 (medium): the independent estimate runs after the whole verify -------------------

_STEPS: Final = 10
_CALL_S: Final = 0.1


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-22 #7")
async def test_a_design_the_estimate_rejects_is_rejected_in_a_few_calls_time() -> None:
    # Every call takes 100 ms. The design's estimate is 100x the independent one, so the
    # attempt is withheld for a retry. It is found out only after design.experiment,
    # design.trace and 10 formalize calls: ~14.5 calls' time, where 3 calls decide it.
    took, log, trigger = await design_attempt_s(_STEPS, _CALL_S, estimate_cost_usd=500.0)
    assert trigger == "verification"
    assert "design.estimate" in log
    assert took <= 6 * _CALL_S, f"{took:.2f} s ({len(log)} calls: {log})"
