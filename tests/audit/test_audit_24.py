"""AUDIT-24: test coverage and test quality, round 6 (SG24-SG27).

Each finding in `AUDIT-24.md` has a strict xfail here, named in its reason.
- A finding about production behaviour asserts the correct behaviour and fails today.
- A finding about a mutant that survives the suite applies that mutant with
  `monkeypatch` (`_mutant` recompiles one function with one exact-match edit, or a
  constant is patched) and runs a check that kills it, so it fails while the mutant
  is applied. The same check, unmutated, runs as a plain test and passes today: it
  is the missing guard.
Nothing here edits a source file; waits are bounded by events, not wall clocks,
except where a finding is about wall-clock behaviour.
"""

from __future__ import annotations

import asyncio
import inspect
import textwrap
import threading
import time
import types
from collections.abc import Callable
from typing import Any, Final

import pytest
import z3

from autolab.agents import common, design
from autolab.verification import formalizer
from autolab.verification import solver as solver_mod
from autolab.verification.formalizer import Vocabulary
from autolab.verification.solver import SolverExecutor, Z3Solver, _Pool
from tests.audit import test_audit_14 as audit_14
from tests.unit.agents.test_design_estimate import DESIGN, _fake, _run
from tests.unit.verification.test_solver_loading import _chain

_WAIT_S: Final = 10.0
"""Bounds a wait on an event; a passing run never waits this long."""


def _mutant(
    monkeypatch: pytest.MonkeyPatch, owner: type | types.ModuleType, name: str, old: str, new: str
) -> None:
    """Replace `owner.name` with a copy of it whose source has `old` replaced by `new`."""
    func = inspect.getattr_static(owner, name)
    source = textwrap.dedent(inspect.getsource(func))
    assert source.count(old) == 1, f"{old!r} occurs {source.count(old)} times in {name}"
    namespace: dict[str, Any] = dict(func.__globals__)
    code = "from __future__ import annotations\n" + source.replace(old, new)
    exec(compile(code, f"<AUDIT-24 mutant of {name}>", "exec"), namespace)
    compiled = namespace[name]
    # Bound to the module's own globals, so later patches of its constants reach it.
    mutated = types.FunctionType(compiled.__code__, func.__globals__, name, compiled.__defaults__)
    monkeypatch.setattr(owner, name, mutated)


async def _until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + _WAIT_S
    while not condition():
        assert time.monotonic() < deadline, "condition never held"
        await asyncio.sleep(0.005)


def _until_sync(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + _WAIT_S
    while not condition():
        assert time.monotonic() < deadline, "condition never held"
        time.sleep(0.005)


class _Held:
    """A call that attaches a Z3 context, as a running check does, until released."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.gate = threading.Event()
        self.interrupt: solver_mod._Interrupt | None = None

    def __call__(self) -> str:
        interrupt: solver_mod._Interrupt = solver_mod._worker.interrupt
        self.interrupt = interrupt
        assert interrupt.attach(z3.Context())
        self.started.set()
        try:
            self.gate.wait(_WAIT_S)
        finally:
            interrupt.detach()
        return "held"


# --------------------------------------------------------------------------
# #2 (medium): a check's hardness is counted in event-loop wake-ups, not time.
# `SolverExecutor.run` counts `slices` as the `asyncio.wait(timeout=_SLICE_S)` calls
# that time out. While the caller's loop is busy (formalizing, judging, other
# callers), a check that runs 10 slices of wall time is counted as 0 or 1, so a
# hard run keeps the short-caller priority and lent places SG26 reserves for easy
# ones (AUDIT-18 #4). The SG26 test (`..._ran_long_is_not_lent_a_place`) sleeps
# 5 slices on an idle loop, so it passes on a quiet machine and flakes under load.
# --------------------------------------------------------------------------


async def test_a_check_that_ran_long_while_the_loop_was_busy_counts_as_hard() -> None:
    executor = SolverExecutor(1, 0)
    started = threading.Event()

    def long_check() -> None:
        started.set()
        time.sleep(10 * solver_mod._SLICE_S)

    async def caller() -> asyncio.Task[Any] | None:
        await executor.run(long_check)
        return asyncio.current_task()

    try:
        task = asyncio.create_task(caller())
        assert await asyncio.to_thread(started.wait, _WAIT_S)
        time.sleep(12 * solver_mod._SLICE_S)  # this loop is busy elsewhere meanwhile
        assert await task is task
    finally:
        executor.shutdown()
    assert task not in executor._short  # its check ran 10 slices: a hard run


# --------------------------------------------------------------------------
# #1 (high): since SG27 the AUDIT-14 #1 repros no longer catch the bug they name.
# Fixed by SG29: checks run in worker processes, the pool is Int (parsed in one
# call, as before SG27), and the timeout repro times `_solve` in this process, so a
# kill cannot hide an unbounded check. The mutant is applied to `_solve`.
# Their 1600-premise pool is now loaded one formula at a time, and on that path the
# SG19 assumption-based entailment check honours its timeout and cancels: with it
# restored (AUDIT-20's S01), both repros and AUDIT-20 #3's pass, 3 of 3. The phase
# Z3 does not interrupt is on the one-call path (pools of at most `_LOAD_ABOVE`),
# which no wall-clock repro exercises any more. S01 is still killed, but only by the
# two tests that count `check` arguments, not by any test of the behaviour.
# --------------------------------------------------------------------------

_ASSUME: Final = (
    "solver.add(z3.Bool(_NEGATED_CLAIM, ctx))\n"
    '        solver.set("timeout", _remaining_ms(deadline))\n'
    "        result = solver.check()\n",
    'solver.set("timeout", _remaining_ms(deadline))\n'
    "        result = solver.check(z3.Bool(_NEGATED_CLAIM, ctx))\n",
)


@pytest.mark.perf
def test_the_audit_14_1_timeout_repro_fails_with_the_assumption_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(monkeypatch, solver_mod, "_solve", *_ASSUME)
    with pytest.raises(AssertionError):
        audit_14.test_a_check_returns_within_its_timeout()


@pytest.mark.perf
def test_on_the_one_call_path_the_audit_14_1_repro_catches_the_assumption_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The suggested fix: the same repro with its pool kept on the one-call path.
    _mutant(monkeypatch, solver_mod, "_solve", *_ASSUME)
    monkeypatch.setattr(solver_mod, "_LOAD_ABOVE", 10 * audit_14._POOL)
    with pytest.raises(AssertionError):
        audit_14.test_a_check_returns_within_its_timeout()


# --------------------------------------------------------------------------
# #3 (medium): a check running in a lent place must not stick and lend again
# (mutant S06 survived). Only a check holding a real slot may lend: otherwise each
# lent check that runs past a slice lends one more place, and short callers run
# beyond `workers + stuck` slots (the CPU oversubscription of AUDIT-18 #5).
# --------------------------------------------------------------------------

_S06: Final = (
    "elif not (stuck or lent) and interrupt.attached():",
    "elif not stuck and interrupt.attached():",
)


async def _a_lent_check_never_lends() -> tuple[int, int]:
    executor = SolverExecutor(1, 0)
    try:
        await executor.run(lambda: None)  # this task's last check was short
        hard, lent = _Held(), _Held()
        hard_task = asyncio.create_task(executor.run(hard))
        assert await asyncio.to_thread(hard.started.wait, _WAIT_S)
        slots = executor._slots[asyncio.get_running_loop()]
        await _until(lambda: slots.stuck == 1)
        slices: list[int] = []
        real_to_slow = executor._to_slow

        def to_slow(interrupt: solver_mod._Interrupt) -> bool:
            out = real_to_slow(interrupt)
            if interrupt is lent.interrupt:
                slices.append(1)
            return out

        executor._to_slow = to_slow  # type: ignore[method-assign]
        seen: list[tuple[int, int]] = []

        async def drive() -> None:
            assert await asyncio.to_thread(lent.started.wait, _WAIT_S)
            await _until(lambda: len(slices) >= 3)  # the lent check is past its slice
            seen.append((slots.lent, slots.stuck))
            lent.gate.set()
            hard.gate.set()

        driver = asyncio.create_task(drive())
        assert (await executor.run(lent))[0] == "held"
        await asyncio.gather(driver, hard_task)
    finally:
        executor.shutdown()
    return seen[0]


async def test_a_lent_check_never_lends_a_place() -> None:
    assert await _a_lent_check_never_lends() == (1, 1)


async def test_a_lent_check_never_lends_a_place_under_s06(monkeypatch: pytest.MonkeyPatch) -> None:
    _mutant(monkeypatch, SolverExecutor, "run", *_S06)
    assert await _a_lent_check_never_lends() == (1, 1)


# --------------------------------------------------------------------------
# #4 (medium): SG27's loading checks run without a Z3 limit once `or not limit`
# goes (mutant L01 survived). `_load` starts with `limit = 0`, so only that clause
# sets a timeout before the first loading check; without it no loading check has
# one, and only the deadline poll between checks bounds the pool. The SG27 test
# `test_a_loading_check_never_gets_much_more_than_the_time_left` passes regardless:
# it compares the last limit set, which stays 0, against the time left.
# --------------------------------------------------------------------------

_L01: Final = (
    "if remaining < _RESET_SHARE * limit or not limit:",
    "if remaining < _RESET_SHARE * limit:",
)


def _limits_at_the_first_loading_check(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    limits: list[int] = []
    at_first: list[list[int]] = []
    real_set, real_load = z3.Solver.set, z3.Z3_solver_check

    def record(self: z3.Solver, *args: object, **kwargs: object) -> None:
        if args and args[0] == "timeout":
            limits.append(int(str(args[1])))
        real_set(self, *args, **kwargs)

    def load(ctx: object, solver: object) -> int:
        if not at_first:
            at_first.append(list(limits))
        return int(real_load(ctx, solver))

    monkeypatch.setattr(z3.Solver, "set", record)
    monkeypatch.setattr(z3, "Z3_solver_check", load)
    premises, claim = _chain(solver_mod._LOAD_ABOVE + 20)
    out = Z3Solver(30.0)._check_here(premises, claim)  # `_solve`, as its worker runs it
    assert out.reason == "not_entailed"
    return at_first[0]


def test_every_loading_check_runs_under_a_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    limits = _limits_at_the_first_loading_check(monkeypatch)
    assert limits and 0 < limits[-1] <= 30_000


def test_every_loading_check_runs_under_a_limit_kills_l01(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AUDIT-24 #4, pinned: the guard above (and its twin in `test_solver_loading.py`)
    # fails under L01.
    _mutant(monkeypatch, solver_mod, "_load", *_L01)
    limits = _limits_at_the_first_loading_check(monkeypatch)
    assert not (limits and 0 < limits[-1] <= 30_000)


# --------------------------------------------------------------------------
# #5 (medium): the SG27 bound on a loading check's limit is derived from the constant
# it bounds (mutant L02, `_RESET_SHARE = 0.1`, survived). The test asserts each limit
# is at most `time left / _RESET_SHARE`, so any share passes it; at 0.1 a loading
# check may be given 10x the time left. Here the bound is SG27's documented 0.9.
# --------------------------------------------------------------------------

_DOCUMENTED_RESET_SHARE: Final = 0.9


class _Clock:
    """`solver.time` with a `monotonic` the test advances."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.perf_counter = time.perf_counter

    def monotonic(self) -> float:
        return self.now


def _loading_limits_over_the_time_left(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Each loading check "takes" 1% of a 1 s budget; the (limit, ms left) pairs where
    the limit Z3 runs under exceeds the time left by more than the documented share."""
    clock = _Clock()
    deadline = clock.now + 1.0
    limit = [0]
    over: list[tuple[int, int]] = []
    real_set, real_load = z3.Solver.set, z3.Z3_solver_check

    def record(self: z3.Solver, *args: object, **kwargs: object) -> None:
        if args and args[0] == "timeout":
            limit[0] = int(str(args[1]))
        real_set(self, *args, **kwargs)

    def load(ctx: object, solver: object) -> int:
        left_ms = int((deadline - clock.now) * 1000)
        if limit[0] > left_ms / _DOCUMENTED_RESET_SHARE + 1:
            over.append((limit[0], left_ms))
        clock.now += 0.01
        return int(real_load(ctx, solver))

    monkeypatch.setattr(solver_mod, "time", clock)
    monkeypatch.setattr(z3.Solver, "set", record)
    monkeypatch.setattr(z3, "Z3_solver_check", load)
    premises, claim = _chain(solver_mod._LOAD_ABOVE + 20)
    assert Z3Solver(1.0)._check_here(premises, claim).reason == "timeout"
    return over


def test_a_loading_check_never_gets_more_than_the_documented_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _loading_limits_over_the_time_left(monkeypatch) == []


def test_a_loading_check_never_gets_more_than_the_documented_share_kills_l02(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AUDIT-24 #5, pinned: the guard above (and its twin in `test_solver_loading.py`)
    # fails under L02.
    monkeypatch.setattr(solver_mod, "_RESET_SHARE", 0.1)
    assert _loading_limits_over_the_time_left(monkeypatch) != []


# --------------------------------------------------------------------------
# #6 (medium): which pools are loaded one formula at a time is pinned only relative
# to `_LOAD_ABOVE` (mutant L03, `_LOAD_ABOVE = 2000`, survived `-m "not perf"`).
# Every SG27 unit test builds `_LOAD_ABOVE + 20` premises, so raising the threshold
# past AUDIT-18 #3's 999-premise pool puts that pool back on the one-call path,
# where Z3's simplex ignores timeouts and interrupts; only the `perf` repros see it.
# --------------------------------------------------------------------------

_AUDIT_18_3_POOL: Final = 1000
"""AUDIT-18 #3's Real-valued trace length: 999 chained premises."""


def _loading_checks_for_the_audit_18_3_pool(monkeypatch: pytest.MonkeyPatch) -> int:
    loads = [0]
    real_load = z3.Z3_solver_check

    def load(ctx: object, solver: object) -> int:
        loads[0] += 1
        return int(real_load(ctx, solver))

    monkeypatch.setattr(z3, "Z3_solver_check", load)
    premises, claim = _chain(_AUDIT_18_3_POOL, claim="true")
    assert Z3Solver(30.0)._check_here(premises, claim).verdict == "VALID"
    return loads[0]


def test_the_audit_18_3_pool_is_loaded_one_formula_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _loading_checks_for_the_audit_18_3_pool(monkeypatch) == _AUDIT_18_3_POOL - 1


def test_the_audit_18_3_pool_is_loaded_one_formula_at_a_time_kills_l03(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AUDIT-24 #6, pinned: the guard above (and its twin in `test_solver_loading.py`)
    # fails under L03.
    monkeypatch.setattr(solver_mod, "_LOAD_ABOVE", 2000)
    assert _loading_checks_for_the_audit_18_3_pool(monkeypatch) != _AUDIT_18_3_POOL - 1


# --------------------------------------------------------------------------
# Low findings: survivors with a small effect.
# --------------------------------------------------------------------------

# L1 (A01): only one of the 49 look-alike letters SG25 folds is tested (Cyrillic shha).
_LOOKALIKES: Final = (
    f"Incubate for 48 h{chr(0x043E)}urs",  # Cyrillic o
    f"Incubate for 3 d{chr(0x0430)}ys",  # Cyrillic a
    f"Incubate for 48 h{chr(0x03BF)}urs",  # Greek omicron
    f"Incubate for 2 w{chr(0x0435) * 2}ks",  # Cyrillic e
)
# L2 (A02): only the soft hyphen and zero-width space are tested of the format characters.
_INVISIBLE: Final = (
    "Incubate for 48⁠hours",  # word joiner
    "Incubate for 48﻿hours",  # zero-width no-break space
    "Incubate for 48‍hours",  # zero-width joiner
    "Incubate for 48⁣hours",  # invisible separator
)
# L3 (A11): the `8-week-old` exemption must look at the word right after the unit only.
_NOT_AN_AGE: Final = ("incubate 48 hours with old medium", "incubate 48 hours, using old plates")


def _all_flagged(protocols: tuple[str, ...]) -> None:
    for protocol in protocols:
        assert common.protocol_figures(protocol), protocol


def test_look_alike_letters_invisible_characters_and_non_ages_are_flagged() -> None:
    _all_flagged(_LOOKALIKES + _INVISIBLE + _NOT_AN_AGE)


@pytest.mark.xfail(strict=True, reason="AUDIT-24 L1")
def test_look_alike_letters_are_flagged_under_a01(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "_CONFUSABLES", str.maketrans(chr(0x04BB), "h"))
    _all_flagged(_LOOKALIKES)


@pytest.mark.xfail(strict=True, reason="AUDIT-24 L2")
def test_invisible_characters_are_flagged_under_a02(monkeypatch: pytest.MonkeyPatch) -> None:
    old = 'if unicodedata.category(c) != "Cf"'
    _mutant(monkeypatch, common, "_normalized", old, 'if c not in "\\u00ad\\u200b"')
    _all_flagged(_INVISIBLE)


@pytest.mark.xfail(strict=True, reason="AUDIT-24 L3")
def test_a_later_old_is_not_an_age_under_a11(monkeypatch: pytest.MonkeyPatch) -> None:
    old = 'words[1:2] == ["old"]'
    _mutant(monkeypatch, common, "protocol_figures", old, '"old" in words[1:]')
    _all_flagged(_NOT_AN_AGE)


# L4 (D04): the duration floor is pinned only from above (10 -> 3 survives).
async def _a_minute_long_design_agrees_with_a_nine_minute_estimate() -> None:
    tiny = DESIGN.model_copy(update={"estimated_duration_minutes": 1.0})
    update = await _run(_fake(tiny, estimate=(5.0, 9.5)))
    assert update["hitl_pending"] is None


async def test_figures_under_the_duration_floor_compare_as_ten_minutes() -> None:
    await _a_minute_long_design_agrees_with_a_nine_minute_estimate()


@pytest.mark.xfail(strict=True, reason="AUDIT-24 L4")
async def test_figures_under_the_duration_floor_under_d04(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(design, "ESTIMATE_FLOOR_MINUTES", 3.0)
    await _a_minute_long_design_agrees_with_a_nine_minute_estimate()


# L5 (D06): on a blocked trace the policy runs on the larger of each figure; with
# the estimate's own cost instead, a design cost over the limit is dropped from the
# reason the reviewer reads.
async def _a_blocked_trace_names_the_larger_cost() -> None:
    costly = DESIGN.model_copy(update={"estimated_cost_usd": 15.0})
    update = await _run(_fake(costly, estimate=(6.0, 40.0), passing=False))
    assert update["hitl_pending"].reason.endswith(
        "; independent estimate: estimated cost $15.00 > $10.00"
    )


async def test_a_blocked_trace_names_the_policy_on_the_larger_figures() -> None:
    await _a_blocked_trace_names_the_larger_cost()


@pytest.mark.xfail(strict=True, reason="AUDIT-24 L5")
async def test_a_blocked_trace_names_the_larger_cost_under_d06(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = '"estimated_cost_usd": max(design.estimated_cost_usd, estimate.estimated_cost_usd)'
    _mutant(
        monkeypatch, design, "_at_least", old, '"estimated_cost_usd": estimate.estimated_cost_usd'
    )
    await _a_blocked_trace_names_the_larger_cost()


# L6 (S01, S02): `_Pool` reuses an idle thread instead of starting one, and keeps a
# thread at its limit idle instead of ending it. Every SG26 test counts threads only
# while calls are held, so neither the idle hand-off nor the exit bound is pinned.
_S01: Final = ("while self._unclaimed and self._idle:", "while False:")
_S02: Final = (
    "if len(self._threads) > self._max_workers:",
    "if len(self._threads) >= self._max_workers:",
)


def _an_idle_thread_takes_the_next_call(limit: int) -> None:
    pool = _Pool(limit)
    try:
        first = pool.submit(threading.current_thread).result(_WAIT_S)
        _until_sync(lambda: pool._idle == 1)
        second = pool.submit(threading.current_thread).result(_WAIT_S)
        assert second is first
        assert pool._threads == {first}
    finally:
        pool.shutdown()


@pytest.mark.parametrize("limit", [1, 2])
def test_an_idle_thread_takes_the_next_call(limit: int) -> None:
    _an_idle_thread_takes_the_next_call(limit)


@pytest.mark.xfail(strict=True, reason="AUDIT-24 L6")
def test_an_idle_thread_takes_the_next_call_under_s01(monkeypatch: pytest.MonkeyPatch) -> None:
    _mutant(monkeypatch, _Pool, "_claim", *_S01)
    _an_idle_thread_takes_the_next_call(2)


@pytest.mark.xfail(strict=True, reason="AUDIT-24 L6")
def test_an_idle_thread_takes_the_next_call_under_s02(monkeypatch: pytest.MonkeyPatch) -> None:
    _mutant(monkeypatch, _Pool, "_next", *_S02)
    _an_idle_thread_takes_the_next_call(1)


# L7 (F14): the probe's cache key holds the sorts the declaration line mentions,
# not those its whole batch mentions (only the cost of re-probing is at stake).
_F14: Final = ("mentioned = _tokens(stripped)", "mentioned = _tokens(new_decls)")


def _a_batch_with_a_new_sort_reuses_the_probe() -> None:
    formalizer._applied_builtin.cache_clear()
    decl = "(declare-fun g (U Int Int) Bool)"
    base = Vocabulary().merged("(declare-sort U 0)")
    base.merged(decl)
    hits = formalizer._applied_builtin.cache_info().hits
    base.merged(f"(declare-sort V 0)\n{decl}")
    assert formalizer._applied_builtin.cache_info().hits == hits + 1  # `g`'s probe


def test_a_batch_with_a_new_sort_reuses_the_probe() -> None:
    _a_batch_with_a_new_sort_reuses_the_probe()


@pytest.mark.xfail(strict=True, reason="AUDIT-24 L7")
def test_a_batch_with_a_new_sort_reuses_the_probe_under_f14(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(monkeypatch, Vocabulary, "merged", *_F14)
    _a_batch_with_a_new_sort_reuses_the_probe()


# L8 (L06): on the loaded path the `_SETUP_SHARE` guard measures only the final
# consistency check, not the loading before it, so a pool that spent most of its
# budget loading still gets its entailment check (what the AUDIT-14 #1 timeout repro
# now relies on). No deterministic test pins which time the guard measures.
_LOADED: Final = "consistency = z3.sat if loading is None else _load(solver, loading, deadline)\n"
_L06: Final = (
    f"    {_LOADED}    started = time.monotonic()\n",
    f"    started = time.monotonic()\n    {_LOADED}",
)


def _a_pool_that_loaded_slowly_gets_its_entailment_check(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    loads = [0]
    real_load = z3.Z3_solver_check

    def load(ctx: object, solver: object) -> int:
        loads[0] += 1
        if loads[0] == 1:
            clock.now += 0.7  # loading takes 0.7 s of a 1 s budget
        return int(real_load(ctx, solver))

    monkeypatch.setattr(solver_mod, "time", clock)
    monkeypatch.setattr(z3, "Z3_solver_check", load)
    premises, claim = _chain(solver_mod._LOAD_ABOVE + 20)
    out = Z3Solver(1.0)._check_here(premises, claim)
    assert (out.verdict, out.reason) == ("INVALID", "not_entailed")


def test_a_pool_that_loaded_slowly_gets_its_entailment_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _a_pool_that_loaded_slowly_gets_its_entailment_check(monkeypatch)


@pytest.mark.xfail(strict=True, reason="AUDIT-24 L8")
def test_a_pool_that_loaded_slowly_gets_its_entailment_check_under_l06(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(monkeypatch, solver_mod, "_solve", *_L06)
    _a_pool_that_loaded_slowly_gets_its_entailment_check(monkeypatch)
