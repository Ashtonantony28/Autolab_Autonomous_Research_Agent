"""AUDIT-28: test coverage and test quality, round 7 (SG28-SG30).

Each finding in `AUDIT-28.md` that is about a mutant the suite lets through has
two tests here:
- a plain test that kills the mutant and passes today (the missing guard);
- a strict xfail, named for the finding, that applies the mutant with
  `monkeypatch` (`_mutant` from AUDIT-24 recompiles one function with one
  exact-match edit, or a constant is patched) and runs the same check.
Nothing here edits a source file. Waits are bounded by events; the wall-clock
tests compare against a measurement taken on the same machine in the same test
(a linear twin of the same size, or the same run with no LLM latency), never
against absolute seconds.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import operator
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, Final

import pytest

from autolab.verification import formalizer, solver_workers
from autolab.verification import solver as solver_mod
from autolab.verification.llm import LLMPurpose, SchemaT
from autolab.verification.solver import SmtFormula, SolverExecutor, SolverOutcome, Z3Solver
from autolab.verification.vericot import Verifier
from scripts.audit_load import LatencyLLM, chain
from tests.audit.test_audit_24 import _mutant, _until
from tests.fixtures.golden_traces import build

_WAIT_S: Final = 10.0
"""Bounds a wait on an event; a passing run never waits this long."""

_DECLS: Final = "(declare-const p Bool)\n(declare-const q Bool)"
_ENTAILED: Final = ([SmtFormula(_DECLS, "p")], SmtFormula(_DECLS, "p"))
_NOT_ENTAILED: Final = ([SmtFormula(_DECLS, "p")], SmtFormula(_DECLS, "q"))


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[solver_workers.Workers]:
    """A private worker pool of one idle place, used by every check in the test."""
    workers = solver_workers.Workers(solver_mod._solve, idle_max=1)
    monkeypatch.setattr(solver_mod, "_workers", lambda: workers)
    yield workers
    workers.close()


def _own_interrupt(monkeypatch: pytest.MonkeyPatch) -> solver_mod._Interrupt:
    """The interrupt `check_entailment` uses on this thread, as a pool thread sets it."""
    interrupt = solver_mod._Interrupt()
    monkeypatch.setattr(solver_mod._worker, "interrupt", interrupt, raising=False)
    return interrupt


# --------------------------------------------------------------------------
# #1 (high): a check cut short in the caller must not leave its reply in the
# pipe. `check_entailment` kills the worker when anything raises while it waits
# (a per-test timeout's signal, KeyboardInterrupt). That branch is the only thing
# that keeps a worker with an unread reply out of the idle list, and no test
# reaches it (solver.py 238-241 uncovered). With `release` there (W06) the next
# check on that worker reads the previous check's reply: a VALID for a claim
# that is not entailed. The pipe carries no request id that would catch it.
# --------------------------------------------------------------------------

_W06: Final = (
    "pool.discard(worker)\n        raise",
    "pool.release(worker)\n        raise",
)


class _CutShort(BaseException):
    """What a signal handler raises in the caller while it waits on the pipe."""


def _verdict_after_a_check_cut_short(monkeypatch: pytest.MonkeyPatch) -> str:
    real_call = solver_workers.Worker.call

    def cut_short(worker: solver_workers.Worker, args: tuple[Any, ...], budget_s: float) -> Any:
        assert worker.ready()
        worker._send(args)
        assert worker.conn.poll(_WAIT_S)  # its reply is waiting, unread
        raise _CutShort

    monkeypatch.setattr(solver_workers.Worker, "call", cut_short)
    with pytest.raises(_CutShort):
        Z3Solver().check_entailment(*_ENTAILED)
    monkeypatch.setattr(solver_workers.Worker, "call", real_call)
    return Z3Solver().check_entailment(*_NOT_ENTAILED).verdict


def test_a_check_cut_short_never_answers_the_next_one(
    monkeypatch: pytest.MonkeyPatch, pool: solver_workers.Workers
) -> None:
    assert _verdict_after_a_check_cut_short(monkeypatch) == "INVALID"


def test_a_check_cut_short_never_answers_the_next_one_under_w06(
    monkeypatch: pytest.MonkeyPatch, pool: solver_workers.Workers
) -> None:
    # SG31: the reply's sequence number no longer matches, so the worker counts as lost.
    _mutant(monkeypatch, Z3Solver, "check_entailment", *_W06)
    assert _verdict_after_a_check_cut_short(monkeypatch) == "UNGROUNDED"


# --------------------------------------------------------------------------
# #4 (medium): a cancel that lands between the early `requested()` check and
# `attach` must still stop the check. `attach` refuses then, and the check
# returns `interrupted` without sending a request (solver.py 231-232
# uncovered). With the refusal ignored (W07) the request is sent to a worker no
# cancel can reach, and a cancelled or shut-down check returns a verdict.
# --------------------------------------------------------------------------

_W07: Final = ("if not interrupt.attach(worker):", "if not interrupt.attach(worker) and False:")


def _sent_after_a_cancel_at_acquire(
    monkeypatch: pytest.MonkeyPatch, pool: solver_workers.Workers
) -> tuple[SolverOutcome, list[tuple[Any, ...]]]:
    interrupt = _own_interrupt(monkeypatch)
    real_acquire = pool.acquire

    def acquire() -> solver_workers.Worker:
        worker = real_acquire()
        interrupt.request()  # the caller is cancelled while its worker is handed out
        return worker

    monkeypatch.setattr(pool, "acquire", acquire)
    sent: list[tuple[Any, ...]] = []

    def call(worker: solver_workers.Worker, args: tuple[Any, ...], budget_s: float) -> Any:
        sent.append(args)
        return (True, SolverOutcome("VALID", "entailed"))

    monkeypatch.setattr(solver_workers.Worker, "call", call)
    return Z3Solver().check_entailment(*_ENTAILED), sent


def test_a_cancel_at_acquire_sends_no_request(
    monkeypatch: pytest.MonkeyPatch, pool: solver_workers.Workers
) -> None:
    assert _sent_after_a_cancel_at_acquire(monkeypatch, pool) == (solver_mod._INTERRUPTED, [])


@pytest.mark.xfail(strict=True, reason="AUDIT-28 #4")
def test_a_cancel_at_acquire_sends_no_request_under_w07(
    monkeypatch: pytest.MonkeyPatch, pool: solver_workers.Workers
) -> None:
    _mutant(monkeypatch, Z3Solver, "check_entailment", *_W07)
    assert _sent_after_a_cancel_at_acquire(monkeypatch, pool) == (solver_mod._INTERRUPTED, [])


# --------------------------------------------------------------------------
# L1: a worker a cancel killed after its reply arrived is replaced, not parked.
# With `if reply is None:` only (W04) the dead worker goes back to the idle list
# and no replacement starts, so the next check pays a worker start-up. Verdicts
# are unchanged (`acquire` skips dead workers).
# --------------------------------------------------------------------------

_W04: Final = ("if reply is None or interrupt.requested():", "if reply is None:")


def _parked_after_a_late_cancel(
    monkeypatch: pytest.MonkeyPatch, pool: solver_workers.Workers
) -> tuple[str, bool, int]:
    interrupt = _own_interrupt(monkeypatch)
    real_call = solver_workers.Worker.call
    used: list[solver_workers.Worker] = []

    def call(worker: solver_workers.Worker, args: tuple[Any, ...], budget_s: float) -> Any:
        reply = real_call(worker, args, budget_s)
        used.append(worker)
        interrupt.request()  # kills the attached worker, after its reply
        return reply

    monkeypatch.setattr(solver_workers.Worker, "call", call)
    verdict = Z3Solver().check_entailment(*_ENTAILED).verdict
    return verdict, used[0] in pool._idle, sum(w.alive() for w in pool._idle)


def test_a_worker_killed_after_its_reply_is_replaced(
    monkeypatch: pytest.MonkeyPatch, pool: solver_workers.Workers
) -> None:
    assert _parked_after_a_late_cancel(monkeypatch, pool) == ("VALID", False, 1)


@pytest.mark.xfail(strict=True, reason="AUDIT-28 L1")
def test_a_worker_killed_after_its_reply_is_replaced_under_w04(
    monkeypatch: pytest.MonkeyPatch, pool: solver_workers.Workers
) -> None:
    _mutant(monkeypatch, Z3Solver, "check_entailment", *_W04)
    assert _parked_after_a_late_cancel(monkeypatch, pool) == ("VALID", False, 1)


# --------------------------------------------------------------------------
# #5 (medium): `solver_ms` must not count replacing a killed worker. The SG29
# test (`test_solver_ms_excludes_worker_start_up`) says it pins that ("nor is
# replacing the timed-out one"), but its hard check ends by Z3's own timeout
# inside the worker, which replies in time: no worker is killed or replaced, so
# dropping the post-check overhead (W09) passes it. Its start-up half is
# vacuous too once an earlier test has booted the shared workers (`_ready` is
# set, so its slow boot never sleeps): dropping the pre-check overhead (W10)
# passes the suite. Here the clock is fake: start-up takes 10 s, the call 1 s
# (it is killed), stopping the worker 100 s.
# --------------------------------------------------------------------------

_W09: Final = ("        + (time.perf_counter() - checked)\n", "")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _ms_of_a_killed_check(monkeypatch: pytest.MonkeyPatch, pool: solver_workers.Workers) -> float:
    clock = _Clock()
    monkeypatch.setattr(solver_mod, "time", SimpleNamespace(perf_counter=clock))
    monkeypatch.setattr(solver_mod, "_clock", clock)
    real_ready, real_stop = solver_workers.Worker.ready, solver_workers.Worker.stop

    def ready(worker: solver_workers.Worker) -> bool:
        clock.now += 10.0
        return real_ready(worker)

    def call(worker: solver_workers.Worker, args: tuple[Any, ...], budget_s: float) -> Any:
        clock.now += 1.0
        return None  # ran past its budget

    def stop(worker: solver_workers.Worker) -> None:
        clock.now += 100.0
        real_stop(worker)

    monkeypatch.setattr(solver_workers.Worker, "ready", ready)
    monkeypatch.setattr(solver_workers.Worker, "call", call)
    monkeypatch.setattr(solver_workers.Worker, "stop", stop)
    executor = SolverExecutor(1, 0)
    try:
        outcome, ms = executor._call(
            lambda: Z3Solver().check_entailment(*_ENTAILED), solver_mod._Interrupt()
        )
    finally:
        executor.shutdown()
    assert outcome == solver_mod._KILLED
    return ms


def test_solver_ms_excludes_starting_and_replacing_a_killed_worker(
    monkeypatch: pytest.MonkeyPatch, pool: solver_workers.Workers
) -> None:
    assert _ms_of_a_killed_check(monkeypatch, pool) == pytest.approx(1000.0)


@pytest.mark.xfail(strict=True, reason="AUDIT-28 #5")
def test_solver_ms_excludes_starting_a_worker_under_w10(
    monkeypatch: pytest.MonkeyPatch, pool: solver_workers.Workers
) -> None:
    _mutant(monkeypatch, Z3Solver, "check_entailment", "        + (checking - setup)\n", "")
    assert _ms_of_a_killed_check(monkeypatch, pool) == pytest.approx(1000.0)


@pytest.mark.xfail(strict=True, reason="AUDIT-28 #5")
def test_solver_ms_excludes_replacing_a_killed_worker_under_w09(
    monkeypatch: pytest.MonkeyPatch, pool: solver_workers.Workers
) -> None:
    _mutant(monkeypatch, Z3Solver, "check_entailment", *_W09)
    assert _ms_of_a_killed_check(monkeypatch, pool) == pytest.approx(1000.0)


# --------------------------------------------------------------------------
# #2 (medium): a cancelled slow check gives its slow place back only when its
# job has ended (AUDIT-22 #5). Only the AUDIT-22 #5 `perf` repro pins that;
# calling `_end_slow` at once (W17) passes every non-`perf` test. Here the
# check ignores the cancel (a worker whose kill is still in progress) until it
# is released, with no clock involved.
# --------------------------------------------------------------------------


class _Unstoppable:
    """A check's target that does not stop when interrupted (yet)."""

    def interrupt(self) -> None:
        return None


async def _slow_places_after_a_cancel() -> int:
    executor = SolverExecutor(1, 1)
    started, gate = threading.Event(), threading.Event()

    def check() -> None:
        interrupt: solver_mod._Interrupt = solver_mod._worker.interrupt
        assert interrupt.attach(_Unstoppable())
        started.set()
        try:
            gate.wait(_WAIT_S)
        finally:
            interrupt.detach()

    try:
        task = asyncio.create_task(executor.run(check))
        assert await asyncio.to_thread(started.wait, _WAIT_S)
        await _until(lambda: executor._slow == 1)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        during = executor._slow
        gate.set()
        await _until(lambda: executor._slow == 0)
        return during
    finally:
        gate.set()
        executor.shutdown()


async def test_a_cancelled_slow_check_keeps_its_place_until_it_ends() -> None:
    assert await _slow_places_after_a_cancel() == 1


@pytest.mark.xfail(strict=True, reason="AUDIT-28 #2")
async def test_a_cancelled_slow_check_keeps_its_place_under_w17(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(
        monkeypatch,
        SolverExecutor,
        "run",
        "job.add_done_callback(self._end_slow)",
        "self._end_slow(job)",
    )
    assert await _slow_places_after_a_cancel() == 1


# --------------------------------------------------------------------------
# L2: the kill grace is ADR 0009's `max(10 ms, 10% of the timeout)`. Only its
# negation is tested (`_grace_s` patched to `-timeout`); `min` for `max` (W02)
# or a 100% share (W03, which lets a check run to 2x its timeout) passes every
# non-`perf` test. The documented share is hard-coded, as in AUDIT-24 #5.
# --------------------------------------------------------------------------

_ADR_SHARE: Final = 0.1


def _graces() -> list[float]:
    return [solver_mod._grace_s(t) for t in (0.01, 0.05, 1.0, 30.0)]


def test_the_kill_grace_is_the_documented_one() -> None:
    assert _graces() == pytest.approx(
        [solver_mod._KILL_GRACE_MIN_S, solver_mod._KILL_GRACE_MIN_S, _ADR_SHARE, 30 * _ADR_SHARE]
    )


@pytest.mark.xfail(strict=True, reason="AUDIT-28 L2")
def test_the_kill_grace_is_the_documented_one_under_w03(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(solver_mod, "_KILL_GRACE_SHARE", 1.0)
    test_the_kill_grace_is_the_documented_one()


@pytest.mark.xfail(strict=True, reason="AUDIT-28 L2")
def test_the_kill_grace_is_the_documented_one_under_w02(monkeypatch: pytest.MonkeyPatch) -> None:
    _mutant(monkeypatch, solver_mod, "_grace_s", "return max(", "return min(")
    test_the_kill_grace_is_the_documented_one()


# --------------------------------------------------------------------------
# L3: `SolverExecutor()` starts its workers off the caller's thread (ADR 0009,
# "start-up off the hot path"), and the shared pool keeps a worker idle for
# every place. Removing the prestart (W16) or keeping one idle worker (W15)
# passes every non-`perf` test (W16 also the AUDIT-22 `perf` repros); checks then
# pay worker start-ups.
# --------------------------------------------------------------------------

_W16: Final = (
    "threading.Thread(\n"
    "        target=_workers().prestart, args=(workers + slow_workers,), daemon=True\n"
    "    ).start()",
    "pass",
)


def _prestarted(monkeypatch: pytest.MonkeyPatch) -> tuple[int, bool]:
    asked: list[tuple[int, bool]] = []
    done = threading.Event()
    caller = threading.current_thread()

    def prestart(workers: solver_workers.Workers, n: int) -> None:
        asked.append((n, threading.current_thread() is caller))
        done.set()

    monkeypatch.setattr(solver_workers.Workers, "prestart", prestart)
    SolverExecutor(2, 1).shutdown()
    assert done.wait(_WAIT_S), "no prestart"
    return asked[0]


def test_an_executor_prestarts_its_workers_off_the_callers_thread(
    monkeypatch: pytest.MonkeyPatch, pool: solver_workers.Workers
) -> None:
    assert _prestarted(monkeypatch) == (3, False)


@pytest.mark.xfail(strict=True, reason="AUDIT-28 L3")
def test_an_executor_prestarts_its_workers_under_w16(
    monkeypatch: pytest.MonkeyPatch, pool: solver_workers.Workers
) -> None:
    _mutant(monkeypatch, SolverExecutor, "__init__", *_W16)
    assert _prestarted(monkeypatch) == (3, False)


# --------------------------------------------------------------------------
# L6: how long a check must run to make its run hard. Every SG30 test reads
# `_HARD_SLICES` itself, so doubling it (A09: 8 slices, 400 ms) passes the suite.
# The SG30 value (4 slices of 50 ms) is hard-coded here, as in AUDIT-24 #5: a
# check of 250 ms is hard, one of 150 ms is not.
# --------------------------------------------------------------------------

_DOCUMENTED_HARD_S: Final = 4 * 0.05


async def _hard_after(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    clock = _Clock()
    monkeypatch.setattr(solver_mod, "_clock", clock)
    executor = SolverExecutor(1, 0)
    me = asyncio.current_task()
    hard: list[bool] = []
    try:
        for ran_s in (0.75 * _DOCUMENTED_HARD_S, 1.25 * _DOCUMENTED_HARD_S):

            def check(ran_s: float = ran_s) -> None:
                clock.now += ran_s

            await executor.run(check)
            hard.append(me not in executor._short)
    finally:
        executor.shutdown()
    return hard


async def test_a_check_past_the_documented_run_time_makes_its_run_hard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _hard_after(monkeypatch) == [False, True]


@pytest.mark.xfail(strict=True, reason="AUDIT-28 L6")
async def test_a_check_past_the_documented_run_time_under_a09(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(solver_mod, "_HARD_SLICES", 8)
    assert await _hard_after(monkeypatch) == [False, True]


# --------------------------------------------------------------------------
# L4: code that runs only in the worker process. Coverage cannot see it (workers
# end by SIGKILL, so a child's data is never saved: `_serve` and `_die_with_parent`
# show 0% even with multiprocessing coverage), and no test
# checks that a worker ignores Ctrl-C (W26) or asks to die with its parent (W27):
# both pass the suite. `_serve` is run here in this process on the main thread,
# where a mutant can reach it, with `prctl` replaced by a recorder.
# --------------------------------------------------------------------------


def _serve_here(monkeypatch: pytest.MonkeyPatch) -> tuple[list[object], int, bool]:
    """(replies, `_die_with_parent` calls, whether Ctrl-C stopped the loop)."""
    died: list[bool] = []
    monkeypatch.setattr(solver_workers, "_die_with_parent", lambda: died.append(True))
    parent, child = multiprocessing.Pipe()
    replies: list[object] = []

    def drive() -> None:
        try:
            assert parent.poll(_WAIT_S)
            replies.append(parent.recv())  # ready
            signal.raise_signal(signal.SIGINT)  # Ctrl-C reaches the whole process group
            for request in ((1, (6, 3)), (2, (1, 0))):
                parent.send(request)
                if parent.poll(_WAIT_S):
                    replies.append(parent.recv())
        except (EOFError, OSError):  # the loop ended early (the mutants)
            pass
        finally:
            parent.close()

    previous = signal.getsignal(signal.SIGINT)
    driver = threading.Thread(target=drive)
    driver.start()
    interrupted = False
    try:
        solver_workers._serve(child, operator.truediv)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        signal.signal(signal.SIGINT, previous)
        child.close()
        driver.join()
    return replies, len(died), interrupted


_SERVED: Final = (
    [None, (1, (True, 2.0)), (2, (False, "ZeroDivisionError: division by zero"))],
    1,
    False,
)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT semantics")
def test_a_worker_ignores_ctrl_c_and_asks_to_die_with_its_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _serve_here(monkeypatch) == _SERVED


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT semantics")
@pytest.mark.xfail(strict=True, reason="AUDIT-28 L4")
def test_a_worker_ignores_ctrl_c_under_w26(monkeypatch: pytest.MonkeyPatch) -> None:
    _mutant(
        monkeypatch,
        solver_workers,
        "_serve",
        "signal.signal(signal.SIGINT, signal.SIG_IGN)",
        "signal.getsignal(signal.SIGINT)",
    )
    assert _serve_here(monkeypatch) == _SERVED


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT semantics")
@pytest.mark.xfail(strict=True, reason="AUDIT-28 L4")
def test_a_worker_asks_to_die_with_its_parent_under_w27(monkeypatch: pytest.MonkeyPatch) -> None:
    _mutant(monkeypatch, solver_workers, "_serve", "    _die_with_parent()\n", "")
    assert _serve_here(monkeypatch) == _SERVED


# --------------------------------------------------------------------------
# L5: three `Workers` details no test reaches. `close` must stop the idle
# processes, not only forget them (W36: `live()` is 0 while they run on);
# `acquire` must forget a dead idle worker (W37: `live()` counts it for ever).
# W33's `_main_imports` is gone: since SG32 a worker never re-runs the main module.
# --------------------------------------------------------------------------


def _idle_left_running() -> list[bool]:
    workers = solver_workers.Workers(operator.truediv, idle_max=2)
    workers.prestart(2)
    idle = list(workers._idle)
    workers.close()
    return [w.alive() for w in idle]


def test_closing_a_pool_stops_its_idle_processes() -> None:
    assert _idle_left_running() == [False, False]


@pytest.mark.xfail(strict=True, reason="AUDIT-28 L5")
def test_closing_a_pool_stops_its_idle_processes_under_w36(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(
        monkeypatch,
        solver_workers.Workers,
        "close",
        "for worker in idle:\n        worker.stop()",
        "for worker in idle:\n        pass",
    )
    try:
        assert _idle_left_running() == [False, False]
    finally:
        for process in multiprocessing.active_children():
            if process.name == "z3-worker":
                process.kill()


def _live_after_skipping_a_dead_worker() -> int:
    workers = solver_workers.Workers(operator.truediv, idle_max=2)
    try:
        workers.prestart(1)
        dead = workers._idle[0]
        dead.process.kill()
        dead.process.join()
        worker = workers.acquire()
        assert worker is not dead
        workers.release(worker)
        return workers.live()
    finally:
        workers.close()


def test_a_dead_idle_worker_is_no_longer_counted() -> None:
    assert _live_after_skipping_a_dead_worker() == 1


@pytest.mark.xfail(strict=True, reason="AUDIT-28 L5")
def test_a_dead_idle_worker_is_no_longer_counted_under_w37(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(
        monkeypatch,
        solver_workers.Workers,
        "acquire",
        "            self._live.discard(worker)\n",
        "",
    )
    assert _live_after_skipping_a_dead_worker() == 1


def _idle_places(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(solver_mod, "_shared_workers", None)  # restored after the test
    workers = solver_mod._workers()
    try:
        return workers.idle_max
    finally:
        workers.close()


_ADR_IDLE: Final = 2 * solver_mod.SOLVER_WORKERS + solver_mod.SOLVER_SLOW_WORKERS
"""ADR 0009: every slot, slow check and lent place of the default pool finds an idle worker."""


def test_the_shared_pool_keeps_a_worker_idle_for_every_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _idle_places(monkeypatch) >= _ADR_IDLE


@pytest.mark.xfail(strict=True, reason="AUDIT-28 L3")
def test_the_shared_pool_keeps_a_worker_idle_for_every_place_under_w15(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(solver_mod, "_IDLE_WORKERS", 1)
    assert _idle_places(monkeypatch) >= _ADR_IDLE


# --------------------------------------------------------------------------
# #6 (low): the SG28 tokenizer test catches the quadratic scan (F09) only through
# pytest-timeout's 60 s: it has no bound of its own, and the mutant took 76 s to
# be killed here. On a machine ~3x faster, or with `--timeout=0`, it passes. The
# blank tail is timed against a twin of the same length that ends in tokens.
# --------------------------------------------------------------------------

_BLANKS: Final = 4000
_LINEAR_FACTOR: Final = 20
"""The blank tail may cost this many times the token twin; quadratic costs ~1000x."""


def _best_s(fn: Callable[[], object], rounds: int = 3) -> float:
    best = float("inf")
    for _ in range(rounds):
        started = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - started)
    return best


def _blank_tail_over_token_twin() -> float:
    head = "(and a b)"
    blanks = head + " " * _BLANKS
    twin = head + " x" * (_BLANKS // 2)
    assert formalizer._tokens(blanks) == {"and", "a", "b"}
    return _best_s(lambda: formalizer._tokens(blanks)) / _best_s(lambda: formalizer._tokens(twin))


@pytest.mark.perf
def test_a_blank_tail_costs_no_more_than_a_tail_of_tokens() -> None:
    assert _blank_tail_over_token_twin() <= _LINEAR_FACTOR


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-28 #6")
def test_a_blank_tail_costs_no_more_than_a_tail_of_tokens_under_f09(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(monkeypatch, formalizer, "_tokens", "finditer(text.rstrip())}", "finditer(text)}")
    assert _blank_tail_over_token_twin() <= _LINEAR_FACTOR


# --------------------------------------------------------------------------
# #3 (medium): `test_judges_overlap_but_formalization_stays_ordered` bounds the
# wall time at 8 LLM delays in absolute terms. Since SG29 the first check of a
# process starts its workers (0.4-6 s here), and a warm run already takes ~7.6
# delays, so it fails whenever it runs alone (6 of 6 here) and under load.
# Calibrated: the same run with no latency is the baseline, and overlap must save
# at least half of the serial cost (12 calls in turn against 7 delays overlapped).
# --------------------------------------------------------------------------

_STEPS: Final = 6
_DELAY_S: Final = 0.1


async def _verify(delay_s: float) -> tuple[float, list[str], int]:
    trace, fake = build(chain(_STEPS, cite=True))
    order: list[str] = []

    class Recording(LatencyLLM):
        async def call_structured(
            self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
        ) -> SchemaT:
            if purpose == "formalize":
                order.append(prompt.split("step_index: ")[1].split("\n")[0])
            return await super().call_structured(purpose, prompt, schema)

    llm = Recording(fake, delay_s)
    started = time.perf_counter()
    result = await Verifier(llm, check_faithfulness=True).verify(trace)
    wall = time.perf_counter() - started
    assert result.consistency_score == 1.0
    return wall, order, llm.calls


@pytest.mark.perf
async def test_judges_overlap_against_a_baseline_without_latency() -> None:
    await _verify(0.0)  # starts the worker processes
    base = min([(await _verify(0.0))[0] for _ in range(2)])
    delay_s = max(_DELAY_S, 2 * base)
    wall, order, calls = await _verify(delay_s)
    assert order == [str(i) for i in range(_STEPS)]
    overlapped = _STEPS + 1  # formalize in turn, the last judge after them
    assert calls > overlapped + 2  # so the bound below tells overlap from none
    assert wall - base < (overlapped + calls) / 2 * delay_s, (wall, base, delay_s, calls)
