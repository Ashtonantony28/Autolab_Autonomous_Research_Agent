"""AUDIT-26 repros: performance under load, round 7 (SG28-SG30).

Each test asserts the correct behaviour and is a strict xfail until its finding
is fixed. Timing repros are calibrated against this machine's own measured
times (one hard check, one Z3 parse, the same probe at half the size), never
absolute seconds. Measurements: `uv run python -m scripts.audit_load_7`.
"""

from __future__ import annotations

import time
from contextlib import suppress

import pytest

from autolab.verification.formalizer import FormalizationError
from scripts.audit_load_7 import (
    cold_start,
    deep_sort_decl,
    main_imports_per_boot,
    merge_s,
    newcomer_first_check_s,
    parse_once_s,
    unguarded_outcome,
    wide_reply,
)


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-26 #1")
async def test_a_new_tasks_first_check_does_not_queue_behind_every_hard_run() -> None:
    # 2 slots + 2 slow places, 12 hard runs: 4 checking, 8 queued. A new task's first
    # (easy) check should wait at most about one hard check, not for the queue (~3).
    waited, unit = await newcomer_first_check_s(12, 0.4)
    assert waited <= 1.5 * unit, f"waited {waited:.2f} s = {waited / unit:.1f} hard checks"


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-26 #2")
def test_probing_a_reply_of_many_declarations_costs_a_few_parses_of_it() -> None:
    # 100 declarations at MAX_ARITY: ~15 KB, well under the client's max_tokens.
    decls = wide_reply(100)
    unit = min(parse_once_s(decls) for _ in range(3))
    started = time.perf_counter()
    with suppress(FormalizationError):
        merge_s(decls)
    took = time.perf_counter() - started
    assert took <= 50 * unit, f"merged in {took * 1e3:.0f} ms = {took / unit:.0f} parses"


def _merge_or_refuse_s(decls: str) -> float:
    started = time.perf_counter()
    with suppress(FormalizationError):
        merge_s(decls)  # clears the probe's caches first
    return time.perf_counter() - started


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-26 #2")
def test_probing_a_nested_sort_is_linear_in_its_depth() -> None:
    half = min(_merge_or_refuse_s(deep_sort_decl(400)) for _ in range(2))
    full = min(_merge_or_refuse_s(deep_sort_decl(800)) for _ in range(2))
    assert full <= 2.5 * half, f"twice the depth costs {full / half:.1f}x"


@pytest.mark.xfail(strict=True, reason="AUDIT-26 #3")
def test_workers_start_before_a_fresh_process_requests_its_first_check() -> None:
    # 6-step verify with 100 ms LLM calls: 0.7 s of formalization before any check.
    result = cold_start(inline=True)
    assert result["start_before_check"], result


@pytest.mark.xfail(strict=True, reason="AUDIT-26 #4")
def test_worker_boots_do_not_rerun_the_main_scripts_imports() -> None:
    # The parent and the forkserver import the main's modules; a worker should not.
    assert main_imports_per_boot(3) <= 2


def test_an_unguarded_main_is_not_reported_as_a_solver_timeout() -> None:
    verdict, reason, details, _ = unguarded_outcome()
    assert verdict == "VALID" or reason != "timeout", details
