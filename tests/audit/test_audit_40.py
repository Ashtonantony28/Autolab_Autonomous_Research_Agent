"""AUDIT-40: test coverage and test quality, round 10 (SG35).

Each finding in `AUDIT-40.md` about a mutant the suite lets through has two tests here:
- a plain test that kills the mutant and passes today (the missing guard);
- a strict xfail, named for the finding, that applies the mutant with `monkeypatch`
  (`_mutant` from AUDIT-24 recompiles one function with one exact-match edit) and
  runs the same check.
#5 and #6 are about existing tests, not mutants: each strict xfail fixes the
condition under which that test goes wrong (a machine that slows after the
calibration; a prestart thread that runs late), so it fails the same way every
time. Waits are bounded by events, polls of the executor or a fake clock; the one
`perf` pair compares measurements taken on this machine in the same test.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, Final

import pytest

from autolab.verification import solver as solver_mod
from autolab.verification import solver_workers
from autolab.verification.solver import SmtFormula, SolverExecutor, Z3Solver
from tests.audit import test_audit_14 as audit_14
from tests.audit.test_audit_24 import _ASSUME, _Held, _mutant, _until
from tests.unit.verification import test_solver_executor

_WAIT_S: Final = 10.0
"""Bounds a wait on an event; a passing run never waits this long."""


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def slices(self, n: float) -> None:
        self.now += n * solver_mod._SLICE_S


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(solver_mod, "_clock", fake)
    return fake


@pytest.fixture
def executors() -> Iterator[list[SolverExecutor]]:
    made: list[SolverExecutor] = []
    yield made
    for executor in made:
        executor.shutdown()


class _Target:
    def interrupt(self) -> None:
        """Nothing to stop: these checks end when their gate opens."""


# --------------------------------------------------------------------------
# #1 (low): the exception path's pauses are untested (S04, I03 survive). SG35
# replaced the `perf_counter` stamps with `_Interrupt.pause()/resume()` around
# the worker's start, boot and replacement. The suite pins the normal and the
# killed paths (test_solver_ms_excludes_worker_start_up, AUDIT-28 W09/W10), not
# the one where the check raises while its worker is mid-check (a per-test
# timeout's signal): without its `pause()` the worker's stop and replacement
# count as run time, so the task can be recorded as hard; and if `ready` is
# what raises, `pause()` without its guard restarts the pause and counts the
# boot (I03). Here a fake pool and worker advance a fake clock by a different
# number of slices in each phase: acquire 1, ready 2, call 4, release or
# discard 8. Only the call's 4 slices are run time, on every path, and nothing
# counts while the worker is acquired or boots.
# --------------------------------------------------------------------------

_PHASES: Final = {"acquire": 1, "ready": 2, "call": 4, "release": 8, "discard": 8}


class _Boom(BaseException):
    """A signal's exception (a per-test timeout) raised inside a check."""


class _FakeWorker:
    def __init__(self, clock: _Clock, path: str, seen: dict[str, float]) -> None:
        self.clock, self.path, self.seen = clock, path, seen
        self.fault = ""

    def interrupt(self) -> None:
        """Never stopped here."""

    def ready(self) -> bool:
        self.clock.slices(_PHASES["ready"])
        self.seen["booted"] = solver_mod._worker.interrupt.ran_s()
        if self.path == "boot raised":
            raise _Boom
        return True

    def call(self, args: tuple[Any, ...], budget_s: float) -> Any:
        self.clock.slices(_PHASES["call"])
        if self.path == "raised":
            raise _Boom
        return None if self.path == "lost" else (True, solver_mod._NO_PREMISES)


class _FakePool:
    def __init__(self, clock: _Clock, path: str, seen: dict[str, float]) -> None:
        self.clock, self.path, self.seen = clock, path, seen

    def acquire(self) -> _FakeWorker:
        self.clock.slices(_PHASES["acquire"])
        self.seen["acquired"] = solver_mod._worker.interrupt.ran_s()
        return _FakeWorker(self.clock, self.path, self.seen)

    def release(self, worker: _FakeWorker) -> None:
        self.clock.slices(_PHASES["release"])

    def discard(self, worker: _FakeWorker) -> None:
        self.clock.slices(_PHASES["discard"])


_PATHS: Final = ("reply", "lost", "raised", "boot raised")
_D: Final = "(declare-const p Bool)"


def _run_time_in_slices(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> dict[str, Any]:
    """Per path: the check's run time, and its run time seen while acquiring and booting."""
    out: dict[str, Any] = {}
    for path in _PATHS:
        seen: dict[str, float] = {}
        pool = _FakePool(clock, path, seen)
        monkeypatch.setattr(solver_mod, "_workers", lambda pool=pool: pool)
        interrupt = solver_mod._Interrupt()
        solver_mod._worker.interrupt = interrupt
        interrupt.started = clock()
        try:
            Z3Solver().check_entailment([SmtFormula(_D, "p")], SmtFormula(_D, "p"))
        except _Boom:
            pass  # the paths that raise; the run time is what is checked
        finally:
            interrupt.ended = clock()
            solver_mod._worker.interrupt = None
        slice_s = solver_mod._SLICE_S
        out[path] = (
            round(interrupt.ran_s() / slice_s, 6),
            {k: round(v / slice_s, 6) for k, v in sorted(seen.items())},
        )
    return out


_ONLY_THE_CALL: Final = {
    "reply": (4.0, {"acquired": 0.0, "booted": 0.0}),
    "lost": (4.0, {"acquired": 0.0, "booted": 0.0}),
    "raised": (4.0, {"acquired": 0.0, "booted": 0.0}),
    "boot raised": (0.0, {"acquired": 0.0, "booted": 0.0}),
}


def test_only_the_call_itself_is_a_checks_run_time(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    assert _run_time_in_slices(monkeypatch, clock) == _ONLY_THE_CALL


_S04: Final = (
    "    except BaseException:  # e.g. a per-test timeout's signal: the worker is mid-check\n"
    "        interrupt.pause()\n",
    "    except BaseException:  # e.g. a per-test timeout's signal: the worker is mid-check\n",
)


@pytest.mark.xfail(strict=True, reason="AUDIT-40 #1")
def test_only_the_call_itself_is_a_checks_run_time_under_s04(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    _mutant(monkeypatch, Z3Solver, "check_entailment", *_S04)
    assert _run_time_in_slices(monkeypatch, clock) == _ONLY_THE_CALL


_I03: Final = (
    "    if self._paused is None:\n        self._paused = _clock()",
    "    self._paused = _clock()",
)


@pytest.mark.xfail(strict=True, reason="AUDIT-40 #1")
def test_only_the_call_itself_is_a_checks_run_time_under_i03(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    _mutant(monkeypatch, solver_mod._Interrupt, "pause", *_I03)
    assert _run_time_in_slices(monkeypatch, clock) == _ONLY_THE_CALL


# --------------------------------------------------------------------------
# #2 (medium): "a slot whose check waits for its worker is not stuck" is
# untested (U02 and I06 survive). SG35 stops a slot from lending its place
# while its check starts or boots its worker, because that check takes the CPU
# once the worker is up. Dropping `not interrupt.paused() and` passes the whole
# suite. Here the slot's check attaches while paused (as `check_entailment`
# does before `ready`), and the executor polls it for three slices.
# --------------------------------------------------------------------------


class _Booting:
    """A slot's check that waits for its worker, then runs, until released."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.boot = threading.Event()
        self.gate = threading.Event()
        self.interrupt: solver_mod._Interrupt | None = None

    def __call__(self) -> str:
        interrupt: solver_mod._Interrupt = solver_mod._worker.interrupt
        self.interrupt = interrupt
        interrupt.pause()
        assert interrupt.attach(_Target())
        self.started.set()
        try:
            self.boot.wait(_WAIT_S)
            interrupt.resume()
            self.gate.wait(_WAIT_S)
        finally:
            interrupt.detach()
        return "ran"


async def _stuck_while_booting_then_running(executors: list[SolverExecutor]) -> tuple[int, int]:
    executor = SolverExecutor(1, 0)
    executors.append(executor)
    polls: dict[int, int] = {}
    to_slow = executor._to_slow

    def counted(interrupt: solver_mod._Interrupt) -> bool:
        polls[id(interrupt)] = polls.get(id(interrupt), 0) + 1
        return to_slow(interrupt)

    executor._to_slow = counted  # type: ignore[method-assign]
    check = _Booting()
    task = asyncio.create_task(executor.run(check))
    try:
        assert await asyncio.to_thread(check.started.wait, _WAIT_S)
        slots = executor._slots[asyncio.get_running_loop()]
        key = id(check.interrupt)
        await _until(lambda: polls.get(key, 0) >= 3)  # three slices while it boots
        booting = slots.stuck
        check.boot.set()
        await _until(lambda: slots.stuck == 1 or polls.get(key, 0) >= 6)
        running = slots.stuck
    finally:
        check.boot.set()
        check.gate.set()
    assert (await task)[0] == "ran"
    return booting, running


async def test_a_slot_whose_check_waits_for_its_worker_lends_nothing(
    executors: list[SolverExecutor],
) -> None:
    assert await _stuck_while_booting_then_running(executors) == (0, 1)


@pytest.mark.xfail(strict=True, reason="AUDIT-40 #2")
async def test_a_slot_whose_check_waits_for_its_worker_lends_nothing_under_u02(
    monkeypatch: pytest.MonkeyPatch, executors: list[SolverExecutor]
) -> None:
    _mutant(
        monkeypatch,
        SolverExecutor,
        "run",
        "stuck = not interrupt.paused() and slots.stick(lent)",
        "stuck = slots.stick(lent)",
    )
    assert await _stuck_while_booting_then_running(executors) == (0, 1)


# --------------------------------------------------------------------------
# #3 (low): "a stop that comes too late keeps the result" is untested (U01, I07
# survive). SG35's `stopped = interrupt.request()` keeps a lent newcomer's
# result when its check left its worker between `_yields` and the stop; the old
# `interrupt.request(); stopped = True` throws it away and runs the check again.
# The window is a thread race, so here `_yields` itself detaches the check as it
# decides, as the pool thread would at that instant.
# --------------------------------------------------------------------------


class _LeavesItsWorker:
    """A newcomer's check, counted; it returns once its gate opens."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.gate = threading.Event()
        self.calls = 0
        self.interrupt: solver_mod._Interrupt | None = None

    def __call__(self) -> str:
        self.calls += 1
        interrupt: solver_mod._Interrupt = solver_mod._worker.interrupt
        self.interrupt = interrupt
        if interrupt.attach(_Target()):
            self.started.set()
            self.gate.wait(_WAIT_S)
            interrupt.detach()
        return "kept"


async def _late_stop(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, executors: list[SolverExecutor]
) -> tuple[str, int]:
    real_yields = SolverExecutor._yields

    def yields(interrupt: solver_mod._Interrupt, slots: solver_mod._Slots) -> bool:
        if not real_yields(interrupt, slots):
            return False
        interrupt.detach()  # the check leaves its worker just as the stop comes
        return True

    monkeypatch.setattr(SolverExecutor, "_yields", staticmethod(yields))
    executor = SolverExecutor(1, 0)
    executors.append(executor)
    blocker = _Held()
    slot_task = asyncio.create_task(executor.run(blocker))
    assert await asyncio.to_thread(blocker.started.wait, _WAIT_S)
    slots = executor._slots[asyncio.get_running_loop()]
    await _until(lambda: slots.stuck == 1)
    check = _LeavesItsWorker()
    newcomer = asyncio.create_task(executor.run(check))  # a fresh task: lent a place
    try:
        assert await asyncio.to_thread(check.started.wait, _WAIT_S)
        clock.slices(solver_mod._HARD_SLICES)  # it has shown it is hard
        await _until(lambda: check.interrupt is not None and check.interrupt.requested())
    finally:
        check.gate.set()
        blocker.gate.set()
    out, _ = await newcomer
    await slot_task
    return out, check.calls


async def test_a_stop_after_the_check_left_its_worker_keeps_the_result(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, executors: list[SolverExecutor]
) -> None:
    assert await _late_stop(monkeypatch, clock, executors) == ("kept", 1)


@pytest.mark.xfail(strict=True, reason="AUDIT-40 #3")
async def test_a_stop_after_the_check_left_its_worker_keeps_the_result_under_u01(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, executors: list[SolverExecutor]
) -> None:
    _mutant(
        monkeypatch,
        SolverExecutor,
        "run",
        "stopped = interrupt.request()",
        "interrupt.request()\n                        stopped = True",
    )
    assert await _late_stop(monkeypatch, clock, executors) == ("kept", 1)


# --------------------------------------------------------------------------
# #4 (low): `acquire`'s immediate grant honours `lend=False` untested (Q03
# survives). SG35 queues a newcomer stopped for a short caller again "for a
# slot only (lent nothing)". `test_a_caller_queued_again_keeps_its_arrival_and_
# is_lent_nothing` only reaches the `_wake` path (it queues before the slot
# sticks). If a lent place is free when it asks again (the short caller took a
# slot that freed meanwhile, or left), `acquire` must not lend it one either.
# --------------------------------------------------------------------------


async def _lent_at_once_when_queued_again() -> tuple[bool, int]:
    slots = solver_mod._Slots(1)
    assert await slots.acquire(short=False) is False
    assert slots.stick(lent=False)  # a lent place is free, nobody waits
    again = asyncio.create_task(slots.acquire(False, True, slots.arrive(), lend=False))
    await asyncio.sleep(0)
    seen = again.done(), slots.lent
    slots.unstick()
    slots.release(lent=False)
    slots.release(await again)  # lent at once, or given the slot that just freed
    assert (slots.held, slots.lent, slots.stuck, len(slots._waiters)) == (0, 0, 0, 0)
    return seen


async def test_a_caller_queued_again_is_lent_nothing_even_when_a_place_is_free() -> None:
    assert await _lent_at_once_when_queued_again() == (False, 0)


@pytest.mark.xfail(strict=True, reason="AUDIT-40 #4")
async def test_a_caller_queued_again_is_lent_nothing_even_when_a_place_is_free_under_q03(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(
        monkeypatch,
        solver_mod._Slots,
        "acquire",
        "lent = self._grant(short, new and lend)",
        "lent = self._grant(short, new)",
    )
    assert await _lent_at_once_when_queued_again() == (False, 0)


# --------------------------------------------------------------------------
# #5 (medium): the AUDIT-14 #1 repro passes vacuously when the machine slows
# down after calibrating it (the AUDIT-24 one-call-path test's flake).
# `_timeout_case` sets the timeout to 3x the time until the entailment check
# starts, measured first. If the check itself then runs about 2x slower (load
# that comes in bursts), `_solve`'s `_SETUP_SHARE` guard sees too little time
# left and never starts the entailment check: the outcome is "timeout" within
# the timeout, the AUDIT-14 #1 test passes, and AUDIT-24's
# `test_on_the_one_call_path_the_audit_14_1_repro_catches_the_assumption_check`
# fails with "DID NOT RAISE" although ASSUME is applied. Measured under 8 busy
# loops: 3 of 7 core checks under ASSUME returned within the timeout (0.71-0.93x;
# the one whose details were printed: "too little time left for the entailment
# check"), and the test also hit pytest's 60 s timeout once. Here the skew is
# made exact: the calibration is taken as if the machine were 2.5x faster than
# when the check runs.
# --------------------------------------------------------------------------

_SKEW: Final = 2.5
"""How much slower the machine is when the check runs than when it was calibrated."""


def _calibrated(skew: float) -> Callable[[list[SmtFormula], SmtFormula], tuple[float, float]]:
    def phases_s(premises: list[SmtFormula], claim: SmtFormula) -> tuple[float, float]:
        """`_phases_s` without its two full entailment checks: the time a `true` claim
        takes (it stops after the consistency check), on a machine `skew` times faster."""
        started = time.perf_counter()
        Z3Solver(_WAIT_S)._check_here(premises, SmtFormula(claim.declarations, "true"))
        return (time.perf_counter() - started) / skew, 0.0

    return phases_s


def _audit_14_1_core_leg_under_assume(monkeypatch: pytest.MonkeyPatch, skew: float) -> None:
    _mutant(monkeypatch, solver_mod, "_solve", *_ASSUME)
    monkeypatch.setattr(solver_mod, "_LOAD_ABOVE", 10 * audit_14._POOL)
    monkeypatch.setattr(audit_14, "_phases_s", _calibrated(skew))
    # The worker leg cannot catch ASSUME: the kill bounds it (AUDIT-24 #1). Under load
    # it measured 1.09-1.62x the timeout, so it "catches" only by start-up noise.
    monkeypatch.setattr(audit_14, "timed_check", lambda p, c, timeout_s: ("timeout", timeout_s))
    with pytest.raises(AssertionError):
        audit_14.test_a_check_returns_within_its_timeout()


@pytest.mark.perf
def test_the_audit_14_1_repro_catches_assume_when_calibrated_on_this_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _audit_14_1_core_leg_under_assume(monkeypatch, 1.0)  # control


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-40 #5")
def test_the_audit_14_1_repro_catches_assume_when_the_machine_slows_after_calibrating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _audit_14_1_core_leg_under_assume(monkeypatch, _SKEW)


# --------------------------------------------------------------------------
# #6 (medium): `test_solver_executor::test_solver_ms_excludes_worker_start_up`
# depends on thread timing (it failed once in this audit's coverage run:
# "ValueError: too many values to unpack", two idle workers in a pool of
# `idle_max=1`). It patches `_workers` to return its own pool before building a
# `SolverExecutor`, whose prestart thread then starts a worker in that pool too,
# adding 1000 s to the fake clock whenever it runs. If that thread starts its
# worker only after the check has started its own, the clock and the idle list
# are off. The same race lets `prestart` and `discard` together leave one idle
# worker above `idle_max`. Here prestart finds no idle worker and adds its own
# only after the check's replacement is in, the order of the failing run.
# --------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-40 #6")
def test_solver_ms_excludes_worker_start_up_whenever_prestart_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The order of the failing run: prestart finds no idle worker, and adds its worker
    # only after the check's replacement is in.
    real_start, real_prestart = solver_workers.Workers._start, solver_workers.Workers.prestart
    prestarting: list[solver_workers.Workers] = []
    prestarted, replaced = threading.Event(), threading.Event()
    starts = [0]

    def start(self: solver_workers.Workers) -> solver_workers.Worker:
        if threading.current_thread() is not threading.main_thread():
            prestarting.append(self)
            assert replaced.wait(_WAIT_S)
            return real_start(self)
        worker = real_start(self)
        starts[0] += 1
        if starts[0] == 2:  # the check's own worker, then its replacement
            replaced.set()
        return worker

    def prestart(self: solver_workers.Workers, n: int) -> None:
        try:
            real_prestart(self, n)
        finally:
            prestarted.set()

    monkeypatch.setattr(solver_workers.Workers, "_start", start)
    monkeypatch.setattr(solver_workers.Workers, "prestart", prestart)
    try:
        test_solver_executor.test_solver_ms_excludes_worker_start_up(monkeypatch)
    finally:
        assert prestarted.wait(_WAIT_S)
        for pool in prestarting:
            pool.close()
