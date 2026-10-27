"""AUDIT-38 repros: performance under load, round 10 (SG35).

Each test asserts the correct behaviour and is a strict xfail until its finding is
fixed. The scheduling repro orders its events with events and a fake clock, as
`tests/audit/test_audit_34.py` does; the load repro is a share of the run's own samples,
never absolute seconds. Measurements: `uv run python -m scripts.audit_load_10`.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from functools import partial
from typing import Final

import pytest
import z3

from autolab.verification import solver as solver_mod
from autolab.verification.solver import SolverExecutor, Z3Solver
from scripts.audit_load_9 import UNDECIDABLE
from scripts.audit_load_10 import RunningSampler, fork_in_start_window

_WAIT_S: Final = 10.0
"""Bounds a failing wait on an event; a passing run never waits this long."""


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


class _UntilPaused:
    """A check that runs until its worker is paused; resumed, it decides at once. `order`
    records each start and resume (SG36 pauses a check that gives way, not stops it)."""

    def __init__(self, name: str, order: list[str]) -> None:
        self.name = name
        self.order = order
        self.started = threading.Event()
        self._paused = False
        self._end = threading.Event()

    def interrupt(self) -> None:
        self._end.set()

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        if self._paused:
            self._paused = False
            self.order.append(self.name)
            self._end.set()

    def __call__(self) -> str:
        self.order.append(self.name)
        interrupt = solver_mod._worker.interrupt
        assert interrupt.attach(self)
        self.started.set()
        try:
            assert self._end.wait(_WAIT_S)
        finally:
            interrupt.detach()
        return "decided"


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr(solver_mod, "_clock", fake)
    return fake


@pytest.fixture
def executors() -> Iterator[list[SolverExecutor]]:
    made: list[SolverExecutor] = []
    yield made
    for executor in made:
        executor.shutdown()


async def _until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + _WAIT_S
    while not condition():
        assert time.monotonic() < deadline, "condition never held"
        await asyncio.sleep(0.005)


# --- #1: a newcomer that gave way waits for a slot while its lent place idles --------------


async def test_a_newcomer_that_gave_way_is_served_before_the_slot_frees(
    executors: list[SolverExecutor], clock: _FakeClock
) -> None:
    # 1 slot, no slow room, its check stuck (a hard check: it holds the slot up to its
    # whole timeout). A newcomer is lent the place, runs two slices (not hard) and gives
    # it to a waiting short caller, whose check ends at once. SG35 queues the newcomer
    # again for a slot only, so the lent place sits idle while the newcomer waits for the
    # stuck check to end: measured 3.1-5.1 s at the default 5 s timeout with no hard run
    # ahead, 8.1-8.2 s behind 4 aged ones (`audit_load_10 requeue`), as before SG34.
    executor = SolverExecutor(1, 0)
    executors.append(executor)
    order: list[str] = []
    go_short = asyncio.Event()

    async def short() -> None:
        await executor.run(lambda: None)  # this run's last check was short
        await go_short.wait()
        await executor.run(lambda: order.append("short"))

    short_task = asyncio.create_task(short())
    await _until(lambda: len(executor._short) == 1)
    blocker = _Held()
    slot_task = asyncio.create_task(executor.run(blocker))
    assert await asyncio.to_thread(blocker.started.wait, _WAIT_S)
    slots = executor._slots[asyncio.get_running_loop()]
    await _until(lambda: slots.stuck == 1)
    newcomer = _UntilPaused("new", order)
    new_task = asyncio.create_task(executor.run(newcomer))
    assert await asyncio.to_thread(newcomer.started.wait, _WAIT_S)
    go_short.set()
    await _until(lambda: len(slots._waiters) == 1)
    clock.now += 2 * solver_mod._SLICE_S  # two slices, fewer than `_HARD_SLICES`: it gives way
    await _until(lambda: "short" in order)
    try:
        done, _ = await asyncio.wait((new_task,), timeout=20 * solver_mod._SLICE_S)
        state = (slots.held, slots.lent, slots.stuck)
    finally:
        blocker.gate.set()
        await asyncio.gather(slot_task, short_task, new_task)
    assert done, f"still waiting for the slot (held, lent, stuck = {state}); order {order}"


# --- #2: Z3 processes that run at once outnumber the places ----------------------------------


@pytest.mark.perf
async def test_z3_processes_running_at_once_never_outnumber_the_places() -> None:
    # A 1-CPU host's defaults: 1 slot, no slow place. One hard check holds the slot; three
    # hard checks arrive in new tasks while it runs. Counted from /proc (state R), not as
    # calls in flight, so a design that pauses the lending slot's worker (SIGSTOP) while
    # its place is lent passes, as one that never lends does. Today each newcomer runs
    # `_HARD_SLICES` beside the slot's check: 2 running in about a third of the samples.
    executor = SolverExecutor(1, 0)
    slot_check = partial(Z3Solver(1.5).check_entailment, *UNDECIDABLE)
    hard = partial(Z3Solver(0.5).check_entailment, *UNDECIDABLE)
    try:
        with RunningSampler() as sampler:
            slot_task = asyncio.create_task(executor.run(slot_check))
            await asyncio.sleep(2 * solver_mod._SLICE_S)  # the slot's check is stuck
            newcomers = [asyncio.create_task(executor.run(hard)) for _ in range(3)]
            await slot_task
        await asyncio.gather(*newcomers)
    finally:
        executor.shutdown()
    share = sampler.share_over(1)
    assert share <= 0.05, f"more than 1 Z3 process running in {share:.0%} of samples"


# --- #3: a fork between a worker's start and its registration --------------------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-38 #3")
def test_a_child_forked_while_a_worker_starts_spares_it() -> None:
    # SG35's `_adopt` drops the owner's `_live` workers from multiprocessing's children.
    # A worker whose `Process.start()` has returned (so it is in `_children`) but that
    # `Workers._start` has not yet added to `_live` stays in the child's `_children`, and
    # the child's normal exit SIGTERMs it. The window is widened here with a sleep.
    alive, code = fork_in_start_window()
    assert alive, f"the owner's worker exited with {code}"
