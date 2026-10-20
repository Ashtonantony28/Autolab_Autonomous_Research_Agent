"""SG29: solver checks in worker processes (ADR 0009; AUDIT-22 #2, #3, #5, #6).

The worker pool on plain targets, the worker deciding exactly what `_solve`
decides in this process (the in-process tests of `_solve` guard the real path),
cleanup, and sizing from the cgroup CPU quota.
"""

from __future__ import annotations

import json
import operator
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
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
