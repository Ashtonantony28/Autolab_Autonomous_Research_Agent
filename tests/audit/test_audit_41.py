"""AUDIT-41: encoding soundness, round 11. SG36's paused lenders, paused newcomers, the
worker's watchdog on a pause-aware clock, and `Worker._signal` on reaped workers.

Every xfail asserts the *correct* behaviour and is strict until its finding is fixed;
see `AUDIT-41.md`. The control is a plain test. Nothing here calls a real LLM, and no
test asserts an absolute time: the hard check's timeout is a multiple of the time an
easy check took through the same executor in the same script (never below 20 of the
solver's own slices), and every bound is a multiple of that timeout. No finding admits
a wrong trace.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest

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
# AUDIT-41 #1 (medium): a frozen lender is never thawed while its lent place stays
# busy. `_Slots._balance` freezes `_lenders[:lent]`, and nothing ages a lender: while
# short callers keep the lent place occupied, the stuck slot check stays SIGSTOPped
# and its paused time is not budget, so its wall time is the length of the short
# stream plus its timeout, without bound. Before SG36 the lender ran on and ended at
# its timeout. Fails closed (UNGROUNDED/timeout, late), but `timeout_seconds` no
# longer bounds a step's wall time, and the slot it holds is not freed either.

_STARVE_SCRIPT: Final = """
import asyncio, json, os, sys, time
sys.path.insert(0, os.getcwd())  # the repo root, for `scripts`
from autolab.verification import solver as S
from scripts.audit_load import NO_CUBES, POSITIVE, XYZ

X = "(declare-const x Int)"
EASY = ([S.SmtFormula(X, "(> x 0)")], S.SmtFormula(X, "(> x -1)"))
HARD = ([S.SmtFormula(XYZ, POSITIVE)], S.SmtFormula(XYZ, NO_CUBES))
STREAMS = 4

async def main(with_stream):
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
    stream_s = 6 * timeout_s
    hard = S.Z3Solver(timeout_seconds=timeout_s)

    async def hard_run():
        t = time.perf_counter()
        out, _ = await ex.run(lambda: hard.check_entailment(*HARD))
        return time.perf_counter() - t, out.verdict, out.reason

    runs = [0]
    async def stream(stop):
        while time.perf_counter() < stop:
            await ex.run(lambda: easy.check_entailment(*EASY))
            runs[0] += 1

    h = asyncio.ensure_future(hard_run())
    slots = ex._slot()
    while slots.stuck == 0 and not h.done():  # the hard check holds the slot, stuck
        await asyncio.sleep(0.005)
    streams = []
    if with_stream:
        stop = time.perf_counter() + stream_s
        streams = [asyncio.ensure_future(stream(stop)) for _ in range(STREAMS)]
    hard_s, verdict, reason = await h
    await asyncio.gather(*streams)
    ex.shutdown()
    return {"easy_s": easy_s, "timeout_s": timeout_s, "stream_s": stream_s,
            "hard_s": hard_s, "verdict": verdict, "reason": reason, "easy_runs": runs[0]}

print(json.dumps(asyncio.run(main(sys.argv[1] == "stream"))))
"""


@posix_only
@pytest.mark.timeout(300)
def test_a_stuck_check_alone_ends_near_its_timeout(tmp_path: Path) -> None:
    """Control: with no lent place in use, the stuck check ends close to its timeout."""
    got = _run_script(tmp_path, "starve_alone.py", _STARVE_SCRIPT, "alone")
    assert (got["verdict"], got["reason"]) == ("UNGROUNDED", "timeout"), got
    assert got["hard_s"] < 3 * got["timeout_s"], got


@posix_only
@pytest.mark.timeout(300)
def test_a_frozen_lender_ends_near_its_timeout_beside_a_short_stream(tmp_path: Path) -> None:
    # Today: the hard check ends only after the stream (6 timeouts) stops, at about
    # 6.8 timeouts, frozen the whole time; before SG36 it ended at 1.0 timeout.
    got = _run_script(tmp_path, "starve_stream.py", _STARVE_SCRIPT, "stream")
    assert (got["verdict"], got["reason"]) == ("UNGROUNDED", "timeout"), got
    assert got["easy_runs"] > 0, got
    assert got["hard_s"] < 3 * got["timeout_s"], got
