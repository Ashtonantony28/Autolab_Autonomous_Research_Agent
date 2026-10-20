"""SG26: the solver pool's own threads, the slot lending and SG22's fixes, without wall clocks.

Every test orders its events with events, counters or a fake clock; timing only
bounds a wait that a failing run would otherwise hang on.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable, Iterator
from functools import partial
from typing import Final

import pytest
import z3

from autolab.verification import solver as solver_mod
from autolab.verification.solver import (
    SmtFormula,
    SolverExecutor,
    SolverOutcome,
    Z3Solver,
    _Pool,
    _Slots,
)

_WAIT_S: Final = 10.0
"""Bounds a failing wait on an event; a passing run never waits this long."""

_DECLS: Final = "(declare-const p Bool)"


class _Held:
    """A call that attaches a Z3 context, as a running check does, until released."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.gate = threading.Event()

    def __call__(self) -> str:
        interrupt = solver_mod._worker.interrupt
        assert interrupt.attach(z3.Context())
        self.started.set()
        try:
            self.gate.wait(_WAIT_S)
        finally:
            interrupt.detach()
        return "held"


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


@pytest.fixture
def executors() -> Iterator[list[SolverExecutor]]:
    made: list[SolverExecutor] = []
    yield made
    for executor in made:
        executor.shutdown()


def _executor(made: list[SolverExecutor], workers: int, slow_workers: int) -> SolverExecutor:
    made.append(SolverExecutor(workers, slow_workers))
    return made[-1]


# --- `_Pool`: explicit worker threads (AUDIT-18 #2) ------------------------------------


def test_a_call_submitted_before_the_worker_is_idle_still_gets_a_thread() -> None:
    # The interleaving behind AUDIT-18 #2: the next call is submitted after the previous
    # one's result is out but before its worker has counted itself idle. The worker must
    # take that call, and must not leave an idle count behind that a later call would
    # trust instead of starting a thread.
    pool = _Pool(1)
    finishing, submitted = threading.Event(), threading.Event()
    real_next = pool._next

    def held_next() -> bool:
        finishing.set()
        assert submitted.wait(_WAIT_S)
        return real_next()

    pool._next = held_next  # type: ignore[method-assign]
    try:
        assert pool.submit(lambda: 1).result(_WAIT_S) == 1
        assert finishing.wait(_WAIT_S)
        second = pool.submit(lambda: 2)
        submitted.set()
        assert second.result(_WAIT_S) == 2
        pool._next = real_next  # type: ignore[method-assign]
        blocked = threading.Event()
        long = pool.submit(blocked.wait, _WAIT_S)
        pool.resize(1)  # what a check that goes slow does
        assert pool.submit(lambda: 3).result(_WAIT_S) == 3  # not queued behind `long`
        assert not long.done()
        assert len(pool._threads) == 2
        blocked.set()
        assert long.result(_WAIT_S)
    finally:
        pool.shutdown()


def test_a_raised_limit_starts_a_thread_for_a_queued_call() -> None:
    pool = _Pool(1)
    blocked = threading.Event()
    try:
        long = pool.submit(blocked.wait, _WAIT_S)
        queued = pool.submit(lambda: "queued")
        assert pool._work_queue.qsize() >= 1
        pool.resize(1)
        assert queued.result(_WAIT_S) == "queued"
        assert not long.done()
    finally:
        blocked.set()
        pool.shutdown()


def test_threads_above_a_lowered_limit_end_after_their_call() -> None:
    pool = _Pool(1)
    pool.resize(2)
    gate = threading.Event()
    try:
        held = [pool.submit(gate.wait, _WAIT_S) for _ in range(3)]
        assert len(pool._threads) == 3
        pool.resize(-2)
        gate.set()
        assert all(f.result(_WAIT_S) for f in held)
        for _ in range(3):
            assert pool.submit(lambda: None).result(_WAIT_S) is None
        _until_sync(lambda: len(pool._threads) == 1)
        assert pool._max_workers == 1
    finally:
        pool.shutdown()


def test_shutdown_runs_queued_calls_and_refuses_new_ones() -> None:
    pool = _Pool(1)
    gate = threading.Event()
    first = pool.submit(gate.wait, _WAIT_S)
    queued = pool.submit(lambda: "ran")
    stopper = threading.Thread(target=pool.shutdown)
    stopper.start()
    gate.set()
    stopper.join(_WAIT_S)
    assert not stopper.is_alive()
    assert first.result(0) and queued.result(0) == "ran"
    assert not any(t.is_alive() for t in pool._threads)
    with pytest.raises(RuntimeError):
        pool.submit(lambda: None)


def test_a_call_that_raises_hands_the_error_to_its_future() -> None:
    pool = _Pool(1)
    try:
        with pytest.raises(ZeroDivisionError):
            pool.submit(lambda: 1 // 0).result(_WAIT_S)
        assert pool.submit(lambda: "next").result(_WAIT_S) == "next"
    finally:
        pool.shutdown()


def test_a_cancelled_queued_call_never_runs() -> None:
    pool = _Pool(1)
    gate = threading.Event()
    calls: list[int] = []
    try:
        first = pool.submit(gate.wait, _WAIT_S)
        queued = pool.submit(lambda: calls.append(1))
        assert queued.cancel()
        gate.set()
        assert first.result(_WAIT_S)
        assert pool.submit(lambda: None).result(_WAIT_S) is None
    finally:
        pool.shutdown()
    assert calls == []


# --- `_Slots`: per-loop admission and lending (AUDIT-18 #4) -------------------------------


async def test_slots_are_granted_in_order() -> None:
    slots = _Slots(1)
    assert await slots.acquire(short=False) is False
    waiting = [asyncio.create_task(slots.acquire(short=False)) for _ in range(2)]
    await asyncio.sleep(0)
    assert not any(t.done() for t in waiting)
    slots.release(lent=False)
    assert await waiting[0] is False
    assert not waiting[1].done()
    slots.release(lent=False)
    assert await waiting[1] is False


async def test_a_free_slot_goes_to_a_short_caller_first() -> None:
    # A hard run re-queues as soon as its check ends; a slot it frees must not go to
    # the next hard run while an easy one waits (AUDIT-18 #4).
    slots = _Slots(1)
    assert await slots.acquire(short=False) is False
    hard = asyncio.create_task(slots.acquire(short=False))
    easy = asyncio.create_task(slots.acquire(short=True))
    await asyncio.sleep(0)
    slots.release(lent=False)
    assert await easy is False
    assert not hard.done()
    slots.release(lent=False)
    assert await hard is False


async def test_a_stuck_slot_lends_one_place_to_a_short_caller_only() -> None:
    slots = _Slots(1)
    assert await slots.acquire(short=False) is False
    fresh = asyncio.create_task(slots.acquire(short=False))
    short = asyncio.create_task(slots.acquire(short=True))
    also_short = asyncio.create_task(slots.acquire(short=True))
    await asyncio.sleep(0)
    slots.stick()
    assert await short is True  # overtakes the caller queued before it
    await asyncio.sleep(0)
    assert not fresh.done() and not also_short.done()  # one place per stuck slot
    slots.release(lent=True)
    assert await also_short is True
    slots.release(lent=True)
    slots.unstick()
    slots.release(lent=False)
    assert await fresh is False
    assert (slots.held, slots.lent, slots.stuck) == (1, 0, 0)


async def test_a_caller_cancelled_after_its_grant_gives_the_place_back() -> None:
    slots = _Slots(1)
    assert await slots.acquire(short=False) is False
    first = asyncio.create_task(slots.acquire(short=False))
    second = asyncio.create_task(slots.acquire(short=False))
    await asyncio.sleep(0)
    slots.release(lent=False)  # granted to `first`, which has not resumed yet
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await second is False
    assert slots.held == 1


async def test_a_caller_cancelled_while_waiting_leaves_the_queue() -> None:
    slots = _Slots(1)
    assert await slots.acquire(short=False) is False
    first = asyncio.create_task(slots.acquire(short=False))
    second = asyncio.create_task(slots.acquire(short=False))
    await asyncio.sleep(0)
    first.cancel()
    slots.release(lent=False)  # `first` is cancelled but not yet removed: skipped
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await second is False
    assert not slots._waiters


# --- `SolverExecutor` scheduling ------------------------------------------------------


async def test_slow_checks_never_exceed_slow_workers(executors: list[SolverExecutor]) -> None:
    # The slow-worker cap (AUDIT-20 #5): 1 slot, 2 slow. Of four held Z3 calls from fresh
    # callers, two go slow, the third stays stuck in the slot and the fourth waits.
    executor = _executor(executors, 1, 2)
    held = [_Held() for _ in range(4)]
    tasks: list[asyncio.Task[tuple[str, float]]] = []
    for h in held[:3]:
        tasks.append(asyncio.create_task(executor.run(h)))
        assert await asyncio.to_thread(h.started.wait, _WAIT_S)
        await _until(lambda: executor._slow == min(len(tasks), 2))
    tasks.append(asyncio.create_task(executor.run(held[3])))
    slots = executor._slots[asyncio.get_running_loop()]
    await _until(lambda: slots.stuck == 1 and len(slots._waiters) == 1)
    assert executor._slow == 2
    assert not held[3].started.is_set()
    assert len(executor._pool._threads) == 3
    held[0].gate.set()  # a slow check ends; the stuck one takes its place, the slot frees
    assert await asyncio.to_thread(held[3].started.wait, _WAIT_S)
    for h in held:
        h.gate.set()
    assert [r for r, _ in await asyncio.gather(*tasks)] == ["held"] * 4
    assert (executor._slow, executor._pool._max_workers, slots.held, slots.stuck) == (0, 1, 0, 0)


async def test_a_short_caller_overtakes_hard_checks_above_the_slow_cap(
    executors: list[SolverExecutor],
) -> None:
    # AUDIT-18 #4 without wall clocks: no slow room, the slot is stuck and a fresh caller
    # waits. A caller whose last check was short runs at once; a fresh one does not.
    executor = _executor(executors, 1, 0)
    await executor.run(lambda: None)  # this task's last check was short
    hard, other = _Held(), _Held()
    hard_task = asyncio.create_task(executor.run(hard))
    assert await asyncio.to_thread(hard.started.wait, _WAIT_S)
    other_task = asyncio.create_task(executor.run(other))
    slots = executor._slots[asyncio.get_running_loop()]
    await _until(lambda: slots.stuck == 1)
    assert (await executor.run(lambda: "easy"))[0] == "easy"
    assert not other.started.is_set() and not hard.gate.is_set()

    async def fresh() -> str:
        return (await executor.run(lambda: "fresh"))[0]

    fresh_task = asyncio.create_task(fresh())
    await _until(lambda: len(slots._waiters) == 2)
    hard.gate.set()
    other.gate.set()
    assert await fresh_task == "fresh"
    await asyncio.gather(hard_task, other_task)
    assert (slots.held, slots.lent, slots.stuck, executor._pool._max_workers) == (0, 0, 0, 1)


async def test_a_lent_place_gets_its_own_thread(executors: list[SolverExecutor]) -> None:
    executor = _executor(executors, 1, 0)
    await executor.run(lambda: None)  # this task's last check was short
    hard, lent = _Held(), _Held()
    hard_task = asyncio.create_task(executor.run(hard))
    assert await asyncio.to_thread(hard.started.wait, _WAIT_S)
    slots = executor._slots[asyncio.get_running_loop()]
    await _until(lambda: slots.stuck == 1)
    seen: list[tuple[int, int, int]] = []

    async def drive() -> None:
        assert await asyncio.to_thread(lent.started.wait, _WAIT_S)
        seen.append((slots.lent, executor._pool._max_workers, len(executor._pool._threads)))
        lent.gate.set()
        hard.gate.set()

    driver = asyncio.create_task(drive())
    assert (await executor.run(lent))[0] == "held"
    await asyncio.gather(driver, hard_task)
    assert seen == [(1, 2, 2)]
    assert executor._pool._max_workers == 1


async def test_a_caller_whose_check_ran_long_is_not_lent_a_place(
    executors: list[SolverExecutor],
) -> None:
    executor = _executor(executors, 1, 0)
    held = _Held()
    task = asyncio.create_task(executor.run(held))
    assert await asyncio.to_thread(held.started.wait, _WAIT_S)
    await _until(lambda: executor._slots[asyncio.get_running_loop()].stuck == 1)
    held.gate.set()
    await task
    assert task not in executor._short
    me = asyncio.current_task()
    await executor.run(lambda: None)
    assert me in executor._short
    await executor.run(partial(time.sleep, 2 * solver_mod._SLICE_S))
    assert me not in executor._short
    await executor.run(lambda: None)
    assert me in executor._short


async def test_a_lent_check_that_goes_slow_returns_its_place(
    executors: list[SolverExecutor],
) -> None:
    executor = _executor(executors, 1, 1)
    await executor.run(lambda: None)  # this task's last check was short
    slow, stuck, lent = _Held(), _Held(), _Held()
    tasks = [asyncio.create_task(executor.run(slow))]
    await _until(lambda: executor._slow == 1)
    tasks.append(asyncio.create_task(executor.run(stuck)))
    slots = executor._slots[asyncio.get_running_loop()]
    await _until(lambda: slots.stuck == 1)
    seen: list[tuple[int, int, int]] = []

    async def drive() -> None:
        assert await asyncio.to_thread(lent.started.wait, _WAIT_S)
        seen.append((slots.lent, executor._slow, slots.stuck))
        stuck.gate.set()  # the lender ends; the lent place stays until its check ends
        await _until(lambda: slots.stuck == 0)
        seen.append((slots.lent, executor._slow, slots.stuck))
        slow.gate.set()
        await _until(lambda: slots.lent == 0)  # the lent check took the free slow place
        seen.append((slots.lent, executor._slow, slots.stuck))
        lent.gate.set()

    driver = asyncio.create_task(drive())
    assert (await executor.run(lent))[0] == "held"
    await asyncio.gather(driver, *tasks)
    assert seen == [(1, 1, 1), (1, 1, 0), (0, 1, 0)]
    assert (executor._slow, slots.held, slots.lent, slots.stuck) == (0, 0, 0, 0)
    assert executor._pool._max_workers == 1


async def test_shutdown_interrupts_a_queued_z3_check_instead_of_cancelling_it(
    executors: list[SolverExecutor],
) -> None:
    # AUDIT-6 #7 on one loop's pool queue: a second loop's check waits in the pool.
    executor = _executor(executors, 1, 0)
    hard = _Held()
    first = asyncio.create_task(executor.run(hard))
    assert await asyncio.to_thread(hard.started.wait, _WAIT_S)
    check = partial(
        Z3Solver(30.0).check_entailment, [SmtFormula(_DECLS, "p")], SmtFormula(_DECLS, "p")
    )
    outcome: list[object] = []

    def other_loop() -> None:
        async def main() -> SolverOutcome:
            return (await executor.run(check))[0]

        try:
            outcome.append(asyncio.run(main()))
        except BaseException as exc:  # recorded: the assertion is about it
            outcome.append(exc)

    thread = threading.Thread(target=other_loop)
    thread.start()
    await _until(lambda: executor._pool._work_queue.qsize() == 1)
    stopper = asyncio.create_task(asyncio.to_thread(executor.shutdown))
    await _until(lambda: executor._closed)
    hard.gate.set()
    await stopper
    await asyncio.to_thread(thread.join, _WAIT_S)
    assert (await first)[0] == "held"
    assert isinstance(outcome[0], SolverOutcome)
    assert (outcome[0].reason, outcome[0].details) == ("timeout", "interrupted")
    with pytest.raises(RuntimeError):
        await executor.run(lambda: None)


# --- Sizing from the CPUs this process may use (AUDIT-18 #5) ------------------------------


def test_sizing_follows_the_affinity_not_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "process_cpu_count", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: {3}, raising=False)
    assert solver_mod._available_cpus() == 1


def test_sizing_prefers_process_cpu_count_where_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "process_cpu_count", lambda: 3, raising=False)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: {0}, raising=False)
    assert solver_mod._available_cpus() == 3


def test_slots_and_slow_checks_do_not_outnumber_the_cpus() -> None:
    cpus = solver_mod._available_cpus()
    assert min(2, cpus) == solver_mod.SOLVER_WORKERS
    assert max(cpus, 1) == solver_mod.SOLVER_WORKERS + solver_mod.SOLVER_SLOW_WORKERS


# --- SG22's fixes, deterministically (AUDIT-20 #5) ------------------------------------------


def test_the_negated_claim_is_asserted_and_checked_without_assumptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added: list[str] = []
    assumed: list[int] = []
    real_add, real_check = z3.Solver.add, z3.Solver.check

    def add(self: z3.Solver, *args: z3.ExprRef) -> None:
        added.extend(str(a) for a in args)
        real_add(self, *args)

    def check(self: z3.Solver, *assumptions: z3.ExprRef) -> z3.CheckSatResult:
        assumed.append(len(assumptions))
        return real_check(self, *assumptions)

    monkeypatch.setattr(z3.Solver, "add", add)
    monkeypatch.setattr(z3.Solver, "check", check)
    out = Z3Solver(5.0).check_entailment([SmtFormula(_DECLS, "p")], SmtFormula(_DECLS, "p"))
    assert out.verdict == "VALID"
    assert added == [solver_mod._NEGATED_CLAIM]
    assert assumed == [0, 0]


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.perf_counter = time.perf_counter

    def monotonic(self) -> float:
        return self.now


@pytest.mark.parametrize(
    ("consistency_s", "checks"),
    [(0.6, 2), (0.66, 2), (0.67, 1), (0.9, 1)],
    ids=["ample", "just-enough", "just-short", "short"],
)
def test_the_setup_guard_needs_half_the_consistency_time_left(
    monkeypatch: pytest.MonkeyPatch, consistency_s: float, checks: int
) -> None:
    # 1 s budget: after a consistency check of c s, 1 - c s are left; the entailment
    # check starts only if 1 - c >= c / 2, that is c <= 2/3.
    clock = _Clock()
    calls: list[int] = []
    real_check = z3.Solver.check

    def check(self: z3.Solver, *args: z3.ExprRef) -> z3.CheckSatResult:
        calls.append(1)
        out = real_check(self, *args)
        if len(calls) == 1:
            clock.now += consistency_s
        return out

    monkeypatch.setattr(solver_mod, "time", clock)
    monkeypatch.setattr(z3.Solver, "check", check)
    out = Z3Solver(1.0).check_entailment([SmtFormula(_DECLS, "p")], SmtFormula(_DECLS, "p"))
    assert len(calls) == checks
    if checks == 1:
        assert (out.reason, out.details) == (
            "timeout",
            "too little time left for the entailment check",
        )
    else:
        assert out.verdict == "VALID"
