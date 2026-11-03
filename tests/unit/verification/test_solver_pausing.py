"""SG36: a lending slot's worker is paused (SIGSTOP) while its place is lent (ADR 0009).

The worker's pause and its budget, which excludes the pause on both sides of the pipe;
which lenders are paused; a check that gives way paused, not stopped; and safety when
a paused worker dies, is killed, outlives its process's exit or is forked over. Every
wait is bounded by an event, a poll or a lower bound on wall time.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest

from autolab.verification import solver as solver_mod
from autolab.verification import solver_workers
from autolab.verification.solver import SolverExecutor, Z3Solver
from scripts.audit_load_9 import UNDECIDABLE

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="SIGSTOP, /proc")

_WAIT_S: Final = 10.0
_PAUSE_S: Final = 0.6
_ROOT: Final = Path(__file__).resolve().parents[3]


def _state(pid: int) -> str:
    """The process's state letter in /proc: R running, S sleeping, T stopped."""
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]


def _until_sync(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + _WAIT_S
    while not condition():
        assert time.monotonic() < deadline, "condition never held"
        time.sleep(0.005)


@pytest.fixture
def sleeper() -> Iterator[solver_workers.Worker]:
    """A booted worker whose call sleeps for its argument."""
    pool = solver_workers.Workers(time.sleep, idle_max=1)
    worker = pool.acquire()
    assert worker.ready()
    yield worker
    worker.stop()


def _call_in_thread(
    worker: solver_workers.Worker, args: tuple[Any, ...], budget_s: float
) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []
    thread = threading.Thread(target=lambda: out.append(worker.call(args, budget_s)))
    thread.start()
    return thread, out


# --- the worker ----------------------------------------------------------------------------


def test_a_paused_worker_is_stopped_and_its_call_outlives_its_budget(
    sleeper: solver_workers.Worker,
) -> None:
    # Paused for longer than the call's whole budget: the parent's deadline moves by the
    # pause, so the reply is taken, not the worker counted lost.
    thread, out = _call_in_thread(sleeper, (0.2,), 0.4)
    time.sleep(0.05)
    sleeper.pause()
    time.sleep(_PAUSE_S)
    stopped = _state(sleeper.process.pid)
    sleeper.resume()
    thread.join(_WAIT_S)
    assert (stopped, out) == ("T", [(True, None)])
    assert sleeper._paused() >= _PAUSE_S
    _until_sync(lambda: _state(sleeper.process.pid) != "T")  # signals arrive async


def test_a_paused_worker_that_dies_is_reported_dead(sleeper: solver_workers.Worker) -> None:
    thread, out = _call_in_thread(sleeper, (5.0,), _WAIT_S)
    sleeper.pause()
    os.kill(sleeper.process.pid, 9)  # e.g. the OOM killer
    thread.join(_WAIT_S)
    sleeper.resume()  # no SIGCONT to a process that is gone
    assert (out, sleeper.fault) == ([None], solver_workers.DIED), thread.is_alive()


def test_a_worker_reaped_behind_its_exit_code_is_not_signalled() -> None:
    # Another thread's `Process.start` reaps dead children: between reading `exitcode`
    # and `os.kill`, the pid can be gone (ProcessLookupError, seen under load).
    gone = subprocess.Popen(["true"])
    gone.wait()
    worker = solver_workers.Worker.__new__(solver_workers.Worker)
    worker.process = SimpleNamespace(exitcode=None, pid=gone.pid)
    worker._shared = ctypes.c_double(0.0)
    worker._lock, worker._paused_at, worker._paused_s = threading.Lock(), None, 0.0
    worker.pause()
    assert worker._paused_at is None
    worker._paused_at = time.monotonic()
    worker.resume()
    assert worker._paused_at is None


def test_a_paused_worker_is_continued_before_it_is_killed(
    sleeper: solver_workers.Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[int] = []
    real_kill = os.kill

    def kill(pid: int, sig: int) -> None:
        sent.append(sig)
        real_kill(pid, sig)

    thread, out = _call_in_thread(sleeper, (5.0,), _WAIT_S)
    sleeper.pause()
    monkeypatch.setattr(os, "kill", kill)
    sleeper.interrupt()
    thread.join(_WAIT_S)
    assert out == [None]
    assert sent[:2] == [signal.SIGCONT, signal.SIGKILL]


def test_a_check_paused_past_its_timeout_keeps_its_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Z3's own `timeout` is wall-clock and would run out while the worker is stopped;
    # `_Watchdog` runs on the worker's clock, which stops with it. So the check runs its
    # whole budget after the pause and ends by its own timeout, not the parent's kill
    # (whose grace is widened here, so load cannot bring it first).
    monkeypatch.setattr(solver_mod, "_KILL_GRACE_SHARE", 10.0)
    interrupt = solver_mod._Interrupt()
    out: list[solver_mod.SolverOutcome] = []

    def check() -> None:
        solver_mod._worker.interrupt = interrupt
        try:
            out.append(Z3Solver(_PAUSE_S).check_entailment(*UNDECIDABLE))
        finally:
            solver_mod._worker.interrupt = None

    solver_mod._workers().prestart(1)
    started = time.perf_counter()
    thread = threading.Thread(target=check)
    thread.start()
    _until_sync(lambda: interrupt.attached() and not interrupt.paused())
    interrupt.freeze()
    time.sleep(2 * _PAUSE_S)
    interrupt.thaw()
    thread.join(_WAIT_S)
    wall_s = time.perf_counter() - started
    # Its own deadline, in whichever check it passes under load: not killed, not stopped.
    assert out[0].reason == "timeout", out
    assert out[0] not in (solver_mod._KILLED, solver_mod._INTERRUPTED), out
    assert wall_s >= 3 * _PAUSE_S


def test_the_watchdog_runs_on_the_workers_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pause recorded by the parent moves the deadline: no interrupt before it.
    paused = SimpleNamespace(value=0.0)
    monkeypatch.setattr(solver_workers, "_paused", paused)
    interrupts: list[float] = []
    ctx = SimpleNamespace(interrupt=lambda: interrupts.append(time.monotonic()))
    started = time.monotonic()
    with solver_mod._Watchdog(ctx, solver_mod._now() + 0.1):
        paused.value = 0.3  # as if the worker had been paused 0.3 s
        _until_sync(lambda: bool(interrupts))
    assert interrupts[0] - started >= 0.4


# --- which lenders are paused -------------------------------------------------------------


class _Pausable:
    def __init__(self, log: list[str], name: str) -> None:
        self.log, self.name = log, name

    def interrupt(self) -> None:
        self.log.append(f"kill {self.name}")

    def pause(self) -> None:
        self.log.append(f"pause {self.name}")

    def resume(self) -> None:
        self.log.append(f"resume {self.name}")


def _lender(log: list[str], name: str) -> solver_mod._Interrupt:
    interrupt = solver_mod._Interrupt()
    assert interrupt.attach(_Pausable(log, name))
    return interrupt


async def test_one_lender_is_paused_per_lent_place_longest_stuck_first() -> None:
    slots = solver_mod._Slots(2)
    log: list[str] = []
    a, b = _lender(log, "a"), _lender(log, "b")
    for lender in (a, b):
        assert await slots.acquire(short=False) is False
        assert slots.stick(False, lender)
    assert log == []
    first = await slots.acquire(short=True)
    second = await slots.acquire(short=False, new=True)
    assert (first, second, log) == (True, True, ["pause a", "pause b"])
    slots.release(lent=True)
    assert log[-1:] == ["resume b"]
    slots.unstick(a)  # `a` ends with a place still lent: `b` lends it now
    assert log[-2:] == ["resume a", "pause b"]
    slots.release(lent=True)
    assert log[-1:] == ["resume b"]
    slots.unstick(b)
    slots.release(lent=False)
    slots.release(lent=False)
    assert (slots.held, slots.lent, slots.stuck, a.frozen(), b.frozen()) == (0, 0, 0, False, False)


def test_a_frozen_check_neither_moves_to_a_slow_place_nor_counts_its_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1.0]
    monkeypatch.setattr(solver_mod, "_clock", lambda: now[0])
    executor = SolverExecutor(1, 1)
    try:
        interrupt = _lender([], "a")
        interrupt.started = 0.0
        interrupt.freeze()  # at 1
        assert not executor._to_slow(interrupt)
        now[0] = 3.0
        assert interrupt.ran_s() == 1.0
        interrupt.thaw()  # at 3
        interrupt.ended = 4.0
        assert interrupt.ran_s() == 2.0
        assert executor._to_slow(interrupt)
        executor._end_slow(SimpleNamespace())  # type: ignore[arg-type]
    finally:
        executor.shutdown()


def test_a_newcomer_that_gave_way_gives_way_again_without_a_slice() -> None:
    interrupt = solver_mod._Interrupt()
    assert interrupt.attach(_Pausable([], "n"))
    interrupt.started = 0.0
    slots = SimpleNamespace(short_waiting=lambda: True)
    for ran, gave_way, expected in ((0.5, False, False), (1, False, True), (0.5, True, True)):
        interrupt.ended = ran * solver_mod._SLICE_S
        assert SolverExecutor._yields(interrupt, slots, gave_way) is expected  # type: ignore[arg-type]


async def test_a_short_caller_that_queues_is_signalled() -> None:
    slots = solver_mod._Slots(1)
    assert await slots.acquire(short=False) is False
    signal_ = slots.short_queued()
    newcomer = asyncio.create_task(slots.acquire(short=False, new=True))
    await asyncio.sleep(0)
    assert not signal_.done()
    short = asyncio.create_task(slots.acquire(short=True))
    await asyncio.sleep(0)
    assert signal_.done() and slots.short_queued() is not signal_
    slots.release(False)  # in arrival order
    slots.release(await newcomer)
    slots.release(await short)
    assert (slots.held, slots.lent) == (0, 0)


def test_a_check_attached_while_its_call_is_frozen_starts_paused() -> None:
    # A frozen call between two checks: the next one must not run beside the lent place.
    log: list[str] = []
    interrupt = solver_mod._Interrupt()
    interrupt.freeze()
    assert interrupt.attach(_Pausable(log, "second"))
    assert log == ["pause second"]
    interrupt.thaw()
    interrupt.detach()
    assert log == ["pause second", "resume second"]


def test_a_paused_worker_boots_past_the_boot_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    # A worker paused while it boots (attached to a frozen call) has not failed to start.
    monkeypatch.setattr(solver_workers, "_BOOT_S", 0.05)
    parent, child = multiprocessing.Pipe()
    worker = solver_workers.Worker.__new__(solver_workers.Worker)
    worker.conn, worker._ready, worker._seq, worker.fault = parent, False, 0, ""
    worker._lock, worker._paused_at, worker._paused_s = threading.Lock(), time.monotonic(), 0.0
    timer = threading.Timer(0.3, child.send, ("ready",))
    timer.start()
    try:
        assert worker.ready()
    finally:
        timer.join()
        child.close()
        parent.close()


# --- a check that gives way --------------------------------------------------------------


async def test_a_cancelled_paused_newcomer_is_killed_and_leaves_nothing_behind() -> None:
    # A newcomer paused on its way back to the queue, then cancelled: its worker is
    # killed (`Worker.interrupt` continues it first) and its places and thread are back.
    executor = SolverExecutor(1, 0)
    try:
        hold = asyncio.create_task(
            executor.run(partial(Z3Solver(_WAIT_S).check_entailment, *UNDECIDABLE))
        )
        slots = executor._slot()
        while slots.stuck == 0:
            await asyncio.sleep(0.005)
        newcomer = asyncio.create_task(
            executor.run(partial(Z3Solver(_WAIT_S).check_entailment, *UNDECIDABLE))
        )
        while not slots._waiters:  # it has shown it is hard and waits for the slot
            await asyncio.sleep(0.005)
        (paused,) = [i for i in executor._running if i.frozen()]
        worker = paused._target
        assert isinstance(worker, solver_workers.Worker)
        _until_sync(lambda: _state(worker.process.pid) == "T")
        newcomer.cancel()
        hold.cancel()
        await asyncio.gather(newcomer, hold, return_exceptions=True)
        _until_sync(lambda: not worker.alive())
        assert (slots.held, slots.lent, slots.stuck, len(slots._waiters)) == (0, 0, 0, 0)
        _until_sync(lambda: executor._pool._max_workers == 1)
    finally:
        executor.shutdown()


# --- exit and fork -------------------------------------------------------------------------

_PAUSED_AT_EXIT: Final = """
import sys, threading, time
from autolab.verification import solver_workers

pool = solver_workers.register(solver_workers.Workers(time.sleep, idle_max=1))
worker = pool.acquire()  # in use: `close` leaves it to its call
assert worker.ready()
threading.Thread(target=worker.call, args=((60.0,), 60.0), daemon=True).start()
worker.pause()
sys.exit(0)  # multiprocessing's exit hook SIGTERMs and joins it: a stopped one never ends
"""

_PAUSED_ACROSS_FORK: Final = """
import json, os, sys, threading, time
from autolab.verification import solver_workers

pool = solver_workers.register(solver_workers.Workers(time.sleep, idle_max=1))
worker = pool.acquire()
assert worker.ready()
out = []
thread = threading.Thread(target=lambda: out.append(worker.call((0.2,), 0.3)))
thread.start()
time.sleep(0.05)
worker.pause()
pid = os.fork()
if pid == 0:
    sys.exit(0)  # the child's exit hooks must neither continue nor end the owner's worker
os.waitpid(pid, 0)
with open(f"/proc/{worker.process.pid}/stat") as f:
    state = f.read().rsplit(")", 1)[1].split()[0]
worker.resume()
thread.join()
print("RESULT " + json.dumps({"state": state, "out": out, "alive": worker.alive()}))
"""


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_process_exits_while_its_worker_is_paused() -> None:
    proc = _run(_PAUSED_AT_EXIT)
    assert proc.returncode == 0, proc.stderr[-800:]


def test_a_child_forked_while_a_worker_is_paused_leaves_it_to_its_owner() -> None:
    proc = _run(_PAUSED_ACROSS_FORK)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), "")
    assert line, proc.stderr[-800:]
    got = json.loads(line[len("RESULT ") :])
    assert got == {"state": "T", "out": [[True, None]], "alive": True}
