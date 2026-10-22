"""AUDIT-36: test coverage and test quality, round 9 (SG34).

Each finding in `AUDIT-36.md` that is about a mutant the suite lets through has
two tests here:
- a plain test that kills the mutant and passes today (the missing guard);
- a strict xfail, named for the finding, that applies the mutant with
  `monkeypatch` (`_mutant` from AUDIT-24 recompiles one function with one
  exact-match edit) and runs the same check.
A mutant of code that only runs in a forked child (the at-fork hooks) is applied
inside the fresh interpreter that forks, by swapping the hook's code object, as
the registered hook is the original function. Waits are bounded by events or by
a pipe that never answers; timing assertions compare measurements taken in the
same test on the same machine, never absolute seconds.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import types
from pathlib import Path
from typing import Any, Final

import pytest

from autolab.verification import solver as solver_mod
from autolab.verification.solver import SolverExecutor
from tests.audit.test_audit_24 import _Held, _mutant, _until

_ROOT: Final = Path(__file__).resolve().parents[2]
_WAIT_S: Final = 10.0
"""Bounds a wait on an event or a pipe; a passing run never waits this long."""

fork_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="os.fork")


# --------------------------------------------------------------------------
# Forked children. `_FORK` runs in a fresh interpreter: it applies a mutant (by
# name) to the at-fork hooks, starts the shared pool, forks in the named way and
# prints one JSON line about the child and the parent's own workers.
# --------------------------------------------------------------------------

_FORK: Final = """
import inspect, json, os, select, signal, sys, textwrap, threading
from multiprocessing import forkserver
from autolab.verification import solver as solver_mod, solver_workers
from autolab.verification.solver import SmtFormula, Z3Solver, default_executor

def mutate(owner, name, old, new):
    func = inspect.getattr_static(owner, name)
    source = textwrap.dedent(inspect.getsource(func))
    assert source.count(old) == 1, (name, old)
    ns = dict(func.__globals__)
    exec(source.replace(old, new), ns)
    func.__code__ = ns[name].__code__  # the hook registered at import is this object

MUTANTS = {
    "none": None,
    "f02": (solver_mod, "_after_fork_in_child",
            "_workers_lock, _default_lock = threading.Lock(), threading.Lock()", "pass"),
    "k06": (solver_workers.Workers, "_adopt",
            "self._lock = threading.Lock()\\n", ""),
    "k11": (solver_workers, "_forget_forkserver",
            "os.close(server._forkserver_alive_fd)", "pass"),
    "k14": (solver_workers, "_forget_forkserver",
            "server._lock = threading.Lock()", "pass"),
    "k07": (solver_workers.Workers, "_adopt", "self._pid = os.getpid()", "pass"),
}
D = "(declare-const p Bool)"

def check():
    return Z3Solver().check_entailment([SmtFormula(D, "p")], SmtFormula(D, "p")).verdict

if __name__ == "__main__":
    how, mutant = sys.argv[1], sys.argv[2]
    if MUTANTS[mutant]:
        mutate(*MUTANTS[mutant])
    default_executor()
    first = check()
    pool = solver_mod._workers()
    pool.prestart(2)
    idle = list(pool._idle)
    alive_fd = forkserver._forkserver._forkserver_alive_fd
    held = {
        "default_lock": lambda: solver_mod._default_lock,
        "workers_lock": lambda: solver_mod._workers_lock,
        "pool_lock": lambda: pool._lock,
        "forkserver_lock": lambda: forkserver._forkserver._lock,
    }.get(how)
    read, write = os.pipe()
    if held:  # another thread holds it, as one starting a worker or the forkserver does
        taken, done = threading.Event(), threading.Event()
        def hold():
            with held():
                taken.set()
                done.wait()
        holder = threading.Thread(target=hold)
        holder.start()
        taken.wait()
        pid = os.fork()
        if pid:
            done.set()
            holder.join()
    else:
        pid = os.fork()
    if pid == 0:
        os.close(read)
        if how == "alive_fd":
            try:
                os.fstat(alive_fd)
                got = "open"
            except OSError:
                got = "closed"
        elif how == "reuse":  # worker starts in the child over three checks
            started = []
            start = solver_workers.Workers._start
            def counting(self):
                started.append(1)
                return start(self)
            solver_workers.Workers._start = counting
            got = " ".join(check() for _ in range(3)) + f" {len(started)}"
        else:
            if how == "default_lock":
                default_executor()
            got = check()
        os.write(write, got.encode())
        if how == "exit":
            sys.exit(0)  # a normal exit: atexit handlers run, as in a prefork server's worker
        os._exit(0)
    os.close(write)
    ready, _, _ = select.select([read], [], [], float(sys.argv[3]))
    child = os.read(read, 100).decode() if ready else "hung"
    if not ready:
        os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    print(json.dumps({"first": first, "child": child,
                      "parents_idle_alive": [w.alive() for w in idle], "after": check()}))
"""


def _forked(how: str, mutant: str = "none") -> dict[str, object]:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    done = subprocess.run(
        [sys.executable, "-c", _FORK, how, mutant, str(_WAIT_S)],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=4 * _WAIT_S,
        check=True,
    )
    got: dict[str, object] = json.loads(done.stdout.strip().splitlines()[-1])
    assert got["first"] == got["after"] == "VALID", got
    return got


# --------------------------------------------------------------------------
# #1 (medium): a forked child that exits normally kills the parent's workers.
# SG34's fork test (`test_a_forked_child_starts_its_own_workers_and_leaves_the_
# parents_alone`) and every AUDIT-29/30 fork repro end the child with `os._exit`,
# which skips atexit. On a normal exit (`sys.exit`, or the end of a prefork
# server's worker) multiprocessing's own atexit hook still lists the parent's
# worker processes as this process's daemon children: it sends each SIGTERM, then
# fails "can only join a child process". Every idle worker of the parent dies
# (exit code -15); a check running there at the time would lose its worker.
# --------------------------------------------------------------------------


@fork_only
def test_a_forked_child_that_ends_without_exiting_hard_never_kills_the_parents_workers() -> None:
    assert _forked("_exit")["parents_idle_alive"] == [True] * 5  # control: skips atexit


@fork_only
@pytest.mark.xfail(strict=True, reason="AUDIT-36 #1")
def test_a_forked_child_that_exits_normally_never_kills_the_parents_workers() -> None:
    assert _forked("exit")["parents_idle_alive"] == [True] * 5


# --------------------------------------------------------------------------
# #2 (medium): the at-fork hooks run only in a forked child, so coverage never
# sees them (solver.py 903-905, solver_workers.py 283-294) and nothing tests the
# locks they replace. A lock another thread holds at the fork (the prestart
# thread starting a worker, or the forkserver while it boots) stays held in the
# child for ever unless the hook replaces it. Dropping any one replacement (F02,
# K06, K14) passes the whole suite, and the child hangs on its first check. So
# does keeping the child's copy of the parent forkserver's alive pipe (K11),
# which keeps the parent's forkserver running for as long as the child does.
# --------------------------------------------------------------------------

_HELD_AT_FORK: Final = [
    ("default_lock", "f02"),
    ("workers_lock", "f02"),
    ("pool_lock", "k06"),
    ("forkserver_lock", "k14"),
]


@fork_only
@pytest.mark.parametrize("held", [h for h, _ in _HELD_AT_FORK])
def test_a_lock_held_at_the_fork_is_free_in_the_child(held: str) -> None:
    assert _forked(held)["child"] == "VALID"


@fork_only
@pytest.mark.xfail(strict=True, reason="AUDIT-36 #2")
@pytest.mark.parametrize(("held", "mutant"), _HELD_AT_FORK)
def test_a_lock_held_at_the_fork_is_free_in_the_child_under(held: str, mutant: str) -> None:
    assert _forked(held, mutant)["child"] == "VALID"


@fork_only
def test_the_child_closes_its_copy_of_the_forkservers_alive_pipe() -> None:
    assert _forked("alive_fd")["child"] == "closed"


@fork_only
@pytest.mark.xfail(strict=True, reason="AUDIT-36 #2")
def test_the_child_closes_its_copy_of_the_forkservers_alive_pipe_under_k11() -> None:
    assert _forked("alive_fd", "k11")["child"] == "closed"


# --------------------------------------------------------------------------
# #3 (low): lent-place aging is read, not pinned (L02, L03, R06). Both SG34
# aging tests (`test_a_newcomer_overtaken_for_every_lent_place_twice_is_lent_
# the_next` and the property test) take their bound from `_LENT_AGING`, so with
# the constant at 3, or at 1000 (no lent aging at all), everything passes. The
# one direct test uses one slot, so aging by `_LENT_AGING` places in all, not per
# slot (R06), passes too. The value is SG34's (2 places per slot, `solver.py`
# and the commit), hard-coded here, on 1 and 2 slots.
# --------------------------------------------------------------------------


async def _lent_overtakes(workers: int) -> int:
    """How many later newcomers are lent a place before the first queued newcomer."""
    slots = solver_mod._Slots(workers)
    for _ in range(workers):
        assert await slots.acquire(False) is False  # the slots; one check sticks
    first = asyncio.create_task(slots.acquire(False, True))
    await asyncio.sleep(0)
    latest = asyncio.create_task(slots.acquire(False, True))
    await asyncio.sleep(0)
    assert slots.stick(lent=False)
    await asyncio.sleep(0)
    overtakes = 0
    while not first.done():
        assert latest.done() and overtakes < 10
        overtakes += 1
        latest = asyncio.create_task(slots.acquire(False, True))
        await asyncio.sleep(0)
        slots.release(lent=True)
        await asyncio.sleep(0)
    slots.release(lent=True)  # first's place: the latest takes it
    assert await latest is True
    slots.release(lent=True)
    slots.unstick()
    for _ in range(workers):
        slots.release(lent=False)
    assert (slots.held, slots.lent, slots.stuck, len(slots._waiters)) == (0, 0, 0, 0)
    return overtakes


async def _lent_overtakes_on_1_and_2_slots() -> list[int]:
    return [await _lent_overtakes(1), await _lent_overtakes(2)]


async def test_a_newcomer_is_overtaken_for_two_lent_places_per_slot_at_most() -> None:
    assert await _lent_overtakes_on_1_and_2_slots() == [2, 4]


@pytest.mark.xfail(strict=True, reason="AUDIT-36 #3")
@pytest.mark.parametrize("aging", [3, 1000])
async def test_a_newcomer_is_overtaken_for_two_lent_places_per_slot_at_most_under(
    monkeypatch: pytest.MonkeyPatch, aging: int
) -> None:
    monkeypatch.setattr(solver_mod, "_LENT_AGING", aging)
    assert await _lent_overtakes_on_1_and_2_slots() == [2, 4]


@pytest.mark.xfail(strict=True, reason="AUDIT-36 #3")
async def test_a_newcomer_is_overtaken_for_two_lent_places_per_slot_at_most_under_r06(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(
        monkeypatch,
        solver_mod._Slots,
        "_lent_rank",
        "entry.lent_past >= _LENT_AGING * self.workers",
        "entry.lent_past >= _LENT_AGING",
    )
    assert await _lent_overtakes_on_1_and_2_slots() == [2, 4]


# --------------------------------------------------------------------------
# #5 (low): short callers before newcomers on a lent place is unpinned (R03).
# ADR 0009's SG34 amendment: "A stuck slot now lends to short callers first,
# then to newcomers". Ranking newcomers first passes the suite: no SG34 test
# has a short caller and a newcomer waiting when a place is lent. Here both
# wait, in both arrival orders, when the slot sticks.
# --------------------------------------------------------------------------


async def _lent_first(first: str) -> str:
    slots = solver_mod._Slots(1)
    assert await slots.acquire(False) is False
    kinds = {"short": (True, False), "new": (False, True)}
    tasks = {}
    for kind in [first, *(k for k in kinds if k != first)]:
        tasks[kind] = asyncio.create_task(slots.acquire(*kinds[kind]))
        await asyncio.sleep(0)
    assert slots.stick(lent=False)
    await asyncio.sleep(0)
    (lent,) = [k for k, t in tasks.items() if t.done()]
    assert tasks[lent].result() is True
    slots.release(lent=True)  # the other is lent it next
    slots.release(lent=True)
    slots.unstick()
    slots.release(lent=False)
    await asyncio.gather(*tasks.values())
    assert (slots.held, slots.lent, slots.stuck, len(slots._waiters)) == (0, 0, 0, 0)
    return lent


async def test_a_short_caller_is_lent_a_place_before_a_newcomer() -> None:
    assert [await _lent_first("short"), await _lent_first("new")] == ["short", "short"]


@pytest.mark.xfail(strict=True, reason="AUDIT-36 #5")
async def test_a_short_caller_is_lent_a_place_before_a_newcomer_under_r03(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(
        monkeypatch,
        solver_mod._Slots,
        "_lent_rank",
        "(1, 0) if entry.short else (2, -entry.arrival)",
        "(2, 0) if entry.short else (1, -entry.arrival)",
    )
    assert [await _lent_first("short"), await _lent_first("new")] == ["short", "short"]


# --------------------------------------------------------------------------
# #4 (low): a newcomer stopped while the executor shuts down (P04). `run` keeps
# a stopped check's result when `_closed` is set: its caller is in flight, and
# `shutdown` promises it a result, not RuntimeError (AUDIT-6 #7). Without the
# `or self._closed` the stopped newcomer queues again and gets "cannot schedule
# new futures after shutdown"; nothing tests the branch.
# --------------------------------------------------------------------------


class _StoppedAsShutdownStarts:
    """A newcomer's check on a lent place: stopped as hard, while shutdown begins."""

    def __init__(self, executor: SolverExecutor) -> None:
        self.executor = executor
        self.started = threading.Event()
        self._stop = threading.Event()
        self.shutting: threading.Thread | None = None

    def interrupt(self) -> None:
        self._stop.set()

    def __call__(self) -> str:
        interrupt: solver_mod._Interrupt = solver_mod._worker.interrupt
        assert interrupt.attach(self)
        self.started.set()
        try:
            assert self._stop.wait(_WAIT_S)
            self.shutting = threading.Thread(target=self.executor.shutdown)
            self.shutting.start()
            deadline = time.monotonic() + _WAIT_S
            while not self.executor._closed:
                assert time.monotonic() < deadline
                time.sleep(0.001)
        finally:
            interrupt.detach()
        return "stopped"


async def _stopped_newcomer_at_shutdown(monkeypatch: pytest.MonkeyPatch) -> str:
    now = [0.0]
    monkeypatch.setattr(solver_mod, "_clock", lambda: now[0])
    executor = SolverExecutor(1, 0)
    blocker = _Held()
    slot_task = asyncio.create_task(executor.run(blocker))
    assert await asyncio.to_thread(blocker.started.wait, _WAIT_S)
    slots = executor._slots[asyncio.get_running_loop()]
    await _until(lambda: slots.stuck == 1)
    check = _StoppedAsShutdownStarts(executor)
    newcomer = asyncio.create_task(executor.run(check))  # a fresh task: lent a place
    assert await asyncio.to_thread(check.started.wait, _WAIT_S)
    now[0] += solver_mod._HARD_SLICES * solver_mod._SLICE_S  # it has shown it is hard
    await _until(lambda: executor._closed)
    blocker.gate.set()
    try:
        return (await newcomer)[0]
    except RuntimeError as exc:
        return f"RuntimeError: {exc}"
    finally:
        await slot_task
        assert check.shutting is not None
        await asyncio.to_thread(check.shutting.join)


async def test_a_newcomer_stopped_as_shutdown_starts_gets_its_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _stopped_newcomer_at_shutdown(monkeypatch) == "stopped"


@pytest.mark.xfail(strict=True, reason="AUDIT-36 #4")
async def test_a_newcomer_stopped_as_shutdown_starts_gets_its_result_under_p04(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mutant(
        monkeypatch,
        SolverExecutor,
        "run",
        "if not stopped or self._closed:",
        "if not stopped:",
    )
    assert await _stopped_newcomer_at_shutdown(monkeypatch) == "stopped"


# --------------------------------------------------------------------------
# #6 (medium): which checks SG34 stops is unpinned (Y04, Y05, Y03, Y09, Y11).
# ADR 0009's SG34 amendment stops only "a newcomer's check on a lent place",
# once it runs `_HARD_SLICES`, or a slice while a short caller waits. Five
# one-line slips pass the suite:
# - Y04: a newcomer's check in a slot is stopped too. Its first hard check is
#   killed after 4 slices and run again from the start, every time a new task
#   makes one: with LangGraph's fresh task per node, every hard check's first
#   run is wasted.
# - Y05: a short caller's check on a lent place is stopped too, so an easy run
#   whose one check turns hard is killed and queued again.
# - Y03: a waiting newcomer counts as a short caller, so newcomers stop each
#   other's checks after one slice.
# - Y09: the one-slice minimum is dropped (a check stops at once).
# - Y11: `_yields` no longer requires an attached check.
# SG34's tests reach `_yields` only with a short caller waiting after exactly
# one slice, or with no caller waiting after `_HARD_SLICES`.
# --------------------------------------------------------------------------


class _Target:
    def interrupt(self) -> None:
        """Nothing to stop: `_yields` only reads the interrupt."""


async def _yield_decisions(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """`_yields` for a lent newcomer's check, by what waits and how long it has run."""
    now = [0.0]
    monkeypatch.setattr(solver_mod, "_clock", lambda: now[0])
    out = {}
    for waiting in ("short", "new"):
        slots = solver_mod._Slots(1)
        assert await slots.acquire(False) is False
        waiter = asyncio.create_task(slots.acquire(waiting == "short", waiting == "new"))
        await asyncio.sleep(0)
        for ran in (0.5, 1.0, solver_mod._HARD_SLICES):
            for attached in (True, False):
                interrupt = solver_mod._Interrupt()
                interrupt.started = 0.0
                if attached:
                    assert interrupt.attach(_Target())
                now[0] = ran * solver_mod._SLICE_S
                key = f"{waiting} waits, ran {ran} slices{'' if attached else ', no check'}"
                out[key] = SolverExecutor._yields(interrupt, slots)
        slots.release(False)
        assert await waiter is False
        slots.release(False)
    return out


_YIELDS: Final = {
    "short waits, ran 0.5 slices": False,
    "short waits, ran 0.5 slices, no check": False,
    "short waits, ran 1.0 slices": True,
    "short waits, ran 1.0 slices, no check": False,
    f"short waits, ran {solver_mod._HARD_SLICES} slices": True,
    f"short waits, ran {solver_mod._HARD_SLICES} slices, no check": False,
    "new waits, ran 0.5 slices": False,
    "new waits, ran 0.5 slices, no check": False,
    "new waits, ran 1.0 slices": False,
    "new waits, ran 1.0 slices, no check": False,
    f"new waits, ran {solver_mod._HARD_SLICES} slices": True,
    f"new waits, ran {solver_mod._HARD_SLICES} slices, no check": False,
}


async def test_a_lent_newcomer_yields_only_as_documented(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _yield_decisions(monkeypatch) == _YIELDS


_Y03: Final = ("w.short and not w.future.done()", "(w.short or w.new) and not w.future.done()")
_Y09: Final = ("ran >= _SLICE_S and", "ran >= 0 and")
_Y11: Final = ("if not interrupt.attached():\n        return False\n    ", "")


@pytest.mark.xfail(strict=True, reason="AUDIT-36 #6")
@pytest.mark.parametrize(
    ("name", "edit"),
    [("short_waiting", _Y03), ("_yields", _Y09), ("_yields", _Y11)],
    ids=["y03", "y09", "y11"],
)
async def test_a_lent_newcomer_yields_only_as_documented_under(
    monkeypatch: pytest.MonkeyPatch, name: str, edit: tuple[str, str]
) -> None:
    if name == "_yields":
        _static_mutant(monkeypatch, SolverExecutor, name, *edit)
    else:
        _mutant(monkeypatch, solver_mod._Slots, name, *edit)
    assert await _yield_decisions(monkeypatch) == _YIELDS


def _static_mutant(
    monkeypatch: pytest.MonkeyPatch, owner: type, name: str, old: str, new: str
) -> None:
    """`_mutant` for a staticmethod, which `_mutant` cannot recompile."""
    func = getattr(owner, name)
    source = textwrap.dedent(inspect.getsource(func))
    assert source.count(old) == 1, f"{old!r} occurs {source.count(old)} times in {name}"
    namespace: dict[str, Any] = dict(func.__globals__)
    exec(
        compile("from __future__ import annotations\n" + source.replace(old, new), name, "exec"),
        namespace,
    )
    compiled = namespace[name].__func__
    mutated = types.FunctionType(compiled.__code__, func.__globals__, name)
    monkeypatch.setattr(owner, name, staticmethod(mutated))


async def _calls_of_a_check_that_runs_hard(monkeypatch: pytest.MonkeyPatch, kind: str) -> int:
    """How often a check is called that runs `_HARD_SLICES` as `kind`: a newcomer in the
    slot, or a short caller on a lent place. Neither may be stopped."""
    now = [0.0]
    monkeypatch.setattr(solver_mod, "_clock", lambda: now[0])
    executor = SolverExecutor(1, 0)
    polls: dict[int, int] = {}
    to_slow = executor._to_slow

    def counted(interrupt: solver_mod._Interrupt) -> bool:
        polls[id(interrupt)] = polls.get(id(interrupt), 0) + 1
        return to_slow(interrupt)

    monkeypatch.setattr(executor, "_to_slow", counted)
    calls = [0]
    check = _Held()

    def counting() -> str:
        calls[0] += 1
        return check()

    async def drive() -> None:
        assert await asyncio.to_thread(check.started.wait, _WAIT_S)
        interrupt = check.interrupt
        assert interrupt is not None
        seen = polls.get(id(interrupt), 0)
        now[0] += solver_mod._HARD_SLICES * solver_mod._SLICE_S
        # `_yields` has been asked twice since, or the check was stopped.
        await _until(lambda: polls.get(id(interrupt), 0) >= seen + 2 or interrupt.requested())
        check.gate.set()

    blocker, blocked = _Held(), None
    try:
        if kind == "short":
            await executor.run(lambda: None)  # this task's last check was short
            blocked = asyncio.create_task(executor.run(blocker))
            assert await asyncio.to_thread(blocker.started.wait, _WAIT_S)
            slots = executor._slots[asyncio.get_running_loop()]
            await _until(lambda: slots.stuck == 1)
        driver = asyncio.create_task(drive())
        out, _ = await executor.run(counting)  # in this task: short, or a newcomer
        await driver
        assert out == "held"
    finally:
        blocker.gate.set()
        if blocked is not None:
            await blocked
        await asyncio.to_thread(executor.shutdown)
    return calls[0]


async def test_a_hard_check_in_a_slot_or_of_a_short_caller_is_never_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kinds = ("new", "short")
    assert [await _calls_of_a_check_that_runs_hard(monkeypatch, k) for k in kinds] == [1, 1]


_Y04: Final = ("elif lent and new and self._yields", "elif new and self._yields")
_Y05: Final = ("elif lent and new and self._yields", "elif lent and self._yields")


@pytest.mark.xfail(strict=True, reason="AUDIT-36 #6")
@pytest.mark.parametrize("edit", [_Y04, _Y05], ids=["y04", "y05"])
async def test_a_hard_check_in_a_slot_or_of_a_short_caller_is_never_stopped_under(
    monkeypatch: pytest.MonkeyPatch, edit: tuple[str, str]
) -> None:
    _mutant(monkeypatch, SolverExecutor, "run", *edit)
    kinds = ("new", "short")
    assert [await _calls_of_a_check_that_runs_hard(monkeypatch, k) for k in kinds] == [1, 1]


# --------------------------------------------------------------------------
# #7 (low): a forked child re-adopts its own pool on every call (K07). Without
# `_adopt` recording the child's pid, every `acquire` in the child adopts again:
# it closes its own idle worker's pipe and starts a fresh worker for every check
# (a boot each time, the cost SG29's reuse removed). SG34's fork test checks
# three verdicts and disjoint pids, which a fresh worker per check also gives.
# --------------------------------------------------------------------------


@fork_only
def test_a_forked_child_reuses_its_own_worker() -> None:
    assert _forked("reuse")["child"] == "VALID VALID VALID 1"


@fork_only
@pytest.mark.xfail(strict=True, reason="AUDIT-36 #7")
def test_a_forked_child_reuses_its_own_worker_under_k07() -> None:
    assert _forked("reuse", "k07")["child"] == "VALID VALID VALID 1"
