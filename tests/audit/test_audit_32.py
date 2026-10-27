"""AUDIT-32: test coverage and test quality, round 8 (SG31-SG33).

Each finding in `AUDIT-32.md` that is about a mutant the suite lets through has
two tests here:
- a plain test that kills the mutant and passes today (the missing guard);
- a strict xfail, named for the finding, that applies the mutant with
  `monkeypatch` (`_mutant` from AUDIT-24 recompiles one function with one
  exact-match edit, or a constant is patched) and runs the same check.
Nothing here edits a source file. Waits are bounded by events or by a pipe that
never answers; the one wall-clock test compares two measurements taken on the
same machine in the same test, never absolute seconds.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest
import z3

from autolab.verification import formalizer, solver_workers
from autolab.verification import solver as solver_mod
from autolab.verification.formalizer import FormalizationError, Vocabulary
from autolab.verification.solver import SmtFormula, SolverOutcome, Z3Solver
from tests.audit.test_audit_24 import _mutant, _until

_WAIT_S: Final = 10.0
"""Bounds a wait on an event; a passing run never waits this long."""

_ROOT: Final = Path(__file__).resolve().parents[2]
_DECLS: Final = "(declare-const p Bool)"
_ENTAILED: Final = ([SmtFormula(_DECLS, "p")], SmtFormula(_DECLS, "p"))


# --------------------------------------------------------------------------
# #1 (medium): a worker that never says it is ready is reported as killed past
# the timeout (S04). `Worker.ready` sets `fault = FAILED_TO_START` on two paths:
# the pipe closing (tested) and `_BOOT_S` passing with no ready message
# (solver_workers.py 109-110, uncovered). Without the second, a worker hung at
# boot is "worker killed past the timeout", reason `timeout`: AUDIT-27 #1's
# misreport, for the hang SG31's amendment names.
# --------------------------------------------------------------------------


class _OnePool:
    """`solver._workers()` stand-in that hands out one given worker."""

    def __init__(self, worker: solver_workers.Worker) -> None:
        self.worker = worker
        self.discarded: list[solver_workers.Worker] = []

    def acquire(self) -> solver_workers.Worker:
        return self.worker

    def release(self, worker: solver_workers.Worker) -> None:
        raise AssertionError("a worker that never started was given back")

    def discard(self, worker: solver_workers.Worker) -> None:
        self.discarded.append(worker)
        worker.stop()


def _silent_worker() -> tuple[solver_workers.Worker, Any]:
    """A `Worker` whose process never sends its ready message (the pipe stays open)."""
    parent, child = multiprocessing.Pipe()
    worker = solver_workers.Worker.__new__(solver_workers.Worker)
    worker.conn, worker._ready, worker._seq, worker.fault = parent, False, 0, ""
    worker.process = SimpleNamespace(
        kill=lambda: None, join=lambda: None, exitcode=None, is_alive=lambda: True
    )
    return worker, child


def _outcome_of_a_worker_that_never_boots(monkeypatch: pytest.MonkeyPatch) -> SolverOutcome:
    monkeypatch.setattr(solver_workers, "_BOOT_S", 0.05)  # nothing ever arrives
    worker, child = _silent_worker()
    pool = _OnePool(worker)
    monkeypatch.setattr(solver_mod, "_workers", lambda: pool)
    try:
        out = Z3Solver().check_entailment(*_ENTAILED)
    finally:
        child.close()
    assert pool.discarded == [worker]
    return out


_LOST_AT_BOOT: Final = SolverOutcome("UNGROUNDED", "worker_died", "worker failed to start")


def test_a_worker_that_never_says_ready_failed_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _outcome_of_a_worker_that_never_boots(monkeypatch) == _LOST_AT_BOOT


_S04: Final = (
    "if not self.conn.poll(_BOOT_S):\n                self.fault = FAILED_TO_START\n",
    "if not self.conn.poll(_BOOT_S):\n",
)


@pytest.mark.xfail(strict=True, reason="AUDIT-32 #1")
def test_a_worker_that_never_says_ready_failed_to_start_under_s04(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(monkeypatch, solver_workers.Worker, "ready", *_S04)
    assert _outcome_of_a_worker_that_never_boots(monkeypatch) == _LOST_AT_BOOT


# --------------------------------------------------------------------------
# #2 (medium): nothing pins the forkserver preload (N16, N18). Since SG32 the
# preload list is `(solver_workers, solver)` and the W33 test that touched it is
# gone. With no preload a worker boot imports z3 and the solver itself: 170-190
# ms median here against 5 ms, every start and every replacement after a kill.
# The whole suite passes with `preload=()` or with `set_forkserver_preload`
# never called. Measured in fresh interpreters (the forkserver is per process),
# against the same pool with the preload disabled.
# --------------------------------------------------------------------------

_BOOT_CHILD: Final = """
import multiprocessing.context, sys, time
from autolab.verification import solver as solver_mod, solver_workers
setup = sys.argv[1]
if setup in ("no-preload", "n18"):
    multiprocessing.context.BaseContext.set_forkserver_preload = lambda self, names: None
if setup == "n16":
    solver_mod._shared_workers = solver_workers.Workers(solver_mod._solve, 2, preload=())
pool = solver_mod._workers()
assert pool.acquire().ready()  # starts the forkserver
times = []
for _ in range(5):
    started = time.perf_counter()
    worker = pool._start()
    assert worker.ready()
    times.append(time.perf_counter() - started)
    worker.stop()
print(sorted(times)[2])
"""


def _median_boot_s(setup: str) -> float:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    done = subprocess.run(
        [sys.executable, "-c", _BOOT_CHILD, setup],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return float(done.stdout.split()[-1])


def _boot_ratio(setup: str) -> float:
    """Median worker boot without any preload, over the median under `setup`."""
    return _median_boot_s("no-preload") / _median_boot_s(setup)


@pytest.mark.perf
def test_the_shared_pool_boots_workers_from_a_preloaded_forkserver() -> None:
    ratio = _boot_ratio("default")
    assert ratio > 4, f"a preloaded boot is only {ratio:.1f}x faster than none"


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-32 #2")
@pytest.mark.parametrize("mutant", ["n16", "n18"])
def test_the_shared_pool_boots_workers_from_a_preloaded_forkserver_under(mutant: str) -> None:
    ratio = _boot_ratio(mutant)
    assert ratio > 4, f"a preloaded boot is only {ratio:.1f}x faster than none"


# --------------------------------------------------------------------------
# #3 (medium): the charge test bounds characters, not parse calls (F11). Each
# Z3 parse call is charged `_PARSE_OVERHEAD` (its fixed cost, per the docstring),
# but `test_the_charge_bounds_the_characters_the_probe_parses` compares the
# charge with the characters parsed only, which 3 x len(decl) per parse already
# covers. So a charge that leaves out a parametric sort's applied probe (one
# parse in three) passes everything.
# --------------------------------------------------------------------------


class _ParseCost:
    """Calls to `Z3_parse_smtlib2_string`, and the characters handed to it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = self.chars = 0
        real = z3.Z3_parse_smtlib2_string

        def counting(*args: Any) -> Any:
            self.calls += 1
            self.chars += len(args[1])
            return real(*args)

        monkeypatch.setattr(z3, "Z3_parse_smtlib2_string", counting)


_PROBED: Final = [
    "(declare-sort Box 2)",
    "(declare-sort Atom 0)",
    "(declare-const c Int)",
    "(declare-fun f (Int Real Bool) Bool)",
    f"(declare-fun g ({' '.join(['Int'] * formalizer.MAX_ARITY)}) Bool)",
]


def _undercharged(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    out = []
    for decl in _PROBED:
        formalizer._applied_builtin.cache_clear()
        formalizer._is_z3_builtin.cache_clear()
        with monkeypatch.context() as mp:
            cost = _ParseCost(mp)
            Vocabulary().merged(decl)
        charge = formalizer._probe_chars(formalizer._decl_tokens(decl), decl, ())
        if charge < cost.calls * formalizer._PARSE_OVERHEAD + cost.chars:
            out.append(decl)
    return out


def test_the_charge_covers_every_parse_calls_fixed_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _undercharged(monkeypatch) == []


@pytest.mark.xfail(strict=True, reason="AUDIT-32 #3")
def test_the_charge_covers_every_parse_calls_fixed_cost_under_f11(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(monkeypatch, formalizer, "_probe_chars", "parses += arity > 0\n", "parses += 0\n")
    assert _undercharged(monkeypatch) == []


# --------------------------------------------------------------------------
# #4 (low): newcomers "rank with short callers" (ADR 0009, SG32), but no test
# pins how a newcomer and a short caller are ordered. Ranking newcomers above
# short callers (N10) or between short and hard callers (N09) passes the suite.
# Today they are served in arrival order.
# --------------------------------------------------------------------------


async def _first_served(first: str) -> str:
    """Which of a short caller and a newcomer, queued in that order, gets a freed slot."""
    slots = solver_mod._Slots(1)
    assert await slots.acquire(False) is False  # the slot is held
    kinds = {"short": (True, False), "new": (False, True)}
    order = [first, *(k for k in kinds if k != first)]
    tasks = {}
    for i, kind in enumerate(order):
        tasks[kind] = asyncio.create_task(slots.acquire(*kinds[kind]))
        await _until(lambda i=i: len(slots._waiters) == i + 1)  # type: ignore[misc]
    slots.release(False)
    await _until(lambda: any(t.done() for t in tasks.values()))
    (served,) = [k for k, t in tasks.items() if t.done()]
    slots.release(False)
    await asyncio.gather(*tasks.values())
    return served


async def test_a_newcomer_and_a_short_caller_are_served_in_arrival_order() -> None:
    assert [await _first_served("short"), await _first_served("new")] == ["short", "new"]


_N09: Final = (
    "1 if entry.short or entry.new else 2",
    "1 if entry.short else 1.5 if entry.new else 2",
)
_N10: Final = (
    "1 if entry.short or entry.new else 2",
    "0 if entry.new else 1 if entry.short else 2",
)


@pytest.mark.xfail(strict=True, reason="AUDIT-32 #4")
@pytest.mark.parametrize("edit", [_N09, _N10], ids=["n09", "n10"])
async def test_a_newcomer_and_a_short_caller_are_served_in_arrival_order_under(
    monkeypatch: pytest.MonkeyPatch, edit: tuple[str, str]
) -> None:
    _mutant(monkeypatch, solver_mod._Slots, "_rank", *edit)
    assert [await _first_served("short"), await _first_served("new")] == ["short", "new"]


# --------------------------------------------------------------------------
# #5 (low): `_lost`'s exit-code branch is untested (S12). A worker that exits on
# its own (a Z3 abort, `os._exit` in a native library) is reported as "signal -3"
# under the mutant; nothing notices. Here the worker's target is `os._exit`.
# --------------------------------------------------------------------------


@pytest.fixture
def exiting_pool() -> Iterator[solver_workers.Workers]:
    workers = solver_workers.Workers(os._exit, idle_max=1)
    yield workers
    workers.close()


def _details_of_a_worker_that_exits(pool: solver_workers.Workers) -> str:
    worker = pool.acquire()
    assert worker.call((3,), _WAIT_S) is None
    assert worker.fault == solver_workers.DIED
    pool.discard(worker)  # as `check_entailment` does before `_lost`
    return solver_mod._lost(worker).details


def test_a_worker_that_exits_is_reported_with_its_exit_code(
    exiting_pool: solver_workers.Workers,
) -> None:
    assert _details_of_a_worker_that_exits(exiting_pool) == "worker died (exit code 3)"


@pytest.mark.xfail(strict=True, reason="AUDIT-32 #5")
def test_a_worker_that_exits_is_reported_with_its_exit_code_under_s12(
    monkeypatch: pytest.MonkeyPatch, exiting_pool: solver_workers.Workers
) -> None:
    _mutant(
        monkeypatch,
        solver_mod,
        "_lost",
        'details += f" (signal {-code})" if code < 0 else f" (exit code {code})"',
        'details += f" (signal {-code})"',
    )
    assert _details_of_a_worker_that_exits(exiting_pool) == "worker died (exit code 3)"


# --------------------------------------------------------------------------
# #6 (low): two documented constants are read, not pinned. Every SG31 and SG33
# test reads `TIMEOUT_MAX_S` and `MAX_NESTING` from the module, so a cap of 10^5
# s (S15: a configured 5 days silently becomes 28 h) or a nesting cap of 16 (F01:
# 4x the quadratic parse cost the cap exists for) passes the suite. The AUDIT-26
# #2 repro refuses depths 400 and 800, so any cap up to 399 passes it too. The
# documented values (ADR 0009 SG31 amendment: 10^6 s; SG33: 8) are hard-coded.
# --------------------------------------------------------------------------


def _clamped(timeouts: tuple[float, ...]) -> list[float]:
    return [Z3Solver(t)._args(*_ENTAILED)[-1] for t in timeouts]


def test_the_timeout_cap_is_the_documented_one() -> None:
    assert _clamped((5e5, 1e6, 5e6)) == [5e5, 1e6, 1e6]


@pytest.mark.xfail(strict=True, reason="AUDIT-32 #6")
def test_the_timeout_cap_is_the_documented_one_under_s15(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(solver_mod, "TIMEOUT_MAX_S", 1e5)
    assert _clamped((5e5, 1e6, 5e6)) == [5e5, 1e6, 1e6]


def _nested(depth: int) -> str:
    sort = "Int"
    for _ in range(depth):
        sort = f"(Array Int {sort})"
    return sort


def _accepted_depths(depths: range) -> list[int]:
    out = []
    for depth in depths:
        try:
            Vocabulary().merged(f"(declare-const g {_nested(depth)})")
        except FormalizationError as exc:
            assert "deep" in str(exc)
        else:
            out.append(depth)
    return out


def test_the_nesting_cap_is_the_documented_one() -> None:
    assert _accepted_depths(range(6, 12)) == [6, 7, 8]


@pytest.mark.xfail(strict=True, reason="AUDIT-32 #6")
def test_the_nesting_cap_is_the_documented_one_under_f01(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(formalizer, "MAX_NESTING", 16)
    assert _accepted_depths(range(6, 12)) == [6, 7, 8]
