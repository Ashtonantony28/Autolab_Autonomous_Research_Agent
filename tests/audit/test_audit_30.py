"""AUDIT-30 repros: performance under load, round 8 (SG31-SG33).

Each test asserts the correct behaviour and is a strict xfail until its finding
is fixed. Timing repros are calibrated against this machine's own measured
times (one warm hard check, the same merge into an empty vocabulary), never
absolute seconds. Measurements: `uv run python -m scripts.audit_load_8`.
"""

from __future__ import annotations

import statistics

import pytest

from scripts.audit_load_8 import (
    checks_in_forked_child,
    cold_first_check,
    merge_into_vocab_s,
    newcomer_behind_together_s,
    newcomer_beside_fresh_task_runs_s,
)


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-30 #1")
async def test_a_newcomer_does_not_queue_behind_hard_runs_that_started_together() -> None:
    # The AUDIT-26 #1 load as round 7 wrote it: 2 slots + 2 slow places, 12 hard runs
    # started together, and an easy check in a new task 0.1 s later. Its wait should be
    # about one hard check, not the queue of the runs' own first checks (~3).
    waited, unit = await newcomer_behind_together_s(12, 0.4)
    assert waited <= 1.5 * unit, f"waited {waited:.2f} s = {waited / unit:.1f} hard checks"


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-30 #1")
async def test_a_newcomer_beside_runs_that_check_in_fresh_tasks_waits_one_hard_check() -> None:
    # Steady state, no start-up: 12 hard runs whose every check is made in a fresh task,
    # as each agent's verify is (LangGraph runs a node in a task of its own).
    waits, unit = await newcomer_beside_fresh_task_runs_s(12, 0.4)
    waited = statistics.median(waits)
    assert waited <= 1.5 * unit, f"waited {waited:.2f} s = {waited / unit:.1f} hard checks"


@pytest.mark.xfail(strict=True, reason="AUDIT-30 #2")
def test_a_process_forked_after_a_verifier_is_built_can_still_check() -> None:
    # A prefork server (or multiprocessing's default fork on Linux) after `build_graph()`.
    outcomes = checks_in_forked_child(4)
    assert all(o == "VALID entailed" for o in outcomes), outcomes


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-30 #3")
def test_merging_a_reply_does_not_scale_with_the_vocabulary_it_joins() -> None:
    # The same 100-constant reply (admitted by SG33's budget), merged into an empty
    # vocabulary and into one of 20 000 symbols (a long trace, or many facts).
    alone = merge_into_vocab_s(0)
    loaded = merge_into_vocab_s(20_000)
    assert loaded <= 2 * alone, f"{loaded * 1e3:.0f} ms against {alone * 1e3:.0f} ms"


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-30 #4")
def test_the_first_checks_start_up_wait_is_recorded_in_its_verdict() -> None:
    # A fresh process verifies as soon as its Verifier is built (no LLM latency to hide
    # the worker start-up behind). The step's verdict should account for most of the wall.
    r = cold_first_check()
    assert r["recorded_ms"] >= 0.5 * r["first_check_wall_ms"], r
