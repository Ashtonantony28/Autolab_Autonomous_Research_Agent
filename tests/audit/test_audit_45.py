"""AUDIT-45: encoding soundness, round 12. SG37's bounded pauses: a due lender runs on
while the earliest lent check is frozen in its place, paused newcomers capped at
`workers` (past that, stopped and run again), a lent check that keeps the slot of a
lender that ended, one shared watchdog thread, and overlapping pauses counted once.

Every xfail asserts the *correct* behaviour and is strict until its finding is fixed;
see `AUDIT-45.md`. The control is a plain test. Nothing here calls a real LLM, and no
test asserts an absolute time: the hard check's timeout is a multiple of the time an
easy check took through the same executor in the same script (never below 20 of the
solver's own slices), and the injected pool-thread delay is a multiple of that easy
check too. No finding admits a wrong trace.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from autolab.state.models import StepVerdict

_ROOT: Final = Path(__file__).resolve().parents[2]

posix_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="SIGSTOP")


def _run_script(tmp_path: Path, name: str, source: str, *args: str) -> dict[str, Any]:
    script = tmp_path / name
    script.write_text(source)
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    assert lines, proc.stdout + proc.stderr
    got: dict[str, Any] = json.loads(lines[-1])
    return got


# ---------------------------------------------------------------------------
# AUDIT-45 #1 (low): a lent check frozen before its pool thread starts gets a negative
# `solver_ms`. `SolverExecutor.run` calls `slots.lending(interrupt)` *before*
# `_pool.submit`, and `_Slots._balance` freezes the earliest lent check as soon as a
# lender is due. `_Interrupt.freeze` then sets `_off` before `_call` sets `started`, so
# `ran_s()` subtracts frozen time from before the call began. If the pool thread starts
# the call later than the check runs after its thaw, `solver_ms < 0`, and
# `StepVerdict(solver_ms=...)` (`ge=0`) raises out of `Verifier.verify`. Fails loud,
# never admits. The script injects the pool-thread delay (scheduler latency).

_NEG_SCRIPT: Final = """
import asyncio, json, os, sys, time
sys.path.insert(0, os.getcwd())  # the repo root, for `scripts`
from autolab.verification import solver as S
from scripts.audit_load import NO_CUBES, POSITIVE, XYZ

X = "(declare-const x Int)"
EASY = ([S.SmtFormula(X, "(> x 0)")], S.SmtFormula(X, "(> x -1)"))
HARD = ([S.SmtFormula(XYZ, POSITIVE)], S.SmtFormula(XYZ, NO_CUBES))
DELAY = float(sys.argv[1])  # pool-thread start delay, in easy checks; 0 = unmodified

async def main():
    ex = S.SolverExecutor(workers=1, slow_workers=0)
    pool = S._workers()
    end = time.monotonic() + 300
    while len(pool._idle) < 1 and time.monotonic() < end:  # prestart done
        await asyncio.sleep(0.01)
    easy = S.Z3Solver(timeout_seconds=600.0)
    t0 = time.perf_counter()
    for _ in range(5):
        await ex.run(lambda: easy.check_entailment(*EASY))
    easy_s = (time.perf_counter() - t0) / 5
    timeout_s = max(40 * easy_s, 20 * S._SLICE_S)
    if DELAY:
        orig, d = S.SolverExecutor._call, DELAY * easy_s
        def delayed(self, fn, interrupt):
            time.sleep(d)  # the pool thread starts the call late
            return orig(self, fn, interrupt)
        S.SolverExecutor._call = delayed
    hard = S.Z3Solver(timeout_seconds=timeout_s)
    ms = []
    async def stream(stop):
        while time.perf_counter() < stop:
            out, m = await ex.run(lambda: easy.check_entailment(*EASY))
            ms.append((m, out.verdict))
    h = asyncio.ensure_future(ex.run(lambda: hard.check_entailment(*HARD)))
    slots = ex._slot()
    while slots.stuck == 0 and not h.done():  # the hard check lends its slot's place
        await asyncio.sleep(0.005)
    stop = time.perf_counter() + 4 * timeout_s  # past the lender's 1.5-timeout share
    streams = [asyncio.ensure_future(stream(stop)) for _ in range(4)]
    hout, _ = await h
    await asyncio.gather(*streams)
    ex.shutdown()
    return {"easy_s": easy_s, "timeout_s": timeout_s, "runs": len(ms),
            "negatives": sum(m < 0 for m, _ in ms), "min_ms": min(m for m, _ in ms),
            "verdicts": sorted({v for _, v in ms}), "hard": [hout.verdict, hout.reason]}

print(json.dumps(asyncio.run(main())))
"""


@posix_only
@pytest.mark.timeout(300)
def test_lent_checks_beside_a_due_lender_keep_a_non_negative_run_time(tmp_path: Path) -> None:
    """Control: unmodified, every lent check's `solver_ms` is >= 0 and VALID."""
    got = _run_script(tmp_path, "neg_plain.py", _NEG_SCRIPT, "0")
    assert got["hard"] == ["UNGROUNDED", "timeout"], got
    assert got["verdicts"] == ["VALID"], got
    assert got["negatives"] == 0, got


@posix_only
@pytest.mark.timeout(300)
@pytest.mark.xfail(strict=True, reason="AUDIT-45 #1")
def test_a_lent_check_frozen_before_it_starts_keeps_a_non_negative_run_time(
    tmp_path: Path,
) -> None:
    # Today: 1 lent check per run (the one lent when the lender came due) reports
    # solver_ms of -29 to -106 ms with a 20-easy-check pool-thread delay (-5 ms at 5).
    got = _run_script(tmp_path, "neg_delayed.py", _NEG_SCRIPT, "20")
    assert got["hard"] == ["UNGROUNDED", "timeout"], got
    assert got["verdicts"] == ["VALID"], got  # never a wrong verdict
    assert got["negatives"] == 0, got
    # The impact on the agent's path: the step's verdict cannot be built.
    try:
        StepVerdict(
            step_index=0, verdict="VALID", reason="entailed", details="", solver_ms=got["min_ms"]
        )
    except ValidationError as exc:
        pytest.fail(f"Verifier.verify would raise: {exc}")
