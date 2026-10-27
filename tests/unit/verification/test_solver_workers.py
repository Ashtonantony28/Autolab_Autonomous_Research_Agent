"""SG29: solver checks in worker processes (ADR 0009; AUDIT-22 #2, #3, #5, #6).

The worker pool on plain targets, the worker deciding exactly what `_solve`
decides in this process (the in-process tests of `_solve` guard the real path),
cleanup, and sizing from the cgroup CPU quota.
"""

from __future__ import annotations

import json
import multiprocessing
import operator
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from multiprocessing import spawn
from multiprocessing.connection import Connection
from pathlib import Path
from types import SimpleNamespace
from typing import Final

import pytest

from autolab.verification import solver as solver_mod
from autolab.verification import solver_workers
from autolab.verification.solver import (
    SmtFormula,
    Z3Solver,
    default_executor,
    shutdown_default_executor,
)
from autolab.verification.vericot import Verifier
from scripts.audit_load import NO_CUBES, POSITIVE, XYZ
from tests.fixtures.golden_traces import ALL, build

_WAIT_S: Final = 10.0
_ROOT: Final = Path(__file__).resolve().parents[3]


@pytest.fixture
def pool() -> Iterator[solver_workers.Workers]:
    workers = solver_workers.Workers(operator.truediv, idle_max=2)
    yield workers
    workers.close()


# --- the pool -----------------------------------------------------------------------


def test_a_worker_answers_calls_and_is_reused(pool: solver_workers.Workers) -> None:
    worker = pool.acquire()
    assert worker.call((6, 3), _WAIT_S) == (True, 2.0)
    pid = worker.process.pid
    pool.release(worker)
    again = pool.acquire()
    assert again.process.pid == pid
    assert again.call((1, 4), _WAIT_S) == (True, 0.25)
    pool.release(again)
    assert pool.live() == 1


def test_an_error_in_a_worker_is_its_reply_and_the_worker_lives_on(
    pool: solver_workers.Workers,
) -> None:
    worker = pool.acquire()
    ok, text = worker.call((1, 0), _WAIT_S)  # type: ignore[misc]
    assert (ok, text.split(":")[0]) == (False, "ZeroDivisionError")
    assert worker.call((1, 1), _WAIT_S) == (True, 1.0)
    pool.release(worker)


def test_a_call_past_its_budget_returns_none_and_discard_waits_for_the_process() -> None:
    sleeping = solver_workers.Workers(time.sleep, idle_max=1)
    try:
        worker = sleeping.acquire()
        assert worker.call((0,), _WAIT_S) == (True, None)  # booted
        assert worker.call((_WAIT_S,), 0.05) is None
        sleeping.discard(worker)
        assert not worker.alive()  # gone before `discard` returned (AUDIT-22 #5)
        assert sleeping.live() == 1  # its replacement
        fresh = sleeping.acquire()
        assert fresh is not worker
        assert fresh.call((0,), _WAIT_S) == (True, None)
        sleeping.release(fresh)
    finally:
        sleeping.close()
    assert sleeping.live() == 0


def test_an_interrupted_call_returns_none_at_once() -> None:
    sleeping = solver_workers.Workers(time.sleep, idle_max=1)
    try:
        worker = sleeping.acquire()
        assert worker.call((0,), _WAIT_S) == (True, None)
        started = time.monotonic()
        threading.Timer(0.05, worker.interrupt).start()
        assert worker.call((_WAIT_S,), _WAIT_S) is None
        assert time.monotonic() - started < _WAIT_S / 2
        sleeping.discard(worker)
    finally:
        sleeping.close()


def test_start_up_does_not_count_against_the_budget(pool: solver_workers.Workers) -> None:
    worker = pool.acquire()  # not booted yet: the forkserver forks it now
    assert worker.call((1, 2), 0.0001) in ((True, 0.5), None)
    pool.discard(worker)
    worker = pool.acquire()
    assert worker.call((1, 2), _WAIT_S) == (True, 0.5)  # booted, then the call
    pool.release(worker)


def test_beyond_idle_max_a_released_worker_is_stopped(pool: solver_workers.Workers) -> None:
    held = [pool.acquire() for _ in range(3)]
    for worker in held:
        assert worker.call((1, 1), _WAIT_S) == (True, 1.0)
    for worker in held:
        pool.release(worker)
    assert pool.live() == 2
    assert not held[-1].alive()


def test_prestart_fills_the_idle_list_up_to_idle_max(pool: solver_workers.Workers) -> None:
    pool.prestart(5)
    assert len(pool._idle) == pool.idle_max == 2
    pool.prestart(1)
    assert pool.live() == 2


def test_a_dead_idle_worker_is_never_handed_out(pool: solver_workers.Workers) -> None:
    pool.prestart(1)
    dead = pool._idle[0]
    dead.process.kill()
    dead.process.join()
    worker = pool.acquire()
    assert worker is not dead
    assert worker.call((2, 1), _WAIT_S) == (True, 2.0)
    pool.release(worker)


def test_workers_come_from_a_forkserver() -> None:
    worker = solver_mod._workers().acquire()
    try:
        assert worker.process._start_method in ("forkserver", "spawn")
        assert worker.process.daemon
    finally:
        solver_mod._workers().release(worker)


@pytest.mark.parametrize(
    "main",
    [
        SimpleNamespace(__spec__=None, __file__="/srv/user_script.py"),
        SimpleNamespace(__spec__=SimpleNamespace(name="user_pkg.run"), __file__="/srv/run.py"),
    ],
    ids=["path", "dash-m"],
)
def test_a_worker_is_prepared_without_the_main_module(
    monkeypatch: pytest.MonkeyPatch, main: SimpleNamespace
) -> None:
    # AUDIT-26 #4, AUDIT-25 #1: a worker never re-runs the caller's main; other processes do.
    monkeypatch.setitem(sys.modules, "__main__", main)
    keys = {"init_main_from_path", "init_main_from_name"}
    assert not keys & spawn.get_preparation_data(solver_workers._NAME).keys()
    assert keys & spawn.get_preparation_data("another").keys()


_UNGUARDED_MODULE: Final = """
import os, sys
from autolab.verification.solver import SmtFormula, Z3Solver
with open(sys.argv[1], "a") as log:
    log.write(f"{os.getpid()}\\n")
decls = "(declare-const p Bool)"
out = Z3Solver().check_entailment([SmtFormula("", "p")], SmtFormula(decls, "p"))
print(out.verdict, out.details)
"""


def test_an_unguarded_dash_m_main_runs_once_and_gets_valid(tmp_path: Path) -> None:
    (tmp_path / "unguarded_main.py").write_text(_UNGUARDED_MODULE)
    log = tmp_path / "body.log"
    proc = subprocess.run(
        [sys.executable, "-m", "unguarded_main", str(log)],
        cwd=_ROOT,
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), str(_ROOT)])},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert proc.stdout.startswith("VALID"), proc.stdout + proc.stderr
    assert len(log.read_text().splitlines()) == 1


# --- replies matched to their request, and why a call returned none (SG31) -----------


def _piped_worker() -> tuple[solver_workers.Worker, Connection]:
    """A booted `Worker` whose other end of the pipe the test plays."""
    parent, child = multiprocessing.Pipe()
    worker = solver_workers.Worker.__new__(solver_workers.Worker)
    worker.conn, worker._ready, worker._seq, worker.fault = parent, True, 0, ""
    return worker, child


@pytest.mark.parametrize(
    "message",
    [(0, (True, 1.0)), (2, (True, 1.0)), (True, 1.0), (1, "VALID"), (1, (1, 2, 3)), None],
    ids=["earlier", "later", "untagged", "not-a-reply", "malformed", "boot"],
)
def test_a_message_that_is_not_this_requests_reply_is_never_returned(message: object) -> None:
    worker, other = _piped_worker()
    other.send(message)
    assert worker.call((1, 1), _WAIT_S) is None
    assert worker.fault == solver_workers.OUT_OF_TURN
    assert other.recv() == (1, (1, 1))


def test_a_reply_that_echoes_its_request_is_returned() -> None:
    worker, other = _piped_worker()
    worker._seq = 6
    other.send((7, (True, 1.0)))
    assert worker.call((1, 1), _WAIT_S) == (True, 1.0)
    assert worker.fault == ""


def test_a_reply_left_in_the_pipe_never_answers_the_next_call(
    pool: solver_workers.Workers,
) -> None:
    worker = pool.acquire()
    assert worker.ready()
    worker._send((6, 3))  # cut short: its reply arrives, unread
    assert worker.conn.poll(_WAIT_S)
    assert worker.call((1, 4), _WAIT_S) is None
    assert worker.fault == solver_workers.OUT_OF_TURN
    pool.discard(worker)


def test_a_worker_that_never_starts_failed_to_start() -> None:
    worker, other = _piped_worker()
    worker._ready = False
    other.close()
    assert not worker.ready()
    assert worker.call((1, 1), _WAIT_S) is None
    assert worker.fault == solver_workers.FAILED_TO_START


def test_a_worker_killed_mid_call_died_and_one_past_its_budget_has_no_fault() -> None:
    sleeping = solver_workers.Workers(time.sleep, idle_max=1)
    try:
        worker = sleeping.acquire()
        assert worker.call((_WAIT_S,), 0.0) is None
        assert worker.fault == ""
        sleeping.discard(worker)
        worker = sleeping.acquire()
        assert worker.call((0,), _WAIT_S) == (True, None)
        threading.Timer(0.05, worker.process.kill).start()  # e.g. the OOM killer
        assert worker.call((_WAIT_S,), _WAIT_S) is None
        assert worker.fault == solver_workers.DIED
        sleeping.discard(worker)
    finally:
        sleeping.close()


@pytest.fixture
def solver_pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[solver_workers.Workers]:
    """A private `_solve` pool of one idle place, used by every check in the test."""
    workers = solver_workers.Workers(solver_mod._solve, idle_max=1)
    monkeypatch.setattr(solver_mod, "_workers", lambda: workers)
    yield workers
    workers.close()


_PQ: Final = "(declare-const p Bool)\n(declare-const q Bool)"
_P_ENTAILS_P: Final = ([SmtFormula(_PQ, "p")], SmtFormula(_PQ, "p"))
_P_ENTAILS_Q: Final = ([SmtFormula(_PQ, "p")], SmtFormula(_PQ, "q"))


def test_a_stale_valid_reply_never_answers_a_later_check(
    solver_pool: solver_workers.Workers,
) -> None:
    # A worker parked with the unread VALID of `p |- p`, as a release in place of a
    # discard would leave it (AUDIT-28 #1): `p |- q` must not read it.
    worker = solver_pool.acquire()
    assert worker.ready()
    worker._send(Z3Solver()._args(*_P_ENTAILS_P))
    assert worker.conn.poll(_WAIT_S)
    solver_pool.release(worker)
    out = Z3Solver().check_entailment(*_P_ENTAILS_Q)
    assert out == solver_mod.SolverOutcome(
        "UNGROUNDED", "worker_died", "worker replied out of turn"
    )
    assert not worker.alive() and worker not in solver_pool._idle
    assert Z3Solver().check_entailment(*_P_ENTAILS_Q).verdict == "INVALID"


def test_a_cancel_while_a_worker_is_acquired_is_interrupted(
    monkeypatch: pytest.MonkeyPatch, solver_pool: solver_workers.Workers
) -> None:
    interrupt = solver_mod._Interrupt()
    monkeypatch.setattr(solver_mod._worker, "interrupt", interrupt, raising=False)
    real_acquire = solver_pool.acquire
    acquired: list[solver_workers.Worker] = []
    calls: list[object] = []

    def acquire() -> solver_workers.Worker:
        acquired.append(real_acquire())
        interrupt.request()  # lands after the early check, before `attach`
        return acquired[-1]

    def call(worker: solver_workers.Worker, args: tuple[object, ...], budget_s: float) -> None:
        calls.append(args)

    monkeypatch.setattr(solver_pool, "acquire", acquire)
    monkeypatch.setattr(solver_workers.Worker, "call", call)
    assert Z3Solver().check_entailment(*_P_ENTAILS_P) == solver_mod._INTERRUPTED
    assert calls == []
    assert not interrupt.attached()
    assert acquired[0] in solver_pool._idle and acquired[0].alive()  # given back, unused


class _Signal(BaseException):
    """What a signal handler raises in the caller while a worker boots."""


def test_an_error_while_a_worker_boots_discards_it(
    monkeypatch: pytest.MonkeyPatch, solver_pool: solver_workers.Workers
) -> None:
    acquired: list[solver_workers.Worker] = []
    real_acquire = solver_pool.acquire

    def acquire() -> solver_workers.Worker:
        acquired.append(real_acquire())
        return acquired[-1]

    monkeypatch.setattr(solver_pool, "acquire", acquire)

    def ready(worker: solver_workers.Worker) -> bool:
        raise _Signal

    monkeypatch.setattr(solver_workers.Worker, "ready", ready)
    with pytest.raises(_Signal):
        Z3Solver().check_entailment(*_P_ENTAILS_P)
    assert not acquired[0].alive() and acquired[0] not in solver_pool._idle
    assert solver_pool.live() == 1  # its replacement


def test_a_worker_that_fails_to_start_is_reported_so(
    monkeypatch: pytest.MonkeyPatch, solver_pool: solver_workers.Workers
) -> None:
    def ready(worker: solver_workers.Worker) -> bool:
        worker.fault = solver_workers.FAILED_TO_START
        return False

    calls: list[object] = []
    monkeypatch.setattr(solver_workers.Worker, "ready", ready)
    monkeypatch.setattr(solver_workers.Worker, "call", lambda *a: calls.append(a))
    out = Z3Solver().check_entailment(*_P_ENTAILS_P)
    assert out == solver_mod.SolverOutcome("UNGROUNDED", "worker_died", "worker failed to start")
    assert calls == []


@pytest.mark.parametrize("timeout_s", [2.25e6, 1e12, float("inf")])
def test_a_huge_timeout_is_clamped_and_decided_as_here(
    monkeypatch: pytest.MonkeyPatch, timeout_s: float
) -> None:
    budgets: list[float] = []
    real_call = solver_workers.Worker.call

    def call(worker: solver_workers.Worker, args: tuple[object, ...], budget_s: float) -> object:
        budgets.append(budget_s)
        return real_call(worker, args, budget_s)

    monkeypatch.setattr(solver_workers.Worker, "call", call)
    solver = Z3Solver(timeout_s)
    here = solver._check_here(*_P_ENTAILS_P)
    assert here.verdict == "VALID"
    assert solver.check_entailment(*_P_ENTAILS_P) == here
    cap = solver_mod.TIMEOUT_MAX_S
    assert budgets == [cap + solver_mod._grace_s(cap)]
    assert budgets[0] * 1000 < 2**31  # what `poll` accepts


# --- the worker decides what `_solve` decides here ------------------------------------

_DECLS: Final = "(declare-const p Bool)\n(declare-const q Bool)\n(declare-const x Int)"
_CASES: Final[dict[str, tuple[list[str], str]]] = {
    "entailed": (["p", "(=> p q)"], "q"),
    "not-entailed": (["p"], "q"),
    "inconsistent": (["p", "(not p)"], "q"),
    "true": (["p"], "true"),
    "arithmetic": (["(> x 3)"], "(> x 1)"),
    "malformed": (["p"], "(> p"),
    "no-premises": ([], "p"),
}


def test_the_worker_runs_solve() -> None:
    assert solver_mod._workers().target is solver_mod._solve


@pytest.mark.parametrize("case", sorted(_CASES))
def test_a_worker_reaches_the_outcome_solve_reaches_here(case: str) -> None:
    premises, claim = _CASES[case]
    pool = [SmtFormula(_DECLS, a) for a in premises]
    solver = Z3Solver(30.0)
    there = solver.check_entailment(pool, SmtFormula(_DECLS, claim))
    here = solver._check_here(pool, SmtFormula(_DECLS, claim))
    assert there == here


def test_a_worker_times_out_where_solve_times_out_here() -> None:
    # Z3's own `unknown` at its timeout, not a kill: the worker returns in time.
    solver = Z3Solver(0.3)
    premises, claim = [SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES)
    here = solver._check_here(premises, claim)
    there = solver.check_entailment(premises, claim)
    assert (here.verdict, here.reason) == (there.verdict, there.reason) == ("UNGROUNDED", "timeout")
    assert there != solver_mod._KILLED


async def test_every_golden_verdict_is_the_same_in_a_worker_and_here(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def verdicts() -> list[object]:
        out: list[object] = []
        for golden in ALL:
            trace, fake = build(golden)
            result = await Verifier(fake).verify(trace)
            out.append(
                (
                    result.consistency_score,
                    [(v.verdict, v.reason, v.details) for v in result.step_verdicts],
                )
            )
        return out

    there = await verdicts()
    monkeypatch.setattr(Z3Solver, "check_entailment", Z3Solver._check_here)
    assert await verdicts() == there


# --- cleanup --------------------------------------------------------------------------

_EXITING_CHILD: Final = """
import json
from autolab.verification import solver as solver_mod
from autolab.verification.solver import SmtFormula, Z3Solver
decls = "(declare-const p Bool)"
out = Z3Solver().check_entailment([SmtFormula(decls, "p")], SmtFormula(decls, "p"))
assert out.verdict == "VALID"
solver_mod._workers().prestart(3)
print(json.dumps([w.process.pid for w in solver_mod._workers()._live]))
"""


def _running(pid: int) -> bool:
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return False
    return "State:\tZ" not in status


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_no_worker_outlives_the_interpreter() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _EXITING_CHILD],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    pids: list[int] = json.loads(proc.stdout.strip().splitlines()[-1])
    assert pids
    deadline = time.monotonic() + _WAIT_S
    while any(_running(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not [pid for pid in pids if _running(pid)]


async def test_shutting_down_the_default_executor_stops_idle_workers() -> None:
    executor = default_executor()
    decls = "(declare-const p Bool)"
    out, _ = await executor.run(
        lambda: Z3Solver().check_entailment([SmtFormula(decls, "p")], SmtFormula(decls, "p"))
    )
    assert out.verdict == "VALID"
    shutdown_default_executor()
    assert solver_mod._workers().live() == 0


# --- a process forked after the pool started (AUDIT-29 #1, AUDIT-30 #2) ------------------

_FORKING: Final = """
import asyncio, json, os, sys, time
from multiprocessing import forkserver
from autolab.verification import solver as solver_mod
from autolab.verification.solver import SmtFormula, Z3Solver, default_executor

D = "(declare-const p Bool)"

def check():
    async def main():
        out, _ = await default_executor().run(
            lambda: Z3Solver().check_entailment([SmtFormula(D, "p")], SmtFormula(D, "p"))
        )
        return out.verdict
    return asyncio.run(main())

def state():
    return {"workers": sorted(w.process.pid for w in solver_mod._workers()._live),
            "server": forkserver._forkserver._forkserver_pid}

if __name__ == "__main__":
    first = check()
    pool = solver_mod._workers()
    pool.prestart(2)
    parent = state()
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            got = {"verdicts": [check() for _ in range(3)], **state()}
            solver_mod.shutdown_default_executor()  # stops this process's workers only
        except BaseException as exc:
            got = {"error": repr(exc)}
        os.write(write, json.dumps(got).encode())
        os.close(write)
        if sys.argv[1] == "exit":
            sys.exit(0)  # atexit hooks run, multiprocessing's among them
        os._exit(0)
    os.close(write)
    os.waitpid(pid, 0)
    with os.fdopen(read) as f:
        child = json.loads(f.read())
    alive = all(w.alive() for w in pool._idle)
    print(json.dumps({"first": first, "parent": parent, "child": child, "alive": alive,
                      "after": check()}))
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="fork")
@pytest.mark.parametrize("ending", ["_exit", "exit"])
def test_a_forked_child_starts_its_own_workers_and_leaves_the_parents_alone(ending: str) -> None:
    # A child that exits normally runs multiprocessing's atexit hook, which terminates
    # the daemons it lists as its children (AUDIT-35 #1, AUDIT-36 #1).
    proc = subprocess.run(
        [sys.executable, "-c", _FORKING, ending],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    child, parent = got["child"], got["parent"]
    assert child.get("verdicts") == ["VALID"] * 3, child
    assert child["workers"] and not set(child["workers"]) & set(parent["workers"])
    assert child["server"] not in (None, parent["server"])
    assert got["alive"] and got["first"] == got["after"] == "VALID"


def test_a_pool_used_in_another_process_adopts_none_of_the_owners_workers(
    pool: solver_workers.Workers, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool.prestart(2)
    owned = list(pool._idle)
    killed: list[solver_workers.Worker] = []
    monkeypatch.setattr(solver_workers.Worker, "stop", killed.append)
    monkeypatch.setattr(os, "getpid", lambda: -1)  # as in a forked child
    pool.close()
    assert (pool._idle, pool.live(), pool._ctx, killed) == ([], 0, None, [])
    assert all(w.conn.closed for w in owned)  # this process's copies of the pipes only
    monkeypatch.undo()
    for worker in owned:
        worker.process.kill()
        worker.process.join()


# --- sizing from the cgroup CPU quota (AUDIT-22 #6) -------------------------------------


def _files(monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> None:
    monkeypatch.setattr(solver_mod, "_read", files.get)


@pytest.mark.parametrize(
    ("files", "cpus"),
    [
        ({}, None),
        ({"/sys/fs/cgroup/cpu.max": "max 100000\n"}, None),
        ({"/sys/fs/cgroup/cpu.max": "100000 100000\n"}, 1),
        ({"/sys/fs/cgroup/cpu.max": "150000 100000\n"}, 2),
        ({"/sys/fs/cgroup/cpu.max": "50000 100000\n"}, 1),
        (
            {
                "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "-1\n",
                "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000\n",
            },
            None,
        ),
        (
            {
                "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us": "200000\n",
                "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us": "100000\n",
            },
            2,
        ),
        (
            {
                "/proc/self/cgroup": "0::/a/b\n",
                "/sys/fs/cgroup/a/b/cpu.max": "300000 100000\n",
            },
            3,
        ),
        (
            {
                "/proc/self/cgroup": "4:cpu,cpuacct:/job\n",
                "/sys/fs/cgroup/cpu,cpuacct/job/cpu.cfs_quota_us": "100000\n",
                "/sys/fs/cgroup/cpu,cpuacct/job/cpu.cfs_period_us": "100000\n",
                "/sys/fs/cgroup/cpu.max": "400000 100000\n",
            },
            1,
        ),
        ({"/sys/fs/cgroup/cpu.max": "garbage\n"}, None),
    ],
    ids=[
        "none",
        "v2-max",
        "v2-one",
        "v2-rounded-up",
        "v2-half",
        "v1-unlimited",
        "v1-two",
        "v2-own-path",
        "smallest-wins",
        "unreadable",
    ],
)
def test_the_cgroup_quota_is_read_from_v1_and_v2_files(
    monkeypatch: pytest.MonkeyPatch, files: dict[str, str], cpus: int | None
) -> None:
    _files(monkeypatch, files)
    assert solver_mod._cgroup_cpus() == cpus


def test_the_pool_takes_the_smaller_of_affinity_and_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "process_cpu_count", raising=False)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: {0, 1, 2, 3})
    _files(monkeypatch, {"/sys/fs/cgroup/cpu.max": "100000 100000\n"})
    assert solver_mod._available_cpus() == 1
    _files(monkeypatch, {"/sys/fs/cgroup/cpu.max": "800000 100000\n"})
    assert solver_mod._available_cpus() == 4
    _files(monkeypatch, {})
    assert solver_mod._available_cpus() == 4
