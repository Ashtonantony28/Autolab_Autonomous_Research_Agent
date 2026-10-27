"""AUDIT-44: test coverage and test quality, round 11 (SG36, pausing the lender).

Each finding in `AUDIT-44.md` about a mutant the suite lets through has two tests here:
- a plain test that kills the mutant and passes today (the missing guard);
- a strict xfail, named for the finding, that applies the mutant with `monkeypatch`
  (`_mutant` from AUDIT-24 recompiles one function with one exact-match edit) and runs
  the same check. Mutants of code that runs only in a worker process cannot be applied
  this way; each rule's xfail applies a parent-side mutant of the same rule.
Waits are bounded by events, by a fake clock, or by evidence read from /proc (the
worker has used CPU, so its check has started); the one wall-clock comparison is
between two spans of the same run, recalibrated when the machine is too slow for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import multiprocessing
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from multiprocessing.connection import Connection
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest
import z3

from autolab.verification import solver as solver_mod
from autolab.verification import solver_workers
from autolab.verification.solver import SmtFormula, SolverExecutor, SolverOutcome, Z3Solver
from scripts.audit_load_9 import UNDECIDABLE
from tests.audit import test_audit_34 as audit_34
from tests.audit import test_audit_40 as audit_40
from tests.audit.test_audit_24 import _mutant
from tests.audit.test_audit_36 import _static_mutant

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="SIGSTOP, /proc")

_WAIT_S: Final = 10.0
"""Bounds a wait on an event or a thread; a passing run never waits this long."""
_D: Final = "(declare-const p Bool)"
_ENTAILED: Final = ([SmtFormula(_D, "p")], SmtFormula(_D, "p"))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> audit_40._Clock:
    fake = audit_40._Clock()
    monkeypatch.setattr(solver_mod, "_clock", fake)
    return fake


@pytest.fixture
def executors() -> Iterator[list[SolverExecutor]]:
    made: list[SolverExecutor] = []
    yield made
    for executor in made:
        executor.shutdown()


def _until_sync(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + _WAIT_S
    while not condition():
        assert time.monotonic() < deadline, "condition never held"
        time.sleep(0.002)


def _stat(pid: int) -> list[str]:
    """/proc/<pid>/stat after the command name: [0] state, [11] utime, [12] stime."""
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()


def _cpu_ticks(pid: int) -> int:
    fields = _stat(pid)
    return int(fields[11]) + int(fields[12])


def _bare_worker(process: Any) -> solver_workers.Worker:
    """A `Worker` around `process`, with no pipe: for pause and resume alone."""
    worker = solver_workers.Worker.__new__(solver_workers.Worker)
    worker.process = process
    worker._shared = ctypes.c_double(0.0)
    worker._lock, worker._paused_at, worker._paused_s = threading.Lock(), None, 0.0
    return worker


def _piped_worker() -> tuple[solver_workers.Worker, Connection]:
    """A booted `Worker` whose other end of the pipe the test plays."""
    parent, child = multiprocessing.Pipe()
    worker = _bare_worker(SimpleNamespace(exitcode=None, pid=0))
    worker.conn, worker._ready, worker._seq, worker.fault = parent, True, 0, ""
    return worker, child


# --------------------------------------------------------------------------
# #1 (medium): the paused check's own budget is pinned only by luck of timing
# (W04, W17, P01). ADR 0009, SG36: "a watchdog thread in the worker calls
# `ctx.interrupt()` once a deadline passes on a clock that excludes paused time
# (the parent writes the paused total to shared memory before SIGCONT)". The one
# test of it, `test_a_check_paused_past_its_timeout_keeps_its_budget`, freezes the
# check as soon as it is attached, which is before the worker has read its request;
# a check paused before `_solve` sets its deadline loses nothing on either clock.
# It catches the mutants only when the worker happens to be quicker than the
# freeze: W04 (the parent never writes the total) survived the suite under load,
# and failed that test 4 of 6 times alone (W17 and P01 were caught, by the same
# luck). Here the pause lands only once the
# worker has spent CPU on the check (so its deadline is set), and the time the
# check runs after it is continued must be what was left of its budget.
# --------------------------------------------------------------------------


def _paused_mid_check(budget_s: float) -> tuple[Any, float, float]:
    """(reply, wall s from the request to the pause, wall s from SIGCONT to the reply)
    of a check that runs to its timeout, paused for twice its budget mid-check."""
    pool = solver_mod._workers()
    worker = pool.acquire()
    assert worker.ready()
    pid = worker.process.pid
    args = Z3Solver(budget_s)._args(*UNDECIDABLE)
    out: list[Any] = []
    base = _cpu_ticks(pid)
    sent = time.monotonic()
    # The parent's budget is wide: the worker's own deadline must end the check.
    thread = threading.Thread(target=lambda: out.append(worker.call(args, 20 * budget_s)))
    thread.start()
    _until_sync(lambda: _cpu_ticks(pid) >= base + 2 or bool(out))  # 2 ticks into `_solve`
    worker.pause()
    frozen = time.monotonic()
    time.sleep(2 * budget_s)
    thawed = time.monotonic()
    worker.resume()
    thread.join(_WAIT_S)
    ended = time.monotonic()
    if out and out[0] is not None:
        pool.release(worker)
    else:
        pool.discard(worker)
    return (out[0] if out else None), frozen - sent, ended - thawed


def _runs_on_after_its_pause() -> None:
    budget_s = 0.5
    for _ in range(4):  # until the pause lands in the first half of the budget
        reply, before, after = _paused_mid_check(budget_s)
        if before <= budget_s / 2:
            break
        budget_s *= 2
    assert reply is not None and reply[1].reason == "timeout", reply
    # The check ran at most `before` before its pause, so at least `budget - before`
    # is left; a deadline that kept running while it was stopped leaves nothing.
    assert after >= (budget_s - before) / 2, (before, after, budget_s)


def test_a_check_paused_mid_check_runs_what_was_left_of_its_budget() -> None:
    _runs_on_after_its_pause()


_W04: Final = (
    "self._shared.value = self._paused_s  # read by the worker once it runs again",
    "pass",
)


@pytest.mark.xfail(strict=True, reason="AUDIT-44 #1")
def test_a_check_paused_mid_check_runs_what_was_left_of_its_budget_under_w04(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(monkeypatch, solver_workers.Worker, "resume", *_W04)
    _runs_on_after_its_pause()


# --------------------------------------------------------------------------
# #2 (medium): a second pause replaces the first instead of adding to it (W06,
# I08). `_balance` freezes and thaws a lender every time a lent place comes and
# goes, so one check is often paused several times. Both totals are covered, but
# every test pauses once: with `=` for `+=` in `Worker.resume` the shared total
# the worker's watchdog reads goes *down* at the second pause (its deadline jumps
# forward by the first pause), and the parent's own wait for the reply shrinks
# by as much; with `=` in `_Interrupt.thaw`, `ran_s` counts the earlier frozen
# time as run time, so hardness and `solver_ms` include it. Here on fake clocks.
# --------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _worker_paused_twice(monkeypatch: pytest.MonkeyPatch) -> tuple[float, float, float]:
    """(paused total during the second pause, after it, as written for the worker)."""
    clock = _Clock()
    monkeypatch.setattr(solver_workers, "time", SimpleNamespace(monotonic=clock))
    worker = _bare_worker(SimpleNamespace(exitcode=None, pid=0))
    monkeypatch.setattr(worker, "_signal", lambda signum: True)  # no process to signal
    for paused_s in (2.0, 3.0):
        worker.pause()
        clock.now += paused_s / 2
        during = worker._paused()
        clock.now += paused_s / 2
        worker.resume()
        clock.now += 1.0  # running between the pauses
    return during, worker._paused(), worker._shared.value


def test_a_workers_pauses_add_up(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _worker_paused_twice(monkeypatch) == (3.5, 5.0, 5.0)


@pytest.mark.xfail(strict=True, reason="AUDIT-44 #2")
def test_a_workers_pauses_add_up_under_w06(monkeypatch: pytest.MonkeyPatch) -> None:
    _mutant(
        monkeypatch,
        solver_workers.Worker,
        "resume",
        "self._paused_s += time.monotonic() - self._paused_at",
        "self._paused_s = time.monotonic() - self._paused_at",
    )
    assert _worker_paused_twice(monkeypatch) == (3.5, 5.0, 5.0)


def _check_frozen_twice(monkeypatch: pytest.MonkeyPatch) -> float:
    """The run time of a 10 s call frozen for 2 s and then 3 s."""
    clock = _Clock()
    monkeypatch.setattr(solver_mod, "_clock", clock)
    interrupt = solver_mod._Interrupt()
    interrupt.started = clock.now
    for frozen_s in (2.0, 3.0):
        clock.now += 1.0
        interrupt.freeze()
        clock.now += frozen_s
        interrupt.thaw()
    clock.now += 8.0
    interrupt.ended = clock.now
    return interrupt.ran_s()


def test_a_checks_frozen_spans_add_up(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _check_frozen_twice(monkeypatch) == 10.0


@pytest.mark.xfail(strict=True, reason="AUDIT-44 #2")
def test_a_checks_frozen_spans_add_up_under_i08(monkeypatch: pytest.MonkeyPatch) -> None:
    _mutant(
        monkeypatch,
        solver_mod._Interrupt,
        "thaw",
        "self._frozen_s += _clock() - self._frozen",
        "self._frozen_s = _clock() - self._frozen",
    )
    assert _check_frozen_twice(monkeypatch) == 10.0


# --------------------------------------------------------------------------
# #3 (medium): a worker's earlier pauses must not move a later check's deadline
# (W09, P07, P08). A worker is reused across checks, and its paused total only
# grows: `_poll` subtracts it at the start of each wait, and `_solve` sets its
# deadline on `_now`, which subtracts it too. Every test starts from a fresh
# worker whose total is 0, so dropping either subtraction passes the suite: then
# a worker once paused for 1000 s waits that long for a reply that never comes
# (W09), runs its next check on a deadline 1000 s away (P08), or, with the
# loading poll back on `time.monotonic` (P07), cuts a loaded pool off at once.
# --------------------------------------------------------------------------

_PAUSED_BEFORE_S: Final = 1000.0


def _wait_for_a_reply_that_never_comes(budget_s: float) -> tuple[bool, Any]:
    """(whether the call returned within `_WAIT_S`, its result) for a worker paused
    for `_PAUSED_BEFORE_S` before this call, whose reply never comes."""
    worker, child = _piped_worker()
    worker._paused_s = _PAUSED_BEFORE_S
    out: list[Any] = []
    thread = threading.Thread(target=lambda: out.append(worker.call((), budget_s)))
    thread.start()
    thread.join(_WAIT_S)
    returned = not thread.is_alive()
    child.close()  # ends a wait that is still going: EOF
    thread.join(_WAIT_S)
    worker.conn.close()
    return returned, out[0] if out else "no result"


def test_earlier_pauses_do_not_extend_a_calls_wait() -> None:
    assert _wait_for_a_reply_that_never_comes(0.05) == (True, None)


@pytest.mark.xfail(strict=True, reason="AUDIT-44 #3")
def test_earlier_pauses_do_not_extend_a_calls_wait_under_w09(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(
        monkeypatch,
        solver_workers.Worker,
        "_poll",
        "end = time.monotonic() + budget_s - self._paused()",
        "end = time.monotonic() + budget_s",
    )
    assert _wait_for_a_reply_that_never_comes(0.05) == (True, None)


def _chain(n: int) -> tuple[list[SmtFormula], SmtFormula]:
    """`n - 1` premises r_i > r_{i+1} + 1.0 over Reals: a pool loaded one at a time."""
    decls = "\n".join(f"(declare-const r{i} Real)" for i in range(n))
    premises = [SmtFormula("", f"(> r{i} (+ r{i + 1} 1.0))") for i in range(n - 1)]
    return premises, SmtFormula(decls, "(> r0 0.0)")


def test_a_worker_paused_before_decides_its_next_checks_in_their_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # As in a worker the parent once paused for 1000 s: `paused_s()` reads 1000.
    monkeypatch.setattr(solver_workers, "_paused", SimpleNamespace(value=_PAUSED_BEFORE_S))
    loaded = _chain(solver_mod._LOAD_ABOVE + 20)
    assert solver_mod._loaded(solver_mod._build_smt2(*loaded), len(loaded[0]))
    assert Z3Solver(30.0)._check_here(*loaded).reason == "not_entailed"  # P07: timeout
    contexts: list[z3.Context] = []
    real_init = solver_mod._Watchdog.__init__

    def init(self: solver_mod._Watchdog, ctx: z3.Context, deadline: float) -> None:
        contexts.append(ctx)
        real_init(self, ctx, deadline)

    monkeypatch.setattr(solver_mod._Watchdog, "__init__", init)
    out: list[SolverOutcome] = []
    thread = threading.Thread(target=lambda: out.append(Z3Solver(0.3)._check_here(*UNDECIDABLE)))
    thread.start()
    thread.join(_WAIT_S)  # P08: its deadline is 1000 s away
    finished = not thread.is_alive()
    deadline = time.monotonic() + _WAIT_S
    while thread.is_alive() and time.monotonic() < deadline:
        for ctx in contexts:  # stop a check still running, so a failing run ends
            ctx.interrupt()
        thread.join(0.05)
    assert finished
    assert out[0].reason == "timeout"


# --------------------------------------------------------------------------
# #4 (low): the `_SETUP_SHARE` guard's two times are unpinned on the paused
# clock (P05; P06 is caught only by #1's timing-dependent test). `_decide`
# measures the consistency check (`took`) and the
# time left on `_now`; with either back on `time.monotonic`, a check paused
# during its consistency check counts the pause as Z3 work, or as time spent, and
# skips its entailment check ("too little time left"): UNGROUNDED for a claim it
# had all its budget left to decide. Here the worker's paused total grows while
# the consistency check is "stopped", as the worker sees it: no time passes on
# its clock (its threads, the watchdog's too, do not run while it is stopped).
# --------------------------------------------------------------------------


class _PausedTotal:
    """The worker's view of its paused total; a pause in progress counts in full."""

    def __init__(self) -> None:
        self.total = 0.0
        self._since: float | None = None

    @property
    def value(self) -> float:
        now = time.monotonic()
        return self.total + (0.0 if self._since is None else now - self._since)

    @contextlib.contextmanager
    def paused(self) -> Iterator[None]:
        self._since = time.monotonic()
        try:
            yield
        finally:
            self.total += time.monotonic() - self._since
            self._since = None


def _paused_in_its_consistency_check(monkeypatch: pytest.MonkeyPatch) -> SolverOutcome:
    budget_s = 0.2
    total = _PausedTotal()
    monkeypatch.setattr(solver_workers, "_paused", total)
    real_check = z3.Solver.check
    checks = [0]

    def check(self: z3.Solver, *args: z3.ExprRef) -> z3.CheckSatResult:
        checks[0] += 1
        if checks[0] == 1:
            with total.paused():
                time.sleep(3 * budget_s)  # stopped for longer than its whole budget
        return real_check(self, *args)

    monkeypatch.setattr(z3.Solver, "check", check)
    return Z3Solver(budget_s)._check_here(*_ENTAILED)


def test_a_check_paused_in_its_consistency_check_still_decides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _paused_in_its_consistency_check(monkeypatch)
    assert (out.verdict, out.reason) == ("VALID", "entailed"), out


@pytest.mark.xfail(strict=True, reason="AUDIT-44 #4")
def test_a_check_paused_in_its_consistency_check_still_decides_under_p05(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(
        monkeypatch,
        solver_mod,
        "_decide",
        "started = _now()\n    if consistency == z3.sat:\n        consistency = solver.check()\n"
        "    took = _now() - started",
        "started = time.monotonic()\n    if consistency == z3.sat:\n"
        "        consistency = solver.check()\n    took = time.monotonic() - started",
    )
    out = _paused_in_its_consistency_check(monkeypatch)
    assert (out.verdict, out.reason) == ("VALID", "entailed"), out


# --------------------------------------------------------------------------
# #5 (low): the parent's wait for a paused worker may spin (W10). `_poll` waits
# on the pipe for what is left of the budget plus the time paused so far. Without
# `+ self._paused()` in that timeout, once the wall-clock budget has passed the
# loop polls with timeout 0 until the pause ends: the pool thread of a frozen
# lender burns a CPU, the CPU SG36 freed. Nothing measures the parent's CPU, so
# the suite passes. Here the calling thread's own CPU time over a 0.5 s pause
# must be a small share of it.
# --------------------------------------------------------------------------


def _cpu_share_while_paused(pause_s: float = 0.5) -> float:
    """The calling thread's CPU s per wall s while its worker is paused past the budget."""
    worker, child = _piped_worker()
    worker._paused_at = time.monotonic()  # paused as the call starts (no process here)
    spent: list[float] = []

    def call() -> None:
        cpu = time.thread_time()
        worker.call((), 0.01)
        spent.append(time.thread_time() - cpu)

    thread = threading.Thread(target=call)
    started = time.monotonic()
    thread.start()
    time.sleep(pause_s)
    with worker._lock:  # `resume`, without a process to continue
        assert worker._paused_at is not None
        worker._paused_s += time.monotonic() - worker._paused_at
        worker._paused_at = None
    child.send((1, (True, None)))
    thread.join(_WAIT_S)
    wall_s = time.monotonic() - started
    child.close()
    worker.conn.close()
    return spent[0] / wall_s


def test_a_call_waits_for_a_paused_worker_without_spinning() -> None:
    assert _cpu_share_while_paused() < 0.2


@pytest.mark.xfail(strict=True, reason="AUDIT-44 #5")
def test_a_call_waits_for_a_paused_worker_without_spinning_under_w10(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(
        monkeypatch,
        solver_workers.Worker,
        "_poll",
        "self.conn.poll(max(0.0, end + self._paused() - time.monotonic()))",
        "self.conn.poll(max(0.0, end - time.monotonic()))",
    )
    assert _cpu_share_while_paused() < 0.2


# --------------------------------------------------------------------------
# #6 (low): "one reaped meanwhile is not signalled" is pinned for a pid that is
# gone, not for one that is reused (W07). `_signal` checks `exitcode` before
# `os.kill`; SG36's test hands it a reaped pid with `exitcode` None, which only
# the `ProcessLookupError` handler sees. Without the `exitcode` check (W07) the
# suite passes, and a worker that has ended (`exitcode` set) is signalled by pid:
# once the pid is reused, SIGSTOP goes to an unrelated process. Here the pid is
# a live process's.
# --------------------------------------------------------------------------


def _signalled_after_it_ended() -> tuple[bool, str]:
    """(whether `pause` recorded a pause, the state of the process now at that pid)."""
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        worker = _bare_worker(SimpleNamespace(exitcode=-9, pid=other.pid))  # ended
        worker.pause()
        paused = worker._paused_at is not None
        if paused:
            _until_sync(lambda: _stat(other.pid)[0] == "T")
        state = _stat(other.pid)[0]
        return paused, "T" if state == "T" else "running"
    finally:
        other.kill()
        other.wait()


def test_a_worker_that_has_ended_is_never_signalled() -> None:
    assert _signalled_after_it_ended() == (False, "running")


@pytest.mark.xfail(strict=True, reason="AUDIT-44 #6")
def test_a_worker_that_has_ended_is_never_signalled_under_w07(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(
        monkeypatch,
        solver_workers.Worker,
        "_signal",
        "if self.process.exitcode is None:",
        "if True:",
    )
    assert _signalled_after_it_ended() == (False, "running")


# --------------------------------------------------------------------------
# #7 (low): the AUDIT-40 #6 production half is fixed but unpinned (W16, W19).
# SG36 made `prestart` and `discard` hand a started worker to `release`, which
# re-checks `idle_max` under the lock, instead of appending it; AUDIT-40 #6
# showed the two can race to leave `idle_max + 1` idle workers. Its repro was
# fixed in the test instead (the executor is built first), so reverting either
# line (W16, W19) passes the suite. Here another thread returns a worker while
# one starts, on a fake `_start`.
# --------------------------------------------------------------------------


class _FakeProcess:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True

    def alive(self) -> bool:
        return not self.stopped


def _idle_after_a_return_during_a_start(
    monkeypatch: pytest.MonkeyPatch, how: str
) -> tuple[int, int]:
    """(idle workers, idle_max) once `how` (prestart or discard) has started a worker
    while another thread gave one back to the idle list."""
    pool = solver_workers.Workers(time.sleep, idle_max=1)
    starts: list[_FakeProcess] = []

    def start() -> _FakeProcess:
        worker = _FakeProcess()
        starts.append(worker)
        if len(starts) == 1:
            pool.release(_FakeProcess())  # type: ignore[arg-type]  # another caller's
        return worker

    monkeypatch.setattr(pool, "_start", start)
    if how == "prestart":
        pool.prestart(1)
    else:
        pool.discard(_FakeProcess())  # type: ignore[arg-type]
    return len(pool._idle), pool.idle_max


@pytest.mark.parametrize("how", ["prestart", "discard"])
def test_a_start_never_leaves_more_than_idle_max_idle(
    monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    assert _idle_after_a_return_during_a_start(monkeypatch, how) == (1, 1)


@pytest.mark.xfail(strict=True, reason="AUDIT-44 #7")
def test_a_start_never_leaves_more_than_idle_max_idle_under_w16(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(
        monkeypatch,
        solver_workers.Workers,
        "prestart",
        "self.release(self._start())  # another thread may have filled the idle list",
        "worker = self._start()\n        with self._lock:\n            self._idle.append(worker)",
    )
    assert _idle_after_a_return_during_a_start(monkeypatch, "prestart") == (1, 1)


# --------------------------------------------------------------------------
# #8 (medium): a lender that stops lending must leave the lender list (R10, R11).
# `run` passes its interrupt to `unstick` when its stuck check moves to a slow
# place (R10) and when it ends (R11), so `_Slots` drops it from `_lenders`; with
# a bare `unstick()` it stays there, first in line. The next lent place then
# freezes that stale entry (a check now on a slow place, or one that has ended)
# and not the slot check that lent it, which runs on beside the lent check: the
# AUDIT-34 #2 oversubscription again. Only `_Slots` unit tests pass a lender to
# `unstick`; no executor test has a second lender after the first has left. Here:
# 1 slot, 1 slow place; B sticks in the slot, then leaves it; C sticks; a
# newcomer is lent C's place, and C's check must be the one paused.
# --------------------------------------------------------------------------


class _Logged:
    """A check's target that records what is done to it."""

    def __init__(self, name: str, log: list[str]) -> None:
        self.name, self.log = name, log

    def interrupt(self) -> None:
        self.log.append(f"kill {self.name}")

    def pause(self) -> None:
        self.log.append(f"pause {self.name}")

    def resume(self) -> None:
        self.log.append(f"resume {self.name}")


class _Gated:
    """A check that attaches a `_Logged` target and returns once its gate opens."""

    def __init__(self, name: str, log: list[str]) -> None:
        self.target = _Logged(name, log)
        self.started = threading.Event()
        self.gate = threading.Event()

    def __call__(self) -> str:
        interrupt: solver_mod._Interrupt = solver_mod._worker.interrupt
        assert interrupt.attach(self.target)
        self.started.set()
        try:
            assert self.gate.wait(_WAIT_S)
        finally:
            interrupt.detach()
        return self.target.name


async def _until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + _WAIT_S
    while not condition():
        assert time.monotonic() < deadline, "condition never held"
        await asyncio.sleep(0.005)


async def _paused_for_a_lent_place(monkeypatch: pytest.MonkeyPatch, b_leaves: str) -> list[str]:
    """What is paused when a newcomer is lent C's place, after B stuck and then left
    its slot for a slow place or by ending (`b_leaves`)."""
    monkeypatch.setattr(solver_mod, "_clock", lambda: 0.0)  # no check ever runs long
    executor = SolverExecutor(1, slow_workers=1)
    log: list[str] = []
    checks = {name: _Gated(name, log) for name in "ABCN"}
    tasks: list[asyncio.Task[Any]] = []

    async def start(name: str) -> None:  # each in a task of its own
        tasks.append(asyncio.create_task(executor.run(checks[name])))
        assert await asyncio.to_thread(checks[name].started.wait, _WAIT_S)

    try:
        slots = executor._slot()
        await start("A")
        await _until(lambda: executor._slow == 1)  # A holds the slow place
        await start("B")
        await _until(lambda: slots.stuck == 1)  # B lends its slot
        if b_leaves == "slow":
            checks["A"].gate.set()
            await _until(lambda: executor._slow == 1 and slots.stuck == 0)  # B moved
        else:
            checks["B"].gate.set()
            await _until(lambda: slots.stuck == 0 and slots.held == 0)  # B ended
        await start("C")
        await _until(lambda: slots.stuck == 1)  # C lends its slot
        await start("N")  # a newcomer: lent C's place
        assert slots.lent == 1
        return [entry for entry in log if entry.startswith("pause")]
    finally:
        for check in checks.values():
            check.gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        executor.shutdown()


@pytest.mark.parametrize("b_leaves", ["slow", "ends"])
async def test_a_lent_place_pauses_the_slot_check_that_lent_it(
    monkeypatch: pytest.MonkeyPatch, b_leaves: str
) -> None:
    assert await _paused_for_a_lent_place(monkeypatch, b_leaves) == ["pause C"]


@pytest.mark.xfail(strict=True, reason="AUDIT-44 #8")
async def test_a_lent_place_pauses_the_slot_check_that_lent_it_under_r11(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(
        monkeypatch,
        SolverExecutor,
        "run",
        "            if stuck:\n                slots.unstick(interrupt)\n"
        "            slots.release(lent)\n        if extra",
        "            if stuck:\n                slots.unstick()\n"
        "            slots.release(lent)\n        if extra",
    )
    assert await _paused_for_a_lent_place(monkeypatch, "ends") == ["pause C"]


# --------------------------------------------------------------------------
# #9 (medium): a frozen check that ends before it gets a place must withdraw its
# request (R14). `_again` waits for a place or for the check to end; when the
# check ends first (it had left its worker as it was frozen), `place.cancel()`
# withdraws the queued `acquire`. Without it (R14) the request stays queued, is
# granted once a place frees, and is never released: the executor loses that
# slot (or lent place) for good. AUDIT-40 #3's test reaches this path, as
# retargeted by SG36, but checks only the result and how soon it came back, not
# the places afterwards.
# --------------------------------------------------------------------------


async def _places_after_a_late_freeze(
    monkeypatch: pytest.MonkeyPatch, clock: audit_40._Clock, executors: list[SolverExecutor]
) -> tuple[Any, ...]:
    """(result, held, lent, stuck, waiters) once AUDIT-40 #3's late freeze has run and
    every check has ended."""
    out = await audit_40._late_stop(monkeypatch, clock, executors)
    slots = executors[-1]._slot()
    for _ in range(10):  # a request still queued is granted on the next loop turns
        await asyncio.sleep(0)
    return out[0], slots.held, slots.lent, slots.stuck, len(slots._waiters)


async def test_a_late_freeze_leaves_every_place_free(
    monkeypatch: pytest.MonkeyPatch, clock: audit_40._Clock, executors: list[SolverExecutor]
) -> None:
    assert await _places_after_a_late_freeze(monkeypatch, clock, executors) == ("kept", 0, 0, 0, 0)


@pytest.mark.xfail(strict=True, reason="AUDIT-44 #9")
async def test_a_late_freeze_leaves_every_place_free_under_r14(
    monkeypatch: pytest.MonkeyPatch, clock: audit_40._Clock, executors: list[SolverExecutor]
) -> None:
    _static_mutant(
        monkeypatch,
        SolverExecutor,
        "_again",
        "    place.cancel()\n    return None",
        "    return None",
    )
    assert await _places_after_a_late_freeze(monkeypatch, clock, executors) == ("kept", 0, 0, 0, 0)


# --------------------------------------------------------------------------
# #10 (low): "it gives way again as soon as a short caller queues" is pinned only
# for a short caller that queues after the newcomer is waiting again (R02). If a
# short caller is already waiting when the newcomer that gave way is given a
# place back, `run` skips the slice wait and gives way at once; without that skip
# (R02) the newcomer waits on a fresh `short_queued()` future, which only a later
# short caller completes, so the waiting one waits a whole slice. Here the slice
# is made far longer than the test once the newcomer has given way, and a short
# caller queues just as the newcomer gets its place back.
# --------------------------------------------------------------------------


async def _a_short_caller_waiting_when_the_newcomer_returns(
    monkeypatch: pytest.MonkeyPatch, clock: audit_40._Clock, executors: list[SolverExecutor]
) -> list[str]:
    """The order in which the newcomer and two short callers ran, within `_WAIT_S / 2`."""
    executor = SolverExecutor(1, 0)
    executors.append(executor)
    order: list[str] = []
    go = {name: asyncio.Event() for name in ("s1", "s2")}

    async def short(name: str) -> None:
        await executor.run(lambda: None)  # this task's last check was short
        await go[name].wait()
        await executor.run(lambda: order.append(name))

    shorts = [asyncio.create_task(short(name)) for name in go]
    await _until(lambda: len(executor._short) == 2)
    log: list[str] = []
    blocker, newcomer = _Gated("blocker", log), _Gated("new", log)
    tasks = [asyncio.create_task(executor.run(blocker))]
    assert await asyncio.to_thread(blocker.started.wait, _WAIT_S)
    slots = executor._slot()
    await _until(lambda: slots.stuck == 1)
    tasks.append(asyncio.create_task(executor.run(newcomer)))  # a fresh task: lent a place
    assert await asyncio.to_thread(newcomer.started.wait, _WAIT_S)
    real_again = SolverExecutor._again

    async def again(*args: Any) -> bool | None:
        place = await real_again(*args)
        if place is not None and not go["s2"].is_set():  # given its place back after s1
            go["s2"].set()
            await _until(slots.short_waiting)  # s2 waits as the newcomer returns
            monkeypatch.setattr(solver_mod, "_SLICE_S", 1000 * _WAIT_S)  # no slice ends now
        return place

    monkeypatch.setattr(SolverExecutor, "_again", staticmethod(again))
    clock.now += 1.5 * solver_mod._SLICE_S  # it has run a slice, not `_HARD_SLICES`
    go["s1"].set()  # the newcomer gives way to s1, then gets its place back
    try:
        async with asyncio.timeout(_WAIT_S / 2):  # before the held checks' gates time out
            await shorts[1]
    except TimeoutError:
        order.append("s2 still waiting")
    finally:
        newcomer.gate.set()
        blocker.gate.set()
        await asyncio.gather(*shorts, *tasks, return_exceptions=True)
    return order


async def test_a_newcomer_that_gave_way_gives_way_to_a_short_caller_already_waiting(
    monkeypatch: pytest.MonkeyPatch, clock: audit_40._Clock, executors: list[SolverExecutor]
) -> None:
    order = await _a_short_caller_waiting_when_the_newcomer_returns(monkeypatch, clock, executors)
    assert order == ["s1", "s2"]


@pytest.mark.xfail(strict=True, reason="AUDIT-44 #10")
async def test_a_newcomer_that_gave_way_gives_way_to_a_short_caller_already_waiting_under_r02(
    monkeypatch: pytest.MonkeyPatch, clock: audit_40._Clock, executors: list[SolverExecutor]
) -> None:
    _mutant(
        monkeypatch,
        SolverExecutor,
        "run",
        "if not (ceded and interrupt.attached() and slots.short_waiting()):",
        "if True:",
    )
    order = await _a_short_caller_waiting_when_the_newcomer_returns(monkeypatch, clock, executors)
    assert order == ["s1", "s2"]


# --------------------------------------------------------------------------
# #11 (medium): a check detached while its call is frozen must be continued
# (I07). `_Interrupt.detach` resumes a frozen call's target before letting it go,
# because `check_entailment` then hands the worker back to the pool: a freeze that
# lands after the worker replied but before the detach would otherwise leave a
# stopped process idle, and the next check given it waits for ever (its wait
# excludes the time paused). The one test of `detach` on a frozen call thaws it
# first, so the resume in `detach` is never needed there.
# --------------------------------------------------------------------------


def _detached_while_frozen() -> list[str]:
    log: list[str] = []
    interrupt = solver_mod._Interrupt()
    assert interrupt.attach(_Logged("a", log))
    interrupt.freeze()  # e.g. the reply is in, the pool thread not yet past `detach`
    interrupt.detach()
    interrupt.thaw()
    return log


def test_a_check_detached_while_frozen_is_continued() -> None:
    assert _detached_while_frozen() == ["pause a", "resume a"]


@pytest.mark.xfail(strict=True, reason="AUDIT-44 #11")
def test_a_check_detached_while_frozen_is_continued_under_i07(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(
        monkeypatch,
        solver_mod._Interrupt,
        "detach",
        "if self._frozen is not None and isinstance(self._target, _Pausable):\n"
        "            self._target.resume()\n",
        "",
    )
    assert _detached_while_frozen() == ["pause a", "resume a"]


# --------------------------------------------------------------------------
# #12 (low): two newcomers that gave way share one `short_queued` signal (L09).
# `short_queued` returns the pending future, so every lent check waiting on it
# wakes when a short caller queues; making a fresh one per call (L09) wakes only
# the last to ask, and the others give way a slice late. The suite has one such
# newcomer at a time.
# --------------------------------------------------------------------------


async def _both_signalled() -> tuple[bool, bool]:
    slots = solver_mod._Slots(1)
    assert await slots.acquire(short=False) is False
    first, second = slots.short_queued(), slots.short_queued()  # two waiting newcomers
    short = asyncio.create_task(slots.acquire(short=True))
    await asyncio.sleep(0)
    out = first.done(), second.done()
    slots.release(False)
    slots.release(await short)
    return out


async def test_every_newcomer_waiting_on_a_short_caller_is_signalled() -> None:
    assert await _both_signalled() == (True, True)


@pytest.mark.xfail(strict=True, reason="AUDIT-44 #12")
async def test_every_newcomer_waiting_on_a_short_caller_is_signalled_under_l09(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(
        monkeypatch,
        solver_mod._Slots,
        "short_queued",
        "if self._short_queued is None or self._short_queued.done():",
        "if True:",
    )
    assert await _both_signalled() == (True, True)


# --------------------------------------------------------------------------
# #13 (high): "gave way" matters only on a lent place (R01). `run` lets a check
# skip its slice wait only while it is `ceded`: lent a place and having given way
# before. With `ceded = gave_way` (R01), a newcomer that gave way and then got a
# slot skips the wait whenever a short caller waits, and nothing in that pass
# awaits: the event loop spins in `run`. Unless that slot's own stick lends the
# short caller a place at once, nothing can serve it while the loop spins, not
# even a check that ends (its future is completed by the loop): with the lent
# places taken (1 slot, no slow place, the 1-CPU defaults, and one short check
# running) every caller on that loop hangs. No test has a newcomer that gave way
# get a slot back while a short caller waits. Here `short_waiting` counts its
# calls and stops a spin by raising once they pass any number a slice-paced loop
# could reach.
# --------------------------------------------------------------------------

_SPIN_CALLS: Final = 10_000


class _Spinning(Exception):
    """Raised from `short_waiting` once a loop has called it `_SPIN_CALLS` times."""


async def _a_newcomer_back_on_a_slot_while_a_short_caller_waits(
    monkeypatch: pytest.MonkeyPatch, clock: audit_40._Clock, executors: list[SolverExecutor]
) -> list[Any]:
    """The results of the newcomer and the two short callers, in that order."""
    calls = [0]
    real_short_waiting = solver_mod._Slots.short_waiting

    def short_waiting(self: solver_mod._Slots) -> bool:
        calls[0] += 1
        if calls[0] > _SPIN_CALLS:
            raise _Spinning
        return real_short_waiting(self)

    monkeypatch.setattr(solver_mod._Slots, "short_waiting", short_waiting)
    executor = SolverExecutor(1, 0)
    executors.append(executor)
    log: list[str] = []
    s1 = _Gated("s1", log)  # holds the place it is lent
    go = {name: asyncio.Event() for name in ("s1", "s2")}
    fns: dict[str, Callable[[], Any]] = {"s1": s1, "s2": lambda: "s2"}

    async def short(name: str) -> Any:
        await executor.run(lambda: None)  # this task's last check was short
        await go[name].wait()
        return (await executor.run(fns[name]))[0]

    shorts = [asyncio.create_task(short(name)) for name in go]
    await _until(lambda: len(executor._short) == 2)
    blocker, newcomer = _Gated("blocker", log), _Gated("new", log)
    held = asyncio.create_task(executor.run(blocker))
    assert await asyncio.to_thread(blocker.started.wait, _WAIT_S)
    slots = executor._slot()
    await _until(lambda: slots.stuck == 1)
    new_task = asyncio.create_task(executor.run(newcomer))  # a fresh task: lent a place
    assert await asyncio.to_thread(newcomer.started.wait, _WAIT_S)
    clock.slices(1.5)  # it has run a slice, not `_HARD_SLICES`
    go["s1"].set()
    assert await asyncio.to_thread(s1.started.wait, _WAIT_S)  # it gave way; s1 holds the place
    blocker.gate.set()
    await held
    await _until(lambda: slots.held == 1 and not slots._waiters)  # the newcomer has the slot
    go["s2"].set()
    try:
        await _until(lambda: bool(slots._waiters) or shorts[1].done())  # s2 waits
        for _ in range(4):
            await asyncio.sleep(solver_mod._SLICE_S)  # the newcomer's loop runs a few slices
    finally:
        s1.gate.set()
        newcomer.gate.set()
        results = await asyncio.gather(new_task, *shorts, return_exceptions=True)
    return [r[0] if isinstance(r, tuple) else r for r in results]


async def test_a_newcomer_back_on_a_slot_waits_its_slices(
    monkeypatch: pytest.MonkeyPatch, clock: audit_40._Clock, executors: list[SolverExecutor]
) -> None:
    out = await _a_newcomer_back_on_a_slot_while_a_short_caller_waits(monkeypatch, clock, executors)
    assert out == ["new", "s1", "s2"], out


@pytest.mark.xfail(strict=True, reason="AUDIT-44 #13")
async def test_a_newcomer_back_on_a_slot_waits_its_slices_under_r01(
    monkeypatch: pytest.MonkeyPatch, clock: audit_40._Clock, executors: list[SolverExecutor]
) -> None:
    _mutant(monkeypatch, SolverExecutor, "run", "ceded = lent and gave_way", "ceded = gave_way")
    out = await _a_newcomer_back_on_a_slot_while_a_short_caller_waits(monkeypatch, clock, executors)
    assert out == ["new", "s1", "s2"], out


# --------------------------------------------------------------------------
# #14 (low): the AUDIT-34 #1 repro times the newcomer's return by its thaw, not
# by its grant. `test_a_newcomer_that_gives_way_to_a_short_caller_is_served_
# before_hard_runs` records "new" when the paused check is continued, a loop
# turn or two after `_again` gets its place. If the blocker's slot frees in that
# window, h1 is granted it and its check runs on a pool thread before the
# newcomer's task resumes, so the order reads new, short, h1 though the newcomer
# was served first. It failed so once in this audit's mutant runs (W02, which
# changes nothing on Linux): `['new', 'short', 'h1', 'new', 'h2']`. Here the
# window is held open: the newcomer is granted its place first, then waits
# until the slot has been handed on before it continues.
# --------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-44 #14")
async def test_the_audit_34_1_repro_passes_when_the_newcomer_resumes_late(
    monkeypatch: pytest.MonkeyPatch, executors: list[SolverExecutor]
) -> None:
    fake = audit_34._FakeClock()
    monkeypatch.setattr(solver_mod, "_clock", fake)
    real_again = SolverExecutor._again
    granted: list[bool | None] = []

    async def again(
        slots: solver_mod._Slots, new: bool, arrival: int, future: asyncio.Future[Any]
    ) -> bool | None:
        place = await real_again(slots, new, arrival, future)
        granted.append(place)
        # The blocker ends and its slot goes to h1, then h2: both run before this resumes.
        await _until(lambda: slots.stuck == 0 and not slots._waiters)
        return place

    monkeypatch.setattr(SolverExecutor, "_again", staticmethod(again))
    try:
        await audit_34.test_a_newcomer_that_gives_way_to_a_short_caller_is_served_before_hard_runs(
            executors, fake
        )
    finally:
        assert granted == [True]  # lent its place back before the slot freed


# --------------------------------------------------------------------------
# #15 (low): a paused newcomer cancelled just as it is given its place back
# must return that place (R13). `_again` catches the cancellation: if the place
# was granted in the same loop turn (`place.done()`), it releases it; otherwise
# it cancels the request. SG36's cancellation test cancels while the request is
# queued, so only the second branch runs; with the first gone (R13) a place
# granted at the moment of cancelling is never given back. Here the newcomer is
# cancelled from inside the release that grants it its place.
# --------------------------------------------------------------------------


async def _places_after_a_cancel_at_the_grant(
    monkeypatch: pytest.MonkeyPatch, clock: audit_40._Clock, executors: list[SolverExecutor]
) -> tuple[int, int, int, int]:
    """(held, lent, stuck, waiters) once every check has ended."""
    executor = SolverExecutor(1, 0)
    executors.append(executor)
    log: list[str] = []
    blocker, newcomer, short = _Gated("blocker", log), _Gated("new", log), _Gated("short", log)
    go = asyncio.Event()

    async def short_caller() -> Any:
        await executor.run(lambda: None)  # this task's last check was short
        await go.wait()
        return await executor.run(short)

    short_task = asyncio.create_task(short_caller())
    await _until(lambda: len(executor._short) == 1)
    held = asyncio.create_task(executor.run(blocker))
    assert await asyncio.to_thread(blocker.started.wait, _WAIT_S)
    slots = executor._slot()
    await _until(lambda: slots.stuck == 1)
    new_task = asyncio.create_task(executor.run(newcomer))  # a fresh task: lent a place
    assert await asyncio.to_thread(newcomer.started.wait, _WAIT_S)
    real_release = solver_mod._Slots.release

    def release(self: solver_mod._Slots, lent: bool) -> None:
        real_release(self, lent)
        if lent and not new_task.done() and self.lent == 1:  # the newcomer got it back
            new_task.cancel()

    clock.slices(1.5)  # it has run a slice, not `_HARD_SLICES`
    go.set()  # the newcomer gives way to the short caller
    assert await asyncio.to_thread(short.started.wait, _WAIT_S)
    monkeypatch.setattr(solver_mod._Slots, "release", release)
    short.gate.set()  # the short check ends and its lent place goes back to the newcomer
    await asyncio.gather(short_task, new_task, return_exceptions=True)
    assert new_task.cancelled()
    newcomer.gate.set()
    blocker.gate.set()
    await held
    return slots.held, slots.lent, slots.stuck, len(slots._waiters)


async def test_a_newcomer_cancelled_as_it_gets_its_place_back_returns_it(
    monkeypatch: pytest.MonkeyPatch, clock: audit_40._Clock, executors: list[SolverExecutor]
) -> None:
    assert await _places_after_a_cancel_at_the_grant(monkeypatch, clock, executors) == (0, 0, 0, 0)


@pytest.mark.xfail(strict=True, reason="AUDIT-44 #15")
async def test_a_newcomer_cancelled_as_it_gets_its_place_back_returns_it_under_r13(
    monkeypatch: pytest.MonkeyPatch, clock: audit_40._Clock, executors: list[SolverExecutor]
) -> None:
    _static_mutant(
        monkeypatch,
        SolverExecutor,
        "_again",
        "if place.done():\n            slots.release(place.result())",
        "if False:\n            slots.release(place.result())",
    )
    assert await _places_after_a_cancel_at_the_grant(monkeypatch, clock, executors) == (0, 0, 0, 0)
