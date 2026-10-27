"""AUDIT-37: encoding soundness, round 10. SG35's stops that keep their result, run-time
pauses, re-queued newcomers and forked children dropping the owner's workers.

Every xfail asserts the *correct* behaviour and is strict until its finding is fixed;
see `AUDIT-37.md`. The control is a plain test. Nothing here calls a real LLM, and no
test asserts an absolute time: the check's timeout is a multiple of the time the same
script measured for a forked child to exit. No finding admits a wrong trace.
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
# AUDIT-37 #1 (low): SG35's `Workers._adopt` drops from multiprocessing's `_children`
# only the workers in `_live`. `BaseProcess.start` adds a new worker to `_children`
# before `Workers._start` adds it to `_live`, so a raw fork in that window leaves the
# worker in the child's `_children`. When that child exits normally, multiprocessing's
# atexit hook SIGTERMs it (AUDIT-35 #1 again, for that one worker), and the owner's
# check on it ends UNGROUNDED/worker_died instead of running to its own end. The
# repro forks from inside `ForkServerProcess.start`, right after the real start,
# during a replacement start (`Workers.discard`).

_EXIT_SCRIPT: Final = """
import json, os, sys, threading, time
from multiprocessing.context import ForkServerProcess
sys.path.insert(0, os.getcwd())  # the repo root, for `scripts`
from autolab.verification import solver as S
from scripts.audit_load import NO_CUBES, POSITIVE, XYZ

def fork_waiting_child():
    # A raw fork, as a pre-forking server makes; it exits normally when told.
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(w)
        os.read(r, 1)
        sys.exit(0)  # atexit handlers run
    os.close(r)
    return pid, w

def let_exit(child):
    pid, w = child
    t0 = time.perf_counter()
    os.write(w, b"x")
    os.close(w)
    os.waitpid(pid, 0)
    return time.perf_counter() - t0

def check_on_newest(pool, timeout_s):
    # `acquire` pops the newest idle worker: the replacement `discard` just started.
    got = {}
    newest = pool._idle[-1]
    z = S.Z3Solver(timeout_seconds=timeout_s)
    def run():
        got["out"] = z.check_entailment([S.SmtFormula(XYZ, POSITIVE)], S.SmtFormula(XYZ, NO_CUBES))
    thread = threading.Thread(target=run)
    thread.start()
    end = time.monotonic() + 600
    while newest in pool._idle and time.monotonic() < end:
        time.sleep(0.005)
    return newest, thread, got

def round_(pool, in_window, timeout_s):
    child = None
    real_start = ForkServerProcess.start
    if in_window:
        def start_then_fork(self):
            nonlocal child
            real_start(self)
            child = fork_waiting_child()  # before Workers._start adds it to _live
        ForkServerProcess.start = start_then_fork
    pool.discard(pool.acquire())  # a replacement start, as after a timeout or a stop
    ForkServerProcess.start = real_start
    if not in_window:
        child = fork_waiting_child()
    newest, thread, got = check_on_newest(pool, timeout_s)
    let_exit(child)
    alive = newest.alive()
    thread.join()
    out = got["out"]
    return {"alive": alive, "verdict": out.verdict, "reason": out.reason,
            "details": out.details}

if __name__ == "__main__":
    ex = S.default_executor()
    pool = S._workers()
    want = min(ex.workers + ex.slow_workers, pool.idle_max)
    end = time.monotonic() + 600
    while len(pool._idle) < want and time.monotonic() < end:  # prestart done
        time.sleep(0.01)
    exit_s = max(let_exit(fork_waiting_child()) for _ in range(3))
    timeout_s = 50 * exit_s  # the check still runs when the child exits
    print(json.dumps({"exit_s": exit_s, "control": round_(pool, False, timeout_s),
                      "window": round_(pool, True, timeout_s)}))
"""


@pytest.fixture(scope="module")
def exit_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _run_script(tmp_path_factory.mktemp("audit37"), "fork_exit_mid_start.py", _EXIT_SCRIPT)


@fork_only
def test_a_child_forked_outside_a_start_spares_the_owners_worker(exit_run: dict[str, Any]) -> None:
    """Control: SG35's fix: the child's normal exit leaves the owner's busy worker running."""
    control = exit_run["control"]
    assert control["alive"], exit_run
    assert (control["verdict"], control["reason"]) == ("UNGROUNDED", "timeout"), exit_run


@fork_only
@pytest.mark.xfail(strict=True, reason="AUDIT-37 #1")
def test_a_child_forked_during_a_worker_start_spares_that_worker(
    exit_run: dict[str, Any],
) -> None:
    # Today: the worker is dead after the child's exit and the owner's check is
    # UNGROUNDED/worker_died, "worker died (signal 15)" (or "worker failed to start"
    # when the SIGTERM lands during its boot).
    window = exit_run["window"]
    assert window["alive"], exit_run
    assert window["reason"] == "timeout", exit_run
