"""AUDIT-39 repros: API/SDK consistency, round 10 (see AUDIT-39.md).

Every xfail asserts the *correct* behaviour and is strict, so it starts to
XPASS, and fails the suite, once its finding is fixed. No network and no LLM.
Waits are bounded by events, joins and subprocess timeouts, never by wall-clock
assertions.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Final

import pytest

from autolab.verification import solver as solver_mod
from autolab.verification.solver import SmtFormula, SolverExecutor, SolverOutcome, Z3Solver
from scripts.audit_load import NO_CUBES, POSITIVE, XYZ

_ROOT: Final = Path(__file__).resolve().parents[2]
_JOIN_S: Final = 30.0
"""Bounds a wait or a subprocess; a passing run never waits this long."""
_POLL_S: Final = 0.01

_HARD: Final = ([SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES))
"""A check that runs to its timeout (no cube sums to a positive...)."""
_EASY: Final = ([SmtFormula("(declare-const p Bool)", "p")], SmtFormula("", "p"))

fork_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="os.fork")


# --- #1 (medium): SG35 is "done" with flips it was assigned still open ---------------------


def _flips(plan: str, sg: str) -> list[str]:
    """The `AUDIT-n #m` items a sub-goal's **Flip:** list names."""
    bullet = next(ln for ln in plan.splitlines() if ln.startswith(f"- **{sg} "))
    flip = bullet.split("**Flip:**", 1)[1].split("**Verify:**", 1)[0]
    items: list[str] = []
    audit = ""
    for token in re.findall(r"AUDIT-\d+|#\d+", flip):
        if token.startswith("AUDIT-"):
            audit = token
        else:
            items.append(f"{audit} {token}")
    return items


def test_a_done_sub_goal_leaves_none_of_its_flips_open() -> None:
    # PLAN.md: a sub-goal that cannot meet its verification stops and reports (Rules);
    # SG35's row says "done (PR pending)", but two of its flips are still strict xfails.
    plan = (_ROOT / "PLAN.md").read_text()
    row = next(ln for ln in plan.splitlines() if ln.startswith("| SG35 |"))
    assert "done" in row
    xfails = "\n".join(p.read_text() for p in sorted((_ROOT / "tests" / "audit").glob("*.py")))
    still_open = [f for f in _flips(plan, "SG35") if f'reason="{f}"' in xfails]
    # Today: ['AUDIT-34 #2', 'AUDIT-34 #3'].
    assert still_open == [], still_open


# --- #2 (low): a worker started at the fork is still killed by the child's exit -------------

_MID_START: Final = """
import json, os, sys
sys.path.insert(0, ROOT)
from autolab.verification import solver_workers
from autolab.verification import solver as solver_mod

if __name__ == "__main__":
    real = solver_workers.Worker.__init__
    forked = []

    def init(self, ctx, target):
        real(self, ctx, target)
        # The interleaving under test: the process has started (multiprocessing lists it
        # in `process._children`) and `Workers._start` has not yet added it to `_live`.
        # Another thread forks here, as a prefork master does after building a Verifier.
        if not forked:
            pid = os.fork()
            if pid == 0:
                if sys.argv[1] == "exit":
                    sys.exit(0)  # a normal exit: multiprocessing's atexit hook runs
                os._exit(0)
            forked.append(pid)
            os.waitpid(pid, 0)

    solver_workers.Worker.__init__ = init
    out = solver_mod.Z3Solver(5.0).check_entailment(
        [solver_mod.SmtFormula("(declare-const p Bool)", "p")], solver_mod.SmtFormula("", "p")
    )
    print("RESULT " + json.dumps({"reason": out.reason, "details": out.details}))
"""


def _mid_start(tmp_path: Path, ending: str) -> dict[str, str]:
    script = tmp_path / "mid_start.py"
    script.write_text(f"ROOT = {str(_ROOT)!r}\n" + textwrap.dedent(_MID_START))
    proc = subprocess.run(
        [sys.executable, str(script), ending],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=4 * _JOIN_S,
        check=False,
    )
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), "")
    assert line, proc.stderr[-800:]
    got: dict[str, str] = json.loads(line[len("RESULT ") :])
    return got


@fork_only
def test_a_worker_started_at_the_fork_survives_a_child_that_exits_hard(tmp_path: Path) -> None:
    # Control: with `os._exit` no atexit hook runs, so the harness can pass.
    assert _mid_start(tmp_path, "_exit")["reason"] == "entailed"


@fork_only
@pytest.mark.xfail(strict=True, reason="AUDIT-39 #2")
def test_a_worker_started_at_the_fork_survives_a_child_that_exits_normally(
    tmp_path: Path,
) -> None:
    # ADR 0009 (SG35): a forked child's normal exit "no longer sends [the owner's
    # workers] SIGTERM". `_adopt` drops only the workers in `_live` from `_children`.
    got = _mid_start(tmp_path, "exit")
    # Today: {"reason": "worker_died", "details": "worker died (signal 15)"}.
    assert got["reason"] == "entailed", got


# --- #3 (low): `run`'s contract: a stop between two checks of one `fn` is returned ----------


async def test_a_stop_between_two_checks_of_one_fn_is_never_its_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Was: a pure `fn` of two checks, stopped after its first check had left its worker,
    # returned the second check's "interrupted". SG36 pauses a newcomer that gives way
    # instead of stopping it, so the second check, attached while the call is frozen,
    # starts paused and runs once the call has a place again.
    gap, go = threading.Event(), threading.Event()

    def two_checks() -> tuple[SolverOutcome, SolverOutcome]:
        first = Z3Solver(1.0).check_entailment(*_HARD)  # attached for about 1 s
        gap.set()
        go.wait(_JOIN_S)
        return first, Z3Solver(5.0).check_entailment(*_EASY)

    def yields(
        interrupt: solver_mod._Interrupt, slots: solver_mod._Slots, gave_way: bool = False
    ) -> bool:
        # The interleaving under test: `_yields` saw the check attached, and the pool
        # thread detached it before `freeze` ran.
        if gave_way or not interrupt.attached():
            return False
        gap.wait(_JOIN_S)
        return True

    real_freeze = solver_mod._Interrupt.freeze

    def freeze(self: solver_mod._Interrupt) -> None:
        try:
            real_freeze(self)
        finally:
            go.set()

    monkeypatch.setattr(SolverExecutor, "_yields", staticmethod(yields))
    monkeypatch.setattr(solver_mod._Interrupt, "freeze", freeze)
    ex = SolverExecutor(workers=1, slow_workers=0)
    try:
        hog = asyncio.create_task(ex.run(lambda: Z3Solver(20.0).check_entailment(*_HARD)))
        slots = ex._slot()
        while slots.stuck == 0 and not hog.done():  # the hog's slot now lends
            await asyncio.sleep(_POLL_S)
        (first, second), _ = await asyncio.wait_for(
            asyncio.create_task(ex.run(two_checks)), _JOIN_S
        )
        hog.cancel()
        await asyncio.gather(hog, return_exceptions=True)
    finally:
        ex.shutdown()
    assert first.reason == "timeout"
    assert second.reason == "entailed", second
