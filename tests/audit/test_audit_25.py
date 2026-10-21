"""AUDIT-25: encoding soundness, round 7. SG28-SG30's worker processes and scheduling.

Every test asserts the *correct* behaviour and is a strict xfail until its
finding is fixed; see `AUDIT-25.md`. The controls are plain tests. None of
them calls a real LLM. No finding admits a wrong trace: each one makes a check
fail closed for the wrong reason, or repeats work, or leaks a worker.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Final

import pytest

from autolab.verification import solver as solver_mod
from autolab.verification import solver_workers
from autolab.verification.solver import SmtFormula, SolverOutcome, Z3Solver
from scripts.audit_load import POSITIVE, XYZ

_ROOT: Final = Path(__file__).resolve().parents[2]
_WAIT_S: Final = 30.0

# ---------------------------------------------------------------------------
# AUDIT-25 #1 (medium): SG29's forkserver re-runs the caller's main script in
# every worker it starts. A script without an `if __name__ == "__main__":` guard
# (ADR 0009 requires one, nothing checks it) runs its whole body again in each
# prestarted worker and in each replacement, up to its first solver check, which
# then fails in the worker. So every check in the parent is UNGROUNDED, "worker
# killed past the timeout", and the script's side effects (LLM calls, writes)
# repeat once per worker. Before SG29 the same script got VALID once.

_SCRIPT: Final = """
import asyncio, os, sys
from autolab.verification.solver import SmtFormula, Z3Solver, default_executor

async def check():
    decls = "(declare-const p Bool)"
    solver = Z3Solver(timeout_seconds=5.0)
    out, _ = await default_executor().run(
        lambda: solver.check_entailment([SmtFormula("", "p")], SmtFormula(decls, "p"))
    )
    return out

def body():
    with open(sys.argv[1], "a") as log:
        log.write(f"{os.getpid()} {__name__}\\n")  # the script's side effect
    out = asyncio.run(check())
    print(out.verdict, "|", out.details, flush=True)

"""


def _run_script(tmp_path: Path, *, guarded: bool) -> tuple[list[str], str]:
    """Run a user script that checks `p |= p`; return its body's log lines and its output."""
    run = "if __name__ == '__main__':\n    body()\n" if guarded else "body()\n"
    script = tmp_path / "user_script.py"
    script.write_text(_SCRIPT + run)
    log = tmp_path / "body.log"
    proc = subprocess.run(
        [sys.executable, str(script), str(log)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return log.read_text().splitlines(), proc.stdout.strip()


def test_a_guarded_script_checks_once_and_gets_valid(tmp_path: Path) -> None:
    """Control: with the guard the body runs once and `p |= p` is VALID."""
    body, out = _run_script(tmp_path, guarded=True)

    assert len(body) == 1
    assert out.startswith("VALID")


@pytest.mark.xfail(strict=True, reason="AUDIT-25 #1")
def test_an_unguarded_script_is_not_rerun_in_the_workers(tmp_path: Path) -> None:
    body, out = _run_script(tmp_path, guarded=False)

    # Today: the body ran 6-7 times (once as __main__, the rest as __mp_main__ in
    # workers), and the parent got "UNGROUNDED | worker killed past the timeout".
    assert out.startswith("VALID"), out
    assert len(body) == 1, body


# ---------------------------------------------------------------------------
# AUDIT-25 #2 (low): a worker that dies mid-check (OOM killer, a Z3 abort, a stray
# signal) gives UNGROUNDED/`timeout`, "worker killed past the timeout", however
# little of the timeout had passed. The trace then records a timeout that never
# happened, and the check is not retried on a fresh worker.

_HARD: Final = SmtFormula("", f"(and {POSITIVE} (= (+ (* x x x) (* y y y)) (* z z z)))")
"""Fermat n=3: unsat, beyond Z3's nonlinear tactics, so it runs to its timeout."""


def _check_with_worker_killed(monkeypatch: pytest.MonkeyPatch, timeout_s: float) -> Any:
    """Run the hard check, SIGKILL its worker once it is busy; return (outcome, elapsed)."""
    busy: list[solver_workers.Worker] = []
    real_call = solver_workers.Worker.call

    def recording_call(
        self: solver_workers.Worker, args: tuple[Any, ...], budget_s: float
    ) -> solver_workers.Reply | None:
        busy.append(self)
        return real_call(self, args, budget_s)

    monkeypatch.setattr(solver_workers.Worker, "call", recording_call)
    result: list[SolverOutcome] = []
    started = time.perf_counter()
    thread = threading.Thread(
        target=lambda: result.append(
            Z3Solver(timeout_seconds=timeout_s).check_entailment([_HARD], SmtFormula(XYZ, "false"))
        )
    )
    thread.start()
    deadline = time.monotonic() + _WAIT_S
    while not busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert busy, "the check never reached a worker"
    os.kill(busy[0].process.pid, signal.SIGKILL)
    thread.join(_WAIT_S)
    return result[0], time.perf_counter() - started


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="SIGKILL")
@pytest.mark.xfail(strict=True, reason="AUDIT-25 #2")
def test_a_crashed_worker_is_not_reported_as_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    timeout_s = 20.0
    outcome, elapsed = _check_with_worker_killed(monkeypatch, timeout_s)

    # Precondition: it ended well inside its timeout, and fail closed.
    assert elapsed < timeout_s / 2
    assert outcome.verdict == "UNGROUNDED"
    # Today: reason "timeout", details "worker killed past the timeout".
    assert "past the timeout" not in outcome.details


# ---------------------------------------------------------------------------
# AUDIT-25 #3 (low): `check_entailment` waits for a new worker to boot
# (`worker.ready()`) outside its try block. An exception raised there (a
# per-test timeout's signal, Ctrl-C on the main thread) leaves the worker
# attached, neither released nor discarded: a live process no one can reach.
# ADR 0009 says such a worker is killed.


class _Signal(BaseException):
    """Stands in for a signal handler's exception arriving while the worker boots."""


def _record_acquire(monkeypatch: pytest.MonkeyPatch) -> list[solver_workers.Worker]:
    acquired: list[solver_workers.Worker] = []
    real_acquire = solver_workers.Workers.acquire

    def recording_acquire(self: solver_workers.Workers) -> solver_workers.Worker:
        worker = real_acquire(self)
        acquired.append(worker)
        return worker

    monkeypatch.setattr(solver_workers.Workers, "acquire", recording_acquire)
    return acquired


def _check_p() -> SolverOutcome:
    return Z3Solver().check_entailment(
        [SmtFormula("", "p")], SmtFormula("(declare-const p Bool)", "p")
    )


def test_a_check_returns_its_worker_to_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: after a normal check the acquired worker is idle again."""
    acquired = _record_acquire(monkeypatch)

    assert _check_p().verdict == "VALID"
    (worker,) = acquired
    assert worker in solver_mod._workers()._idle


@pytest.mark.xfail(strict=True, reason="AUDIT-25 #3")
def test_an_error_while_a_worker_boots_does_not_leak_it(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = solver_mod._workers()
    acquired = _record_acquire(monkeypatch)

    def interrupted_ready(self: solver_workers.Worker) -> bool:
        raise _Signal

    monkeypatch.setattr(solver_workers.Worker, "ready", interrupted_ready)
    with pytest.raises(_Signal):
        _check_p()
    monkeypatch.undo()
    (worker,) = acquired
    try:
        # Today: still alive, still counted live, and not in the idle list.
        assert worker in pool._idle or not worker.alive()
    finally:
        if worker not in pool._idle:
            pool.discard(worker)
