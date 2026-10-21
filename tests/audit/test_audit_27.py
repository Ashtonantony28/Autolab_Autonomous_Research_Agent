"""AUDIT-27 repros: API/SDK consistency, round 7 (see AUDIT-27.md).

Every test asserts the *correct* behaviour and is a strict xfail, so it
starts to XPASS, and fails the suite, once its finding is fixed. No network
and no LLM. Waits are bounded by events and joins, never by wall-clock
assertions.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Any

import pytest

import autolab.verification.solver as solver_mod
from autolab.verification import solver_workers
from autolab.verification.solver import SmtFormula, Z3Solver

_DECLS = "(declare-const p Bool)"
_PREMISES = [SmtFormula(_DECLS, "p")]
_CLAIM = SmtFormula(_DECLS, "p")  # entailed: only a missing reply can make it not VALID
_JOIN_S = 30.0


# --- #1 (medium): a worker that died or never started is reported as killed past the timeout


@pytest.mark.xfail(strict=True, reason="AUDIT-27 #1")
def test_a_worker_that_dies_is_not_reported_as_killed_past_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The worker dies as soon as the request is sent, far inside its 30 s timeout.
    real_call = solver_workers.Worker.call

    def dying(worker: solver_workers.Worker, args: tuple[Any, ...], budget_s: float) -> Any:
        worker.process.kill()
        worker.process.join()
        return real_call(worker, args, budget_s)

    monkeypatch.setattr(solver_workers.Worker, "call", dying)
    out = Z3Solver(30.0).check_entailment(_PREMISES, _CLAIM)
    assert out.verdict == "UNGROUNDED"  # fail-closed holds
    assert "past the timeout" not in out.details, out.details


@pytest.mark.xfail(strict=True, reason="AUDIT-27 #1")
def test_a_script_without_a_main_guard_is_not_told_its_checks_timed_out(tmp_path: Path) -> None:
    # The quickstart in `vericot`'s docstring, as a plain script: no `__main__` guard.
    # Every worker re-runs the script and dies starting a process of its own.
    script = tmp_path / "check.py"
    script.write_text(
        textwrap.dedent(
            """
            from autolab.verification.solver import SmtFormula, Z3Solver
            d = "(declare-const p Bool)"
            out = Z3Solver(30.0).check_entailment([SmtFormula(d, "p")], SmtFormula(d, "p"))
            print("OUTCOME", out.verdict, out.reason, out.details)
            """
        )
    )
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=_JOIN_S,
        check=False,
    )
    outcome = [line for line in proc.stdout.splitlines() if line.startswith("OUTCOME")]
    # Correct: the caller raises (non-zero exit), or the outcome names the start-up
    # failure. Today: `OUTCOME UNGROUNDED timeout worker killed past the timeout`.
    assert proc.returncode != 0 or "past the timeout" not in outcome[-1], outcome


# --- #2 (low): a long configured timeout raises OverflowError on the worker path ---------


@pytest.mark.xfail(strict=True, reason="AUDIT-27 #2")
def test_a_long_timeout_is_decided_in_a_worker_as_it_is_here() -> None:
    # `Settings.solver_timeout_s` accepts any value > 0 (config.py:49). About 26 days.
    solver = Z3Solver(timeout_seconds=2.25e6)
    here = solver._check_here(_PREMISES, _CLAIM)
    assert here.verdict == "VALID"
    assert solver.check_entailment(_PREMISES, _CLAIM) == here  # OverflowError today


# --- #3 (low): shutdown_default_executor() races the constructor's pre-start thread -----


@pytest.mark.xfail(strict=True, reason="AUDIT-27 #3")
def test_no_worker_is_left_running_after_shutting_down_the_default_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    solver_mod.shutdown_default_executor()  # start from no idle workers
    pool = solver_mod._workers()
    # Process start-up is slow only for the pre-start thread: it is still starting
    # workers when the executor is shut down.
    gate = threading.Event()
    real_start = solver_workers.Workers._start

    def slow_start(self: solver_workers.Workers) -> solver_workers.Worker:
        if threading.current_thread() is not threading.main_thread():
            gate.wait(_JOIN_S)
        return real_start(self)

    monkeypatch.setattr(solver_workers.Workers, "_start", slow_start)
    before = set(threading.enumerate())
    solver_mod.default_executor()
    prestart = [t for t in threading.enumerate() if t not in before]
    solver_mod.shutdown_default_executor()
    gate.set()
    for thread in prestart:
        thread.join(_JOIN_S)
    live = pool.live()
    pool.close()  # this test's own clean-up
    assert prestart
    assert live == 0, f"{live} worker processes started after shutdown_default_executor()"
