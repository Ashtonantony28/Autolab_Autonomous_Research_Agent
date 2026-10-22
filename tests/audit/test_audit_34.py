"""AUDIT-34 repros: performance under load, round 9 (SG34).

Each test asserts the correct behaviour and is a strict xfail until its finding
is fixed. The scheduling repros order their events with events and a fake clock,
as `tests/unit/verification/test_solver_scheduling.py` does; the load repros are
calibrated against the machine's own measurements (the Z3 time the same load
spends), never absolute seconds. Measurements: `uv run python -m scripts.audit_load_9`.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from functools import partial
from typing import Any, Final

import pytest
import z3

from autolab.verification import solver as solver_mod
from autolab.verification import solver_workers
from autolab.verification.solver import SolverExecutor, SolverOutcome, Z3Solver
from scripts.audit_load_7 import EASY
from scripts.audit_load_9 import UNDECIDABLE, fork_report, fresh_task_load, metered

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


class _UntilStopped:
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


class _FakeClock:
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


# --- #1: a newcomer that gives way goes to the back of the hard queue ----------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-34 #1")
async def test_a_newcomer_that_gives_way_to_a_short_caller_is_served_before_hard_runs(
    executors: list[SolverExecutor], clock: _FakeClock
) -> None:
    # 1 slot, no slow room, its check stuck; two known-hard runs queue for the slot. A
    # newcomer is lent the place, runs one slice (not hard), and gives it to a waiting
    # short caller. It has not shown it is hard, so it should still take the slot before
    # the hard runs (SG32's guarantee, AUDIT-26 #1). Today it is queued again at the hard
    # rank, behind every hard caller: with N hard runs queued it waits N hard checks.
    executor = SolverExecutor(1, 0)
    executors.append(executor)
    order: list[str] = []
    go_hard, go_short = asyncio.Event(), asyncio.Event()

    async def hard(name: str) -> None:
        await executor.run(clock.ran(solver_mod._HARD_SLICES))  # this run is now hard
        await go_hard.wait()
        await executor.run(lambda: order.append(name))

    async def short() -> None:
        await executor.run(lambda: None)  # this run's last check was short
        await go_short.wait()
        await executor.run(lambda: order.append("short"))

    runs = [asyncio.create_task(hard("h1")), asyncio.create_task(hard("h2"))]
    runs.append(asyncio.create_task(short()))
    await _until(lambda: len(executor._hard) == 2 and len(executor._short) == 1)
    blocker = _Held()
    slot_task = asyncio.create_task(executor.run(blocker))
    assert await asyncio.to_thread(blocker.started.wait, _WAIT_S)
    slots = executor._slots[asyncio.get_running_loop()]
    await _until(lambda: slots.stuck == 1)
    go_hard.set()
    await _until(lambda: len(slots._waiters) == 2)
    newcomer = _UntilStopped("new", order)
    new_task = asyncio.create_task(executor.run(newcomer))
    assert await asyncio.to_thread(newcomer.started.wait, _WAIT_S)
    go_short.set()
    await _until(lambda: len(slots._waiters) == 3)
    clock.now += 2 * solver_mod._SLICE_S  # two slices, fewer than `_HARD_SLICES`: it gives way
    await _until(lambda: "short" in order)
    blocker.gate.set()
    await asyncio.gather(slot_task, new_task, *runs)
    assert order[:3] == ["new", "short", "new"], order  # today: new, short, h1, h2, new


@pytest.mark.xfail(strict=True, reason="AUDIT-34 #1")
async def test_a_newcomer_is_not_stopped_while_its_worker_starts(
    executors: list[SolverExecutor], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The stop rule reads the check's run time while it runs, and that includes waiting
    # for its worker to start, which SG32 excludes from hardness and `solver_ms`. SG34's
    # own kills empty the idle pool (6.7 worker starts/s under `audit_load_9 waste`), so
    # under load a 1 ms check can be stopped for its boot and queued behind hard runs.
    # Here the boot takes 3 slices (the measured worst was 2.5) while a short caller waits.
    executor = SolverExecutor(1, 0)
    executors.append(executor)
    real_ready = solver_workers.Worker.ready
    slow_boot = [True]

    def ready(self: solver_workers.Worker) -> bool:
        if slow_boot[0]:
            slow_boot[0] = False
            time.sleep(3 * solver_mod._SLICE_S)
        return real_ready(self)

    monkeypatch.setattr(solver_workers.Worker, "ready", ready)
    go_short = asyncio.Event()

    async def short() -> None:
        await executor.run(lambda: None)
        await go_short.wait()
        await executor.run(lambda: None)

    short_task = asyncio.create_task(short())
    await _until(lambda: len(executor._short) == 1)
    blocker = _Held()
    slot_task = asyncio.create_task(executor.run(blocker))
    assert await asyncio.to_thread(blocker.started.wait, _WAIT_S)
    slots = executor._slots[asyncio.get_running_loop()]
    await _until(lambda: slots.stuck == 1)
    calls = [0]
    easy = partial(Z3Solver().check_entailment, *EASY)

    def counted() -> SolverOutcome:
        calls[0] += 1
        return easy()

    new_task = asyncio.create_task(executor.run(counted))
    await _until(lambda: slots.lent == 1)
    go_short.set()
    try:
        done, _ = await asyncio.wait((new_task,), timeout=20 * solver_mod._SLICE_S)
    finally:
        blocker.gate.set()
        await asyncio.gather(slot_task, short_task, new_task)
    assert done and calls[0] == 1, f"started {calls[0]} times; done before the slot freed: {done}"


# --- #2: lent places run hard checks beside every slot and slow place ------------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-34 #2")
async def test_z3_checks_never_outnumber_the_slots_and_slow_places() -> None:
    # A 1-CPU host's defaults: 1 slot, no slow place. AUDIT-18 #5: slots and slow checks
    # together do not outnumber the CPUs. Three hard checks in new tasks: today one runs
    # in the slot and another on a lent place for `_HARD_SLICES` beside it (peak 2), and
    # the lent one is killed and run again.
    executor = SolverExecutor(1, 0)
    check = partial(Z3Solver(0.5).check_entailment, *UNDECIDABLE)
    try:
        with metered() as meter:
            await asyncio.gather(*(executor.run(check) for _ in range(3)))
    finally:
        executor.shutdown()
    assert meter.peak <= 1, f"{meter.peak} Z3 checks at once, {meter.restarts} worker restarts"


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-34 #2")
async def test_runs_that_check_in_fresh_tasks_waste_little_z3_time() -> None:
    # LangGraph's shape: every node is a new task, so every run's first check is a
    # newcomer's. 8 hard runs, 1 check per task, 2 slots + 2 slow places, timeout 0.4 s.
    # Calibrated against the load's own Z3 time: today about a fifth of it is spent on
    # checks that are killed and run again (pre-SG34: none), and Z3 runs 5+ at once.
    out = await fresh_task_load(8, 0.4, 1, 4.0)
    z3_s = out["mean"] * 4.0
    share = out["wasted_s"] / z3_s
    assert share <= 0.05, (
        f"{out['wasted_s']:.1f} of {z3_s:.1f} Z3 s wasted on {out['reruns']:.0f} re-runs, "
        f"{out['restarts']:.0f} restarts, {out['mean']:.2f} checks at once"
    )


# --- #3: the retuned tests, at their old strength --------------------------------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-34 #3")
async def test_slow_checks_are_bounded_by_slow_workers_as_before_sg34() -> None:
    # `test_slow_checks_are_bounded_by_slow_workers` before SG34. Its new `peak <= 3` with
    # three callers, and `threads <= 3`, cannot fail; `calls in (3, 4)` accepts the re-run.
    executor = SolverExecutor(1, slow_workers=1)
    lock = threading.Lock()
    running, peak, calls = 0, 0, 0
    check = partial(Z3Solver(0.3).check_entailment, *UNDECIDABLE)

    def hard() -> SolverOutcome:
        nonlocal running, peak, calls
        with lock:
            running += 1
            calls += 1
            peak = max(peak, running)
        try:
            return check()
        finally:
            with lock:
                running -= 1

    try:
        outs = await asyncio.gather(*(executor.run(hard) for _ in range(3)))
    finally:
        executor.shutdown()
    assert [o.reason for o, _ in outs] == ["timeout"] * 3
    assert (peak, calls) == (2, 3)


# --- #4: a forked child starts cold ----------------------------------------------------------


@pytest.mark.perf
@pytest.mark.xfail(strict=True, reason="AUDIT-34 #4")
def test_a_forked_childs_first_check_costs_about_a_warm_one() -> None:
    # A prefork server: `Verifier()` at import, then fork. SG34's child adopts the pool
    # empty and starts nothing until its first check, which then starts a forkserver and
    # a worker (0.5-1.0 s against 6-21 ms warm), recorded nowhere (AUDIT-30 #4).
    # The child idles 1 s first, as a prefork worker does before its first request.
    report: dict[str, Any] = fork_report(1, 3, idle_s=1.0)
    walls = report["children"][0]["walls_ms"]
    assert walls[0] <= 5 * max(walls[1:]), f"first check {walls[0]} ms, then {walls[1:]} ms"
