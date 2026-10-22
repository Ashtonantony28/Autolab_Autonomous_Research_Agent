"""AUDIT-33: encoding soundness, round 9. SG34's stopped newcomers and fork adoption.

Every test asserts the *correct* behaviour and is a strict xfail until its
finding is fixed; see `AUDIT-33.md`. The controls are plain tests. None of them
calls a real LLM, and none asserts an absolute time: each time bound is a
multiple of a time the same script measured on this machine. No finding admits
a wrong trace: each makes a check hang, raise or report the wrong cause, closed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest

_ROOT: Final = Path(__file__).resolve().parents[2]

fork_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="os.fork")


def _run_script(tmp_path: Path, name: str, source: str) -> dict[str, Any]:
    script = tmp_path / name
    script.write_text(source)
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    assert lines, proc.stdout + proc.stderr
    got: dict[str, Any] = json.loads(lines[-1])
    return got


# ---------------------------------------------------------------------------
# AUDIT-33 #1 (medium): SG34's at-fork hook resets the forkserver's lock but not
# the other process-wide state a worker start holds: multiprocessing's resource
# tracker RLock (taken by `forkserver.ensure_running` on every worker start) and
# the import lock of `multiprocessing.popen_forkserver` (imported by the first
# start). A process forked while the owner's prestart or replacement thread is
# starting a worker inherits that state held by a thread that does not exist in
# it. Every check then waits forever (no timeout covers worker start-up), raises
# ReentrantCallError (a RuntimeError, so an error pause), or raises ImportError,
# which `escalate_permanent_errors` does not catch.
# Unsimulated, forking at once after `default_executor()` (as `build_graph()`
# then a pre-forking server does) broke the child in 8 of 20 runs. The repro holds
# the tracker's lock from a parent thread at the fork, as a worker start does.

_TRACKER_SCRIPT: Final = """
import asyncio, json, os, select, signal, threading, time
from multiprocessing import resource_tracker
from autolab.verification import solver as S
from autolab.verification.solver import SmtFormula, Z3Solver, default_executor

D = "(declare-const x Int)"

def check():
    z = Z3Solver(timeout_seconds=5.0)
    async def main():
        out, _ = await default_executor().run(
            lambda: z.check_entailment([SmtFormula(D, "(> x 5)")], SmtFormula(D, "(> x 0)")))
        return out.verdict
    return asyncio.run(main())

def fork_and_check(wait):
    r, w = os.pipe()
    t0 = time.perf_counter()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            out = check()
        except BaseException as exc:
            out = f"{type(exc).__name__}: {exc}"
        os.write(w, out.encode())
        os._exit(0)
    os.close(w)
    ready = select.select([r], [], [], wait)[0]
    out = os.read(r, 4096).decode() if ready else "no answer"
    took = time.perf_counter() - t0
    if not ready:
        os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    os.close(r)
    return out, took

if __name__ == "__main__":
    first = check()
    S._workers().prestart(S.SOLVER_WORKERS + S.SOLVER_SLOW_WORKERS)
    control, took = fork_and_check(600)  # nothing held: the child starts its own forkserver
    holding, done = threading.Event(), threading.Event()
    def worker_start_in_progress():
        # `forkserver.ensure_running` holds this lock on every worker start.
        with resource_tracker._resource_tracker._lock:
            holding.set()
            done.wait()
    thread = threading.Thread(target=worker_start_in_progress)
    thread.start()
    holding.wait()
    during, during_took = fork_and_check(10 * took)
    done.set()
    thread.join()
    print(json.dumps({"first": first, "control": control, "control_s": took,
                      "during": during, "during_s": during_took, "after": check()}))
"""


@pytest.fixture(scope="module")
def tracker_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _run_script(tmp_path_factory.mktemp("audit33"), "fork_mid_start.py", _TRACKER_SCRIPT)


@fork_only
def test_a_child_forked_with_nothing_held_verifies(tracker_run: dict[str, Any]) -> None:
    """Control: SG34 flips AUDIT-29 #1 for a fork outside a worker start; the parent is unharmed."""
    assert tracker_run["control"] == "VALID", tracker_run
    assert tracker_run["first"] == tracker_run["after"] == "VALID", tracker_run


@fork_only
@pytest.mark.xfail(strict=True, reason="AUDIT-33 #1")
def test_a_child_forked_during_a_worker_start_verifies(tracker_run: dict[str, Any]) -> None:
    # Today: "ReentrantCallError: Reentrant call into the multiprocessing resource
    # tracker" (5 of 5) when the child's pool thread gets the holder's thread id, which
    # it does here; otherwise "no answer": it waits forever in `ensure_running`.
    assert tracker_run["during"] == "VALID", tracker_run


# ---------------------------------------------------------------------------
# AUDIT-33 #2 (low): `Worker.__init__` holds the worker's end of the pipe from
# `ctx.Pipe()` until `child.close()`, across `process.start()` (milliseconds per
# worker; prestart is ~0.3 s of such windows back to back). A process forked in
# that window keeps a copy of the worker's end, and `_adopt` cannot close it. In
# the owner, that worker's death no longer reads as EOF: a killed, cancelled or
# SG34-stopped check waits out its whole budget (timeout + grace) instead of
# returning at once, holding its pool thread, and a crash reads as a timeout
# (fault "", reason `timeout`) rather than `worker_died`.

_LEAK_SCRIPT: Final = """
import json, multiprocessing, os, signal, threading, time
from multiprocessing.context import ForkServerProcess
from autolab.verification import solver as S, solver_workers as W

D = "(declare-const a Int)(declare-const b Int)(declare-const c Int)"
PREMISES = [S.SmtFormula(D, f"(> {v} 0)") for v in "abc"]
CLAIM = S.SmtFormula(D, "(not (= (+ (* a a a) (* b b b)) (* c c c)))")  # Fermat n=3
ARGS = (S._build_smt2(PREMISES, CLAIM), 3, False, 600.0)

ctx = multiprocessing.get_context("forkserver")
ctx.set_forkserver_preload([W.__name__, S.__name__])

def killed_during_call(worker, budget):
    assert worker.ready()
    t0 = time.perf_counter()
    killer = threading.Timer(0.1, worker.interrupt)
    killer.start()
    reply = worker.call(ARGS, budget)
    took = time.perf_counter() - t0
    worker.stop()
    return {"reply": reply is not None, "fault": worker.fault, "took": took}

if __name__ == "__main__":
    clean = killed_during_call(W.Worker(ctx, S._solve), 600.0)
    sleepers = []
    real_start = ForkServerProcess.start
    def start_then_fork(self):
        real_start(self)
        pid = os.fork()  # a user's fork lands before Worker.__init__ closes `child`
        if pid == 0:
            time.sleep(600)
            os._exit(0)
        sleepers.append(pid)
    ForkServerProcess.start = start_then_fork
    leaky_worker = W.Worker(ctx, S._solve)
    ForkServerProcess.start = real_start
    leaky = killed_during_call(leaky_worker, 20 * clean["took"])
    for pid in sleepers:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    print(json.dumps({"clean": clean, "leaky": leaky}))
"""


@pytest.fixture(scope="module")
def leak_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _run_script(tmp_path_factory.mktemp("audit33"), "fork_mid_init.py", _LEAK_SCRIPT)


@fork_only
def test_a_killed_worker_reads_as_died_at_once(leak_run: dict[str, Any]) -> None:
    """Control: with no copy of its pipe elsewhere, a worker killed mid-check is DIED at once."""
    clean = leak_run["clean"]
    assert (clean["reply"], clean["fault"]) == (False, "died"), leak_run


@fork_only
@pytest.mark.xfail(strict=True, reason="AUDIT-33 #2")
def test_a_fork_during_worker_start_does_not_hide_its_death(leak_run: dict[str, Any]) -> None:
    clean, leaky = leak_run["clean"], leak_run["leaky"]
    # Today: fault "" (read as a timeout) after the whole budget, 20x the clean time.
    assert leaky["fault"] == "died", leak_run
    assert leaky["took"] < 5 * clean["took"], leak_run


# ---------------------------------------------------------------------------
# AUDIT-33 #3 (low): the at-fork hook leaves `multiprocessing.process._children`
# alone, so a child made by a plain `os.fork()` (a pre-forking server) still lists
# the owner's workers. Its first worker start runs multiprocessing's `_cleanup`,
# which polls each of them and reads the exit status the forkserver writes on the
# owner's sentinel pipe. The child reads the owner's message: the owner then sees
# exit code 255, not -9, for a worker it killed, and `_lost` reports
# "(exit code 255)" instead of "(signal 9)" for a worker that died.

_STEAL_SCRIPT: Final = """
import asyncio, json, os, time
from autolab.verification import solver as S
from autolab.verification.solver import SmtFormula, Z3Solver, default_executor

D = "(declare-const x Int)"

def check():
    z = Z3Solver(timeout_seconds=5.0)
    async def main():
        out, _ = await default_executor().run(
            lambda: z.check_entailment([SmtFormula(D, "(> x 5)")], SmtFormula(D, "(> x 0)")))
        return out.verdict
    return asyncio.run(main())

if __name__ == "__main__":
    first = check()
    pool = S._workers()
    pool.prestart(2)
    victim, spare = pool._idle[0], pool._idle[1]
    done_r, done_w = os.pipe()
    go_r, go_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.read(go_r, 1)
        out = check()
        os.write(done_w, out.encode())
        os._exit(0)
    t0 = time.perf_counter()
    spare.process.kill()
    spare.process.join()  # control: joined before the child starts a worker
    victim.process.kill()
    time.sleep(10 * (time.perf_counter() - t0))  # the forkserver reports the death
    os.write(go_w, b"g")
    child = os.read(done_r, 64).decode()
    os.waitpid(pid, 0)
    victim.process.join()
    print(json.dumps({"first": first, "child": child, "spare": spare.process.exitcode,
                      "victim": victim.process.exitcode}))
"""


@pytest.fixture(scope="module")
def steal_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _run_script(tmp_path_factory.mktemp("audit33"), "fork_steals_status.py", _STEAL_SCRIPT)


@fork_only
def test_the_child_verifies_and_a_joined_worker_reads_its_signal(
    steal_run: dict[str, Any],
) -> None:
    """Control: verdicts are right on both sides; a worker joined before the child starts is -9."""
    assert steal_run["first"] == steal_run["child"] == "VALID", steal_run
    assert steal_run["spare"] == -9, steal_run


@fork_only
@pytest.mark.xfail(strict=True, reason="AUDIT-33 #3")
def test_a_forked_child_leaves_the_owners_exit_statuses_alone(steal_run: dict[str, Any]) -> None:
    # Today: 255; the child's first worker start read the owner's status message.
    assert steal_run["victim"] == -9, steal_run
