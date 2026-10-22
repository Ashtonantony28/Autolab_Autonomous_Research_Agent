"""AUDIT-35 repros: API/SDK consistency, round 9 (see AUDIT-35.md).

Every xfail asserts the *correct* behaviour and is strict, so it starts to
XPASS, and fails the suite, once its finding is fixed. No network and no LLM.
Waits are bounded by polls, joins and subprocess timeouts, never by wall-clock
assertions.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Final

import pytest

from autolab.verification.solver import SmtFormula, SolverExecutor, SolverOutcome, Z3Solver
from scripts.audit_load import NO_CUBES, POSITIVE, XYZ

_ROOT: Final = Path(__file__).resolve().parents[2]
_JOIN_S: Final = 120.0
"""Bounds a subprocess; a passing run never waits this long."""
_POLL_S: Final = 0.01

_HARD: Final = ([SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES))
"""A check that runs to its timeout (no cube sums to a positive...)."""
_EASY: Final = ([SmtFormula("(declare-const p Bool)", "p")], SmtFormula("", "p"))

fork_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="os.fork")


# --- #1 (medium): a forked child that exits normally kills the owner's workers ------------

_CHILD_EXITS: Final = """
import asyncio, json, os, sys, threading, time
sys.path.insert(0, ROOT)
from autolab.verification import solver as solver_mod
from scripts.audit_load_2 import slow_check

if __name__ == "__main__":
    ex = solver_mod.default_executor()
    pool = solver_mod._workers()
    want = min(ex.workers + ex.slow_workers, pool.idle_max)
    end = time.monotonic() + 60
    while len(pool._idle) < want and time.monotonic() < end:  # prestart done
        time.sleep(0.01)
    before = len(pool._idle)
    got = {}
    check = threading.Thread(target=lambda: got.update(r=asyncio.run(slow_check(ex, 5.0))))
    check.start()
    while len(pool._idle) >= before and time.monotonic() < end:  # a worker is in use
        time.sleep(0.01)
    idle = list(pool._idle)  # the owner's idle workers, besides the one checking
    pid = os.fork()
    if pid == 0:
        sys.exit(0)  # a normal exit: atexit handlers run, as in a prefork server's worker
    os.waitpid(pid, 0)
    check.join()
    alive = [w.alive() for w in idle]
    print("RESULT " + json.dumps({"idle_alive": alive, "in_flight": got.get("r")}))
"""


@fork_only
@pytest.mark.xfail(strict=True, reason="AUDIT-35 #1")
def test_a_forked_child_that_exits_normally_leaves_the_owners_workers_alone(
    tmp_path: Path,
) -> None:
    # ADR 0009 (SG34): a forked child "never [uses or kills] the parent's" workers.
    script = tmp_path / "child_exits.py"
    script.write_text(f"ROOT = {str(_ROOT)!r}\n" + textwrap.dedent(_CHILD_EXITS))
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=_JOIN_S,
        check=False,
    )
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), "")
    assert line, proc.stderr[-800:]
    got = json.loads(line[len("RESULT ") :])
    # Today: {"idle_alive": [false, ...], "in_flight": "worker_died"}, and the child prints
    # "AssertionError: can only join a child process" from multiprocessing's atexit hook.
    assert got["in_flight"] == "timeout", (got, proc.stderr[-400:])
    assert got["idle_alive"] and all(got["idle_alive"]), got


# --- #2 (low): SolverExecutor.run may call `fn` twice ----------------------------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-35 #2")
def test_run_calls_fn_once() -> None:
    # `SolverExecutor.run(fn)`: "Run `fn` on the pool". SG34 stops a newcomer's check on
    # a lent place once it has run `_HARD_SLICES` slices and runs `fn` again for a slot.
    calls = 0

    def counted() -> SolverOutcome:
        nonlocal calls
        calls += 1
        hard = calls == 1  # the second call (if any) returns at once
        return Z3Solver(20.0 if hard else 5.0).check_entailment(*(_HARD if hard else _EASY))

    async def main() -> None:
        ex = SolverExecutor(workers=1, slow_workers=0)
        try:
            hog = asyncio.create_task(ex.run(lambda: Z3Solver(20.0).check_entailment(*_HARD)))
            slots = ex._slot()
            while slots.stuck == 0 and not hog.done():  # the hog's slot now lends
                await asyncio.sleep(_POLL_S)
            newcomer = asyncio.create_task(ex.run(counted))
            lent = False
            while not newcomer.done():
                lent = lent or (calls == 1 and not slots._waiters)
                if lent and slots._waiters:  # stopped and queued again for a slot
                    break
                await asyncio.sleep(_POLL_S)
            hog.cancel()
            await asyncio.gather(hog, return_exceptions=True)
            await newcomer
        finally:
            ex.shutdown()

    asyncio.run(main())
    assert calls == 1, f"fn was called {calls} times"  # today: 2
