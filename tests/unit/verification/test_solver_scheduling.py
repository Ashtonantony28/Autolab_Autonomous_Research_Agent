"""SG26: the solver pool's own threads, the slot lending and SG22's fixes, without wall clocks.

Every test orders its events with events, counters or a fake clock; timing only
bounds a wait that a failing run would otherwise hang on.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from functools import partial
from typing import Final

import pytest
import z3
from hypothesis import given, settings
from hypothesis import strategies as st

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
    slots.stick(lent=False)
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


async def _queued(slots: _Slots, short: bool, new: bool = False) -> asyncio.Task[bool]:
    task = asyncio.create_task(slots.acquire(short, new))
    await asyncio.sleep(0)
    return task


@pytest.mark.parametrize("workers", [1, 2, 3])
async def test_a_caller_overtaken_for_every_slot_is_served_next(workers: int) -> None:
    # AUDIT-22 #4: a stream of short callers no longer holds a hard one back for ever.
    slots = _Slots(workers)
    for _ in range(workers):
        assert await slots.acquire(short=False) is False
    hard = await _queued(slots, short=False)
    for _ in range(workers):
        easy = await _queued(slots, short=True)
        slots.release(lent=False)
        assert await easy is False
        assert not hard.done()
    late = await _queued(slots, short=True)
    slots.release(lent=False)
    assert await hard is False  # served before the short caller that waits
    assert not late.done()
    slots.release(lent=False)
    assert await late is False


async def test_callers_overtaken_alike_are_served_in_order() -> None:
    slots = _Slots(1)
    assert await slots.acquire(short=False) is False
    first, second = await _queued(slots, short=False), await _queued(slots, short=False)
    easy = await _queued(slots, short=True)
    slots.release(lent=False)
    assert await easy is False
    late = await _queued(slots, short=True)
    for expected in (first, second, late):
        slots.release(lent=False)
        assert await expected is False


async def test_a_lent_place_does_not_overtake_a_caller_that_could_not_take_it() -> None:
    # Only a slot grant counts against the callers ahead: a hard caller cannot use a lent
    # place, so lending to short ones behind it does not age it.
    slots = _Slots(1)
    assert await slots.acquire(short=False) is False
    hard = await _queued(slots, short=False)
    easy = await _queued(slots, short=True)
    assert slots.stick(lent=False)  # lends to `easy`, behind `hard`
    for _ in range(3):
        assert await easy is True
        easy = await _queued(slots, short=True)
        slots.release(lent=True)
    assert await easy is True
    slots.release(lent=True)
    assert slots._waiters[0].passed == 0
    late = await _queued(slots, short=True)
    assert await late is True
    slots.unstick()
    slots.release(lent=False)
    assert await hard is False


async def test_newcomers_are_lent_places_latest_first() -> None:
    # AUDIT-30 #1: runs that start together are all newcomers. The latest is lent the next
    # place, so a newcomer never waits for the newcomers queued before it.
    slots = _Slots(1)
    assert await slots.acquire(short=False) is False
    burst = [await _queued(slots, short=False, new=True) for _ in range(2)]
    hard = await _queued(slots, short=False)
    latest = await _queued(slots, short=False, new=True)
    assert slots.stick(lent=False)
    assert await latest is True
    for expected in reversed(burst):
        assert not expected.done()
        slots.release(lent=True)
        assert await expected is True
    slots.release(lent=True)
    assert not hard.done()  # a hard caller is lent nothing
    slots.unstick()
    slots.release(lent=False)
    assert await hard is False


async def test_a_newcomer_overtaken_for_every_lent_place_twice_is_lent_the_next() -> None:
    slots = _Slots(1)
    assert await slots.acquire(short=False) is False
    first = await _queued(slots, short=False, new=True)
    later = await _queued(slots, short=False, new=True)
    assert slots.stick(lent=False)
    for _ in range(solver_mod._LENT_AGING):
        assert await later is True
        later = await _queued(slots, short=False, new=True)
        slots.release(lent=True)
    assert await first is True  # served before the latest newcomer
    assert not later.done()
    slots.release(lent=True)
    assert await later is True
    slots.release(lent=True)
    slots.unstick()
    slots.release(lent=False)
    assert (slots.held, slots.lent, slots.stuck, len(slots._waiters)) == (0, 0, 0, 0)


def test_a_lent_place_never_sticks_or_lends() -> None:
    # AUDIT-24 #3, in `_Slots` itself, whatever `run` asks.
    slots = _Slots(1)
    assert slots.stick(lent=True) is False
    assert slots.stuck == 0
    assert slots._grant(short=False) is False
    assert slots._grant(short=True) is None


def _served_within_bound(workers: int, ops: list[tuple[str, str]]) -> None:
    """Replay `ops` on `_Slots(workers)`, then drain; no caller is overtaken for more
    than `workers` slot grants by callers that arrived after it, nor, if it may be lent
    a place, for more than `_LENT_AGING` lent places per slot."""

    async def main() -> None:
        slots = _Slots(workers)
        arrived: list[tuple[asyncio.Task[bool], int]] = []  # (task, slot grants before it)
        grants: list[asyncio.Task[bool]] = []  # slot grants, in order; one per op at most
        served: dict[asyncio.Task[bool], int] = {}  # slot grants before it was served
        lent_before: dict[asyncio.Task[bool], int] = {}
        lent_served: dict[asyncio.Task[bool], int] = {}
        held: list[asyncio.Task[bool]] = []
        lent: list[asyncio.Task[bool]] = []
        lent_grants: list[asyncio.Task[bool]] = []
        kinds: dict[asyncio.Task[bool], str] = {}

        async def step() -> None:
            await asyncio.sleep(0)
            for task, _ in arrived:
                if task.done() and task not in served:
                    served[task] = len(grants)
                    lent_served[task] = len(lent_grants)
                    (lent if task.result() else held).append(task)
                    (lent_grants if task.result() else grants).append(task)

        for op, kind in ops:
            if op == "arrive":
                task = asyncio.create_task(slots.acquire(kind == "short", kind == "new"))
                kinds[task] = kind
                arrived.append((task, len(grants)))
                lent_before[task] = len(lent_grants)
            elif op == "release" and held:
                held.pop(0)
                slots.release(lent=False)
            elif op == "release_lent" and lent:
                lent.pop(0)
                slots.release(lent=True)
            elif op == "stick" and slots.stuck < slots.held:
                slots.stick(lent=False)
            elif op == "unstick" and slots.stuck:
                slots.unstick()
            await step()
        while slots.stuck:
            slots.unstick()
        while held or lent:
            if lent:
                lent.pop(0)
                slots.release(lent=True)
            else:
                held.pop(0)
                slots.release(lent=False)
            await step()
        assert len(served) == len(arrived)
        order = {task: i for i, (task, _) in enumerate(arrived)}
        for task, before in arrived:
            overtaken = sum(1 for g in grants[before : served[task]] if order[g] > order[task])
            assert overtaken <= workers, (order[task], overtaken)
            if kinds[task] != "hard":
                lent_by = lent_grants[lent_before[task] : lent_served[task]]
                overtaken = sum(1 for g in lent_by if order[g] > order[task])
                assert overtaken <= solver_mod._LENT_AGING * workers, (order[task], overtaken)

    loop = asyncio.new_event_loop()  # not `asyncio.run`, which unsets pytest-asyncio's loop
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


_OPS: Final = st.lists(
    st.tuples(
        st.sampled_from(["arrive", "arrive", "release", "release_lent", "stick", "unstick"]),
        st.sampled_from(["hard", "short", "new"]),
    ),
    max_size=60,
)


@settings(max_examples=200, deadline=None)
@given(workers=st.integers(1, 3), ops=_OPS)
def test_no_caller_is_overtaken_for_more_than_every_slot(
    workers: int, ops: list[tuple[str, str]]
) -> None:
    _served_within_bound(workers, ops)


# --- `SolverExecutor` scheduling ------------------------------------------------------


class _FakeClock:
    """`solver._clock` under test control: a check advances it to say how long it ran."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def ran(self, slices: float) -> Callable[[], None]:
        def check() -> None:
            self.now += slices * solver_mod._SLICE_S

        return check


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr(solver_mod, "_clock", fake)
    return fake


async def test_a_new_runs_first_check_is_served_before_hard_runs(
    executors: list[SolverExecutor], clock: _FakeClock
) -> None:
    # AUDIT-26 #1 without wall clocks: 1 slot, two runs whose checks are hard take turns
    # in it. A new run (no check yet) arrives behind a queued hard check and is served
    # before it; SG30 served it in arrival order, behind every hard caller.
    executor = _executor(executors, 1, 0)
    slots = executor._slots.setdefault(asyncio.get_running_loop(), _Slots(1))
    order: list[str] = []
    gates: dict[str, threading.Event] = {}

    def check(name: str, slices: float) -> None:
        order.append(name)
        assert gates.setdefault(name, threading.Event()).wait(_WAIT_S)
        clock.now += slices * solver_mod._SLICE_S

    async def hard(names: list[str]) -> None:
        for name in names:
            await executor.run(partial(check, name, solver_mod._HARD_SLICES))

    def end(name: str) -> None:
        gates.setdefault(name, threading.Event()).set()

    a = asyncio.create_task(hard(["a1", "a2", "a3"]))
    await _until(lambda: order == ["a1"])
    b = asyncio.create_task(hard(["b1", "b2"]))
    await _until(lambda: len(slots._waiters) == 1)
    end("a1")
    await _until(lambda: order == ["a1", "b1"] and len(slots._waiters) == 1)  # a2 queued
    newcomer = asyncio.create_task(executor.run(partial(check, "n", 0)))
    await _until(lambda: len(slots._waiters) == 2)
    for name, served in [("b1", 3), ("n", 4), ("a2", 5), ("b2", 6), ("a3", 6)]:
        end(name)
        await _until(lambda served=served: len(order) == served)  # type: ignore[misc]
    await asyncio.gather(a, b, newcomer)
    assert order == ["a1", "b1", "n", "a2", "b2", "a3"]


async def _result(run: Awaitable[tuple[str, float]]) -> str:
    return (await run)[0]


async def _hard_run(
    executor: SolverExecutor, clock: _FakeClock, call: _Held
) -> tuple[asyncio.Task[str], asyncio.Event]:
    """A task that makes a hard check, then `call` once its event is set."""
    go = asyncio.Event()

    async def run() -> str:
        await executor.run(clock.ran(solver_mod._HARD_SLICES))
        await go.wait()
        return await _result(executor.run(call))

    task = asyncio.create_task(run())
    await _until(lambda: task in executor._hard)
    return task, go


async def test_slow_checks_never_exceed_slow_workers(
    executors: list[SolverExecutor], clock: _FakeClock
) -> None:
    # The slow-worker cap (AUDIT-20 #5): 1 slot, 2 slow. Of four held Z3 calls, two go
    # slow, the third stays stuck in the slot and the fourth, from a hard run, waits.
    executor = _executor(executors, 1, 2)
    held = [_Held() for _ in range(4)]
    last, go = await _hard_run(executor, clock, held[3])
    tasks: list[asyncio.Task[str]] = []
    for h in held[:3]:
        tasks.append(asyncio.create_task(_result(executor.run(h))))
        assert await asyncio.to_thread(h.started.wait, _WAIT_S)
        await _until(lambda: executor._slow == min(len(tasks), 2))
    tasks.append(last)
    go.set()
    slots = executor._slots[asyncio.get_running_loop()]
    await _until(lambda: slots.stuck == 1 and len(slots._waiters) == 1)
    assert executor._slow == 2
    assert not held[3].started.is_set()
    assert len(executor._pool._threads) == 3
    held[0].gate.set()  # a slow check ends; the stuck one takes its place, the slot frees
    assert await asyncio.to_thread(held[3].started.wait, _WAIT_S)
    for h in held:
        h.gate.set()
    assert await asyncio.gather(*tasks) == ["held"] * 4
    assert (executor._slow, executor._pool._max_workers, slots.held, slots.stuck) == (0, 1, 0, 0)


async def test_a_short_caller_overtakes_hard_checks_above_the_slow_cap(
    executors: list[SolverExecutor], clock: _FakeClock
) -> None:
    # AUDIT-18 #4, AUDIT-26 #1 without wall clocks: no slow room, the slot is stuck and a
    # hard run waits. A caller whose last check was short runs at once on a lent place,
    # and so does a new one (stopped only if its check shows it is hard, AUDIT-30 #1).
    executor = _executor(executors, 1, 0)
    await executor.run(lambda: None)  # this task's last check was short
    hard, other = _Held(), _Held()
    other_task, go = await _hard_run(executor, clock, other)
    hard_task = asyncio.create_task(executor.run(hard))
    assert await asyncio.to_thread(hard.started.wait, _WAIT_S)
    slots = executor._slots[asyncio.get_running_loop()]
    await _until(lambda: slots.stuck == 1)
    go.set()
    await _until(lambda: len(slots._waiters) == 1)
    assert (await executor.run(lambda: "easy"))[0] == "easy"
    assert not other.started.is_set() and not hard.gate.is_set()

    async def new() -> bool:
        await executor.run(lambda: None)
        return other.started.is_set()

    assert await asyncio.create_task(new()) is False  # lent a place, as the short one
    hard.gate.set()
    other.gate.set()
    assert await other_task == "held"
    await hard_task
    assert (slots.held, slots.lent, slots.stuck, executor._pool._max_workers) == (0, 0, 0, 1)


class _HardUntilStopped:
    """A check that runs until its worker is killed; run again, it decides at once."""

    def __init__(self, name: str, order: list[str]) -> None:
        self.name = name
        self.order = order
        self.started = threading.Event()
        self._stop = threading.Event()

    def interrupt(self) -> None:
        self._stop.set()

    def __call__(self) -> str:
        self.order.append(self.name)
        if self._stop.is_set():
            return "decided"
        interrupt = solver_mod._worker.interrupt
        assert interrupt.attach(self)
        self.started.set()
        try:
            assert self._stop.wait(_WAIT_S)
        finally:
            interrupt.detach()
        return "stopped"


async def test_runs_that_start_together_do_not_hold_up_a_later_newcomer(
    executors: list[SolverExecutor], clock: _FakeClock
) -> None:
    # AUDIT-30 #1, AUDIT-32 #8: 1 slot, no slow room, its check stuck. Three runs start
    # together, all newcomers; the one lent the place shows it is hard and is stopped
    # and queued again as hard. An easy newcomer arriving after them is served next, and
    # every stopped run is decided once the slot frees.
    executor = _executor(executors, 1, 0)
    blocker = _Held()
    slot_task = asyncio.create_task(executor.run(blocker))
    assert await asyncio.to_thread(blocker.started.wait, _WAIT_S)
    slots = executor._slots[asyncio.get_running_loop()]
    await _until(lambda: slots.stuck == 1)
    order: list[str] = []
    runs = [_HardUntilStopped(f"r{i}", order) for i in range(3)]
    tasks = [asyncio.create_task(executor.run(r)) for r in runs]
    await _until(lambda: runs[0].started.is_set() and len(slots._waiters) == 2)
    easy = asyncio.create_task(executor.run(lambda: order.append("easy")))
    await _until(lambda: len(slots._waiters) == 3)
    clock.now += solver_mod._HARD_SLICES * solver_mod._SLICE_S
    await easy
    assert order == ["r0", "easy"]
    for run in (runs[2], runs[1]):  # the latest newcomer first
        await _until(run.started.is_set)
        clock.now += solver_mod._HARD_SLICES * solver_mod._SLICE_S
    await _until(lambda: len(slots._waiters) == 3)
    assert all(not w.new and not w.short for w in slots._waiters)
    blocker.gate.set()
    assert [r for r, _ in await asyncio.gather(*tasks)] == ["decided"] * 3
    await slot_task
    assert order == ["r0", "easy", "r2", "r1", "r0", "r2", "r1"]
    assert all(t in executor._short for t in tasks)  # each decided at once when run again
    assert (slots.held, slots.lent, slots.stuck, executor._pool._max_workers) == (0, 0, 0, 1)


async def test_a_short_caller_takes_a_lent_place_from_a_newcomer_after_a_slice(
    executors: list[SolverExecutor], clock: _FakeClock
) -> None:
    # AUDIT-18 #4 with newcomers lent places: a newcomer's check that has run a slice gives
    # its lent place to a waiting short caller and is queued again for a slot only, so
    # it cannot take the place back between the short caller's checks.
    executor = _executor(executors, 1, 0)
    await executor.run(lambda: None)  # this task's last check was short
    blocker = _Held()
    slot_task = asyncio.create_task(executor.run(blocker))
    assert await asyncio.to_thread(blocker.started.wait, _WAIT_S)
    slots = executor._slots[asyncio.get_running_loop()]
    await _until(lambda: slots.stuck == 1)
    order: list[str] = []
    run = _HardUntilStopped("new", order)
    newcomer = asyncio.create_task(executor.run(run))
    assert await asyncio.to_thread(run.started.wait, _WAIT_S)
    clock.now += solver_mod._SLICE_S
    for _ in range(2):
        await executor.run(lambda: order.append("short"))
    await _until(lambda: len(slots._waiters) == 1)
    assert newcomer not in executor._hard  # stopped after a slice: not counted as hard
    blocker.gate.set()
    await slot_task
    assert (await newcomer)[0] == "decided"
    assert order == ["new", "short", "short", "new"]
    assert (slots.held, slots.lent, slots.stuck) == (0, 0, 0)


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


@pytest.mark.parametrize(
    ("slices", "short"),
    [
        (0, True),
        (2, True),
        (solver_mod._HARD_SLICES - 0.01, True),
        (solver_mod._HARD_SLICES, False),
        (40, False),
    ],
)
async def test_a_run_is_hard_by_its_checks_run_time(
    executors: list[SolverExecutor], clock: _FakeClock, slices: float, short: bool
) -> None:
    # AUDIT-24 #2: these checks return at once, so the loop never wakes for a slice;
    # only the run time decides.
    executor = _executor(executors, 1, 0)
    me = asyncio.current_task()
    _, ms = await executor.run(clock.ran(slices))
    assert ms == pytest.approx(slices * solver_mod._SLICE_S * 1000)
    assert (me in executor._short) is short
    await executor.run(clock.ran(0))
    assert me in executor._short  # one short check restores the priority


async def test_loop_wake_ups_do_not_make_a_run_hard(
    executors: list[SolverExecutor], clock: _FakeClock
) -> None:
    # A check the loop waits on for many slices, but whose own clock says it ran for
    # none (an easy check queued behind load), stays short.
    executor = _executor(executors, 1, 0)
    held = _Held()
    wakes: list[int] = []
    real_to_slow = executor._to_slow

    def to_slow(interrupt: solver_mod._Interrupt) -> bool:
        wakes.append(1)
        return real_to_slow(interrupt)

    executor._to_slow = to_slow  # type: ignore[method-assign]
    task = asyncio.create_task(executor.run(held))
    await _until(lambda: len(wakes) > 2 * solver_mod._HARD_SLICES)
    held.gate.set()
    await task
    assert task in executor._short


async def test_a_check_that_ran_long_on_a_busy_loop_is_hard(
    executors: list[SolverExecutor], clock: _FakeClock
) -> None:
    # The converse: the loop never wakes while the check runs (it is blocked), yet the
    # check ran for many slices of its own time.
    executor = _executor(executors, 1, 0)
    started, gate = threading.Event(), threading.Event()

    def long_check() -> None:
        started.set()
        assert gate.wait(_WAIT_S)
        clock.now += 10 * solver_mod._SLICE_S

    task = asyncio.create_task(executor.run(long_check))
    assert await asyncio.to_thread(started.wait, _WAIT_S)
    gate.set()
    _until_sync(lambda: clock.now > 0)  # the loop is blocked until the check has ended
    await task
    assert task not in executor._short


async def test_a_cancelled_check_counts_the_time_it_ran(
    executors: list[SolverExecutor], clock: _FakeClock
) -> None:
    executor = _executor(executors, 1, 0)
    held = _Held()
    task = asyncio.create_task(executor.run(held))
    assert await asyncio.to_thread(held.started.wait, _WAIT_S)
    clock.now += solver_mod._HARD_SLICES * solver_mod._SLICE_S
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    held.gate.set()
    assert task not in executor._short


def test_run_time_is_zero_before_a_call_starts_and_fixed_once_it_ends(clock: _FakeClock) -> None:
    interrupt = solver_mod._Interrupt()
    clock.now = 5.0
    assert interrupt.ran_s() == 0.0
    interrupt.started = 5.0
    clock.now = 6.5
    assert interrupt.ran_s() == 1.5
    interrupt.ended = 7.0
    clock.now = 100.0
    assert interrupt.ran_s() == 2.0


async def test_a_caller_whose_check_ran_long_is_not_lent_a_place(
    executors: list[SolverExecutor], clock: _FakeClock
) -> None:
    executor = _executor(executors, 1, 0)
    await executor.run(clock.ran(solver_mod._HARD_SLICES))  # this task is now a hard run
    hard, mine = _Held(), _Held()
    hard_task = asyncio.create_task(executor.run(hard))
    assert await asyncio.to_thread(hard.started.wait, _WAIT_S)
    slots = executor._slots[asyncio.get_running_loop()]
    await _until(lambda: slots.stuck == 1)
    seen: list[tuple[int, bool]] = []

    async def drive() -> None:
        await _until(lambda: len(slots._waiters) == 1)
        seen.append((slots.lent, mine.started.is_set()))
        hard.gate.set()
        mine.gate.set()

    driver = asyncio.create_task(drive())
    assert (await executor.run(mine))[0] == "held"
    await asyncio.gather(driver, hard_task)
    assert seen == [(0, False)]


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


async def test_a_cancelled_slow_check_keeps_its_place_until_it_ends(
    executors: list[SolverExecutor],
) -> None:
    # AUDIT-28 #2: the held call ignores the interrupt, as a worker whose kill is still
    # in progress does. Its caller is gone, but the slow place frees only when it ends.
    executor = _executor(executors, 1, 1)
    held = _Held()
    task = asyncio.create_task(executor.run(held))
    assert await asyncio.to_thread(held.started.wait, _WAIT_S)
    await _until(lambda: executor._slow == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert executor._slow == 1
    held.gate.set()
    await _until(lambda: executor._slow == 0)
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
    out = Z3Solver(5.0)._check_here([SmtFormula(_DECLS, "p")], SmtFormula(_DECLS, "p"))
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
    out = Z3Solver(1.0)._check_here([SmtFormula(_DECLS, "p")], SmtFormula(_DECLS, "p"))
    assert len(calls) == checks
    if checks == 1:
        assert (out.reason, out.details) == (
            "timeout",
            "too little time left for the entailment check",
        )
    else:
        assert out.verdict == "VALID"
