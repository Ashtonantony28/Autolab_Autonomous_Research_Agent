"""AUDIT-31 repros: API/SDK consistency, round 8 (see AUDIT-31.md).

Every test asserts the *correct* behaviour and is a strict xfail, so it
starts to XPASS, and fails the suite, once its finding is fixed. No network
and no LLM. Waits are bounded by joins and subprocess timeouts, never by
wall-clock assertions.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Final

import pydantic
import pytest

from autolab.agents.verification_monitor import AgentDecision, VerificationMonitor
from autolab.config import Settings
from autolab.verification import formalizer, solver_workers
from autolab.verification import solver as solver_mod
from autolab.verification.formalizer import FormalizationError, Vocabulary
from autolab.verification.vericot import Verifier
from tests.fixtures.golden_traces import GoldenStep, GoldenTrace, build

_JOIN_S: Final = 60.0
"""Bounds a subprocess; a passing run never waits this long."""


# --- #1 (medium): a worker that dies on an intermediate step is admitted, not re-run --------


def _trace_with_an_intermediate_step() -> GoldenTrace:
    # p, q assumed; step 2 (r, from p) is the one whose worker dies; step 3 (q) from step 1.
    decls = "(declare-const p Bool)\n(declare-const q Bool)\n(declare-const r Bool)"
    return GoldenTrace(
        "audit31-died",
        "a worker dies on an intermediate inference",
        "VALID",
        ("VALID", "VALID", "VALID", "VALID"),
        (
            GoldenStep("p holds", "premise", decls, "(and p (=> p r))"),
            GoldenStep("q holds", "premise", "", "q"),
            GoldenStep("so r", "inference", "", "r", (0,)),
            GoldenStep("so q", "conclusion", "", "q", (1,)),
        ),
    )


@pytest.mark.xfail(strict=True, reason="AUDIT-31 #1")
def test_a_check_whose_worker_died_is_rerun_or_escalated_not_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR-0009 (SG31): "No retry on a fresh worker ... The verification monitor's
    # retry or escalation already re-runs the step."
    trace, fake = build(_trace_with_an_intermediate_step())
    real_call = solver_workers.Worker.call
    calls = 0

    def dies_on_the_third_check(
        worker: solver_workers.Worker, args: tuple[Any, ...], budget_s: float
    ) -> Any:
        nonlocal calls
        calls += 1
        if calls == 3:  # step 2, the inference: the OOM killer, a Z3 abort, a `kill`
            worker.ready()
            worker.process.kill()
            worker.process.join()
        return real_call(worker, args, budget_s)

    monkeypatch.setattr(solver_workers.Worker, "call", dies_on_the_third_check)
    result = asyncio.run(Verifier(fake).verify(trace))
    died = [v for v in result.step_verdicts if v.reason == "worker_died"]
    assert [v.step_index for v in died] == [2], result.step_verdicts  # the death is recorded
    decision = VerificationMonitor().decide(result)
    # Today: PASS at consistency_score 0.875; the unchecked step is never re-run.
    assert decision is not AgentDecision.PASS, (decision, result.consistency_score)


# --- #2 (low): Settings accepts a solver timeout the solver silently clamps ---------------


@pytest.mark.xfail(strict=True, reason="AUDIT-31 #2")
def test_settings_refuse_a_solver_timeout_the_solver_would_clamp() -> None:
    over = solver_mod.TIMEOUT_MAX_S * 2
    assert solver_mod.Z3Solver(over)._args([], solver_mod.SmtFormula("", "true"))[-1] < over
    with pytest.raises(pydantic.ValidationError):
        Settings(solver_timeout_s=over)  # accepted today, then clamped to 1e6 s unannounced


# --- #3 (low): MAX_PROBE_CHARS admits less than its own docstring states ------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-31 #3")
def test_the_probe_budget_admits_what_its_docstring_promises() -> None:
    # "one `MAX_ARITY` Int function and a few dozen constants" (formalizer.py).
    ints = " ".join(["Int"] * formalizer.MAX_ARITY)
    decls = [f"(declare-fun g ({ints}) Bool)"]
    decls += [f"(declare-const c{i} Bool)" for i in range(24)]  # two dozen
    try:
        Vocabulary().merged("\n".join(decls))
    except FormalizationError as exc:  # today: refused at the 22nd constant
        pytest.fail(str(exc))


# --- #4 (low): the stdlib patch leaves `Workers` unable to run a `__main__` target --------


@pytest.mark.xfail(strict=True, reason="AUDIT-31 #4")
def test_workers_run_a_target_defined_in_the_main_module(tmp_path: Path) -> None:
    # `solver_workers.Workers(target, ...)` takes any target. Since SG32 a worker
    # never rebuilds `__main__`, so a target defined there cannot be unpickled.
    script = tmp_path / "double.py"
    script.write_text(
        textwrap.dedent(
            """
            from autolab.verification.solver_workers import Workers

            def double(x):
                return 2 * x

            if __name__ == "__main__":
                pool = Workers(double, idle_max=1)
                worker = pool.acquire()
                print("REPLY", worker.call((21,), 30.0), repr(worker.fault))
                pool.close()
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
    reply = [line for line in proc.stdout.splitlines() if line.startswith("REPLY")]
    # Today: `REPLY None 'failed to start'`. Run by hand, the forkserver child prints
    # "AttributeError: Can't get attribute 'double' on <module '__main__' (built-in)>".
    assert reply == ["REPLY (True, 42) ''"], (reply, proc.stderr[-400:])


def test_the_patch_is_installed_on_import() -> None:
    # Context for #4 (passes today): importing the solver replaces a stdlib function.
    from multiprocessing import spawn

    assert spawn.get_preparation_data is solver_workers._without_main
