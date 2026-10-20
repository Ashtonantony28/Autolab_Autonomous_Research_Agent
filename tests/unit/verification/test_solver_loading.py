"""SG27: long premise pools are loaded one formula at a time (AUDIT-18 #3, #7).

Counters and fake clocks, no wall clocks; the AUDIT-18 #3 repros time the real thing.
Loading must reach the verdict the one-call path reaches on every input.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Final

import pytest
import z3

from autolab.verification import solver as solver_mod
from autolab.verification.solver import SmtFormula, SolverOutcome, Z3Solver
from autolab.verification.vericot import Verifier
from tests.fixtures.golden_traces import ALL, build

_LONG: Final = solver_mod._LOAD_ABOVE + 20
"""Premises in a pool that is loaded one formula at a time."""


def _chain(n: int, claim: str = "(> r0 0.0)") -> tuple[list[SmtFormula], SmtFormula]:
    """`n - 1` premises r_i > r_{i+1} + 1.0 over Reals, the shape of AUDIT-18 #3."""
    decls = "\n".join(f"(declare-const r{i} Real)" for i in range(n))
    premises = [SmtFormula("", f"(> r{i} (+ r{i + 1} 1.0))") for i in range(n - 1)]
    return premises, SmtFormula(decls, claim)


class _Counts:
    def __init__(self) -> None:
        self.loading = 0
        self.checks = 0


@pytest.fixture
def counts(monkeypatch: pytest.MonkeyPatch) -> _Counts:
    """Count loading checks (C API) and consistency/entailment checks (`Solver.check`)."""
    seen = _Counts()
    real_load, real_check = z3.Z3_solver_check, z3.Solver.check

    def load(ctx: object, solver: object) -> int:
        seen.loading += 1
        return int(real_load(ctx, solver))

    def check(self: z3.Solver, *assumptions: z3.ExprRef) -> z3.CheckSatResult:
        seen.checks += 1
        return real_check(self, *assumptions)

    monkeypatch.setattr(z3, "Z3_solver_check", load)
    monkeypatch.setattr(z3.Solver, "check", check)
    return seen


@pytest.fixture
def load_everything(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Load every pool one formula at a time, however short."""
    monkeypatch.setattr(solver_mod, "_LOAD_ABOVE", 0)
    yield


# --- what is loaded, and how often it is checked ------------------------------------------


def test_a_long_pool_is_checked_between_formulas_then_twice_as_a_whole(counts: _Counts) -> None:
    premises, claim = _chain(_LONG + 1)
    out = Z3Solver(30.0).check_entailment(premises, claim)
    assert (out.verdict, out.reason) == ("INVALID", "not_entailed")
    assert "r0" in out.counter_model
    # Every premise and the claim's definition, each but the first after a check.
    assert counts.loading == len(premises)
    assert counts.checks == 2


def test_a_pool_at_the_limit_is_asserted_in_one_call(counts: _Counts) -> None:
    premises, claim = _chain(solver_mod._LOAD_ABOVE + 1)
    assert len(premises) == solver_mod._LOAD_ABOVE
    out = Z3Solver(30.0).check_entailment(premises, claim)
    assert out.verdict == "INVALID"
    assert (counts.loading, counts.checks) == (0, 2)


def test_loading_stops_at_the_first_inconsistent_prefix(counts: _Counts) -> None:
    premises, claim = _chain(_LONG)
    premises.insert(3, SmtFormula("", "(< r0 r3)"))  # r0 > r1 + 1 > r2 + 2 > r3 + 3
    out = Z3Solver(30.0).check_entailment(premises, claim)
    assert (out.verdict, out.reason) == ("INVALID", "premises_inconsistent")
    assert counts.loading == 4  # the check after the 4th formula sees it
    assert counts.checks == 0


def test_an_inconsistency_in_the_last_premise_is_found(counts: _Counts) -> None:
    premises, claim = _chain(_LONG)
    premises.append(SmtFormula("", f"(< r0 r{_LONG - 1})"))
    out = Z3Solver(30.0).check_entailment(premises, claim)
    assert (out.verdict, out.reason) == ("INVALID", "premises_inconsistent")


def test_a_malformed_late_premise_is_reported_before_any_check(counts: _Counts) -> None:
    premises, claim = _chain(_LONG)
    premises.insert(1, SmtFormula("", "false"))
    premises.append(SmtFormula("", "(> r0 undeclared)"))
    out = Z3Solver(30.0).check_entailment(premises, claim)
    assert (out.verdict, out.reason) == ("UNGROUNDED", "formalization_failed")
    assert (counts.loading, counts.checks) == (0, 0)


def test_a_long_pool_that_entails_its_claim_is_valid(counts: _Counts) -> None:
    premises, claim = _chain(_LONG, claim=f"(> r0 (+ r{_LONG - 1} {_LONG - 2}.0))")
    out = Z3Solver(30.0).check_entailment(premises, claim)
    assert (out.verdict, out.reason) == ("VALID", "entailed")


# --- deadline and interrupt while loading ---------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.perf_counter = time.perf_counter

    def monotonic(self) -> float:
        return self.now


def test_loading_stops_once_the_deadline_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    loads = [0]
    real_load = z3.Z3_solver_check

    def load(ctx: object, solver: object) -> int:
        loads[0] += 1
        if loads[0] == 2:
            clock.now += 1.0  # the whole budget
        return int(real_load(ctx, solver))

    monkeypatch.setattr(solver_mod, "time", clock)
    monkeypatch.setattr(z3, "Z3_solver_check", load)
    premises, claim = _chain(_LONG)
    out = Z3Solver(1.0).check_entailment(premises, claim)
    assert (out.verdict, out.reason) == ("UNGROUNDED", "timeout")
    assert loads[0] == 2


def test_loading_stops_when_interrupted(monkeypatch: pytest.MonkeyPatch) -> None:
    interrupt = solver_mod._Interrupt()
    loads = [0]
    real_load = z3.Z3_solver_check

    def load(ctx: object, solver: object) -> int:
        loads[0] += 1
        if loads[0] == 3:
            interrupt.request()
        return int(real_load(ctx, solver))

    monkeypatch.setattr(z3, "Z3_solver_check", load)
    premises, claim = _chain(_LONG)
    solver_mod._worker.interrupt = interrupt
    try:
        out = Z3Solver(30.0).check_entailment(premises, claim)
    finally:
        solver_mod._worker.interrupt = None
    assert out is solver_mod._INTERRUPTED
    assert loads[0] == 3


def test_a_loading_check_never_gets_much_more_than_the_time_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each loading check "takes" 1% of the budget; the limit Z3 runs under is set again
    # once less than `_RESET_SHARE` of it is left, so it never exceeds what is left by more.
    clock = _Clock()
    timeout_s = 1.0
    deadline = clock.now + timeout_s
    limit = [0]
    sets = [0]
    over: list[tuple[int, int]] = []
    real_set, real_load = z3.Solver.set, z3.Z3_solver_check

    def record(self: z3.Solver, *args: object, **kwargs: object) -> None:
        if args and args[0] == "timeout":
            limit[0] = int(str(args[1]))
            sets[0] += 1
        real_set(self, *args, **kwargs)

    def load(ctx: object, solver: object) -> int:
        left_ms = int((deadline - clock.now) * solver_mod._MS_PER_S)
        if limit[0] > left_ms / solver_mod._RESET_SHARE + 1:
            over.append((limit[0], left_ms))
        clock.now += timeout_s / 100
        return int(real_load(ctx, solver))

    monkeypatch.setattr(solver_mod, "time", clock)
    monkeypatch.setattr(z3.Solver, "set", record)
    monkeypatch.setattr(z3, "Z3_solver_check", load)
    premises, claim = _chain(_LONG)
    out = Z3Solver(timeout_s).check_entailment(premises, claim)
    assert out.reason == "timeout"
    assert over == []
    assert sets[0] < 30  # about log(100) / log(1 / 0.9), not one per check


# --- the premise check (claim `true`) needs no entailment check (AUDIT-18 #7) ------------


def test_a_premise_check_is_decided_by_its_consistency_check(counts: _Counts) -> None:
    decls = "(declare-const p Bool)\n(declare-const q Bool)"
    premises = [SmtFormula(decls, "p"), SmtFormula(decls, "(=> p q)")]
    out = Z3Solver(5.0).check_entailment(premises, SmtFormula(decls, " true "))
    assert (out.verdict, out.reason) == ("VALID", "entailed")
    assert counts.checks == 1
    premises.append(SmtFormula(decls, "(not q)"))
    out = Z3Solver(5.0).check_entailment(premises, SmtFormula(decls, "true"))
    assert (out.verdict, out.reason) == ("INVALID", "premises_inconsistent")


def test_a_premise_check_with_little_time_left_is_still_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    real_check = z3.Solver.check

    def check(self: z3.Solver, *assumptions: z3.ExprRef) -> z3.CheckSatResult:
        out = real_check(self, *assumptions)
        clock.now += 0.9  # of a 1 s budget: the guard would want 0.45 s
        return out

    monkeypatch.setattr(solver_mod, "time", clock)
    monkeypatch.setattr(z3.Solver, "check", check)
    decls = "(declare-const p Bool)"
    out = Z3Solver(1.0).check_entailment([SmtFormula(decls, "p")], SmtFormula(decls, "true"))
    assert (out.verdict, out.reason) == ("VALID", "entailed")
    # Any other claim still gets the guard.
    out = Z3Solver(1.0).check_entailment([SmtFormula(decls, "p")], SmtFormula(decls, "p"))
    assert out.details == "too little time left for the entailment check"


# --- loading reaches the verdicts of the one-call path ----------------------------------


_XYZ: Final = "(declare-const x Int)\n(declare-const y Int)\n(declare-const z Int)"
_HM: Final = "(declare-fun H (Int) Bool)\n(declare-fun M (Int) Bool)"
_CASES: Final[dict[str, tuple[list[tuple[str, str]], tuple[str, str]]]] = {
    "entailed": ([(_XYZ, "(> x 1)"), (_XYZ, "(> y x)")], (_XYZ, "(> y 1)")),
    "not-entailed": ([(_XYZ, "(> x 1)"), (_XYZ, "(> y x)")], (_XYZ, "(> y 3)")),
    "inconsistent": ([(_XYZ, "(> x 1)"), (_XYZ, "(< x 0)")], (_XYZ, "(> y 3)")),
    "true": ([(_XYZ, "(> x 1)"), (_XYZ, "(> y x)")], (_XYZ, "true")),
    "quantified": (
        [
            (_HM, "(forall ((v Int)) (=> (H v) (M v)))"),
            (_HM, "(H 3)"),
        ],
        (_HM, "(M 3)"),
    ),
    "nonlinear": ([(_XYZ, "(= (* x y) 6)"), (_XYZ, "(> x 2)")], (_XYZ, "(< y 3)")),
    "malformed": ([(_XYZ, "(> x 1)"), (_XYZ, "(> x")], (_XYZ, "(> y 3)")),
    "no-premises": ([], (_XYZ, "(> y 3)")),
}


def _outcome(case: str) -> SolverOutcome:
    premises, (decls, claim) = _CASES[case]
    pool = [SmtFormula(d, a) for d, a in premises]
    out = Z3Solver(30.0).check_entailment(pool, SmtFormula(decls, claim))
    return SolverOutcome(out.verdict, out.reason, counter_model=str(bool(out.counter_model)))


@pytest.mark.parametrize("case", sorted(_CASES))
def test_loading_reaches_the_verdict_of_the_one_call_path(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    once = _outcome(case)
    monkeypatch.setattr(solver_mod, "_LOAD_ABOVE", 0)
    assert _outcome(case) == once


async def _verdicts() -> list[tuple[float, list[tuple[str, str | None]]]]:
    results = []
    for golden in ALL:
        trace, fake = build(golden)
        result = await Verifier(fake).verify(trace)
        steps: list[tuple[str, str | None]] = [(v.verdict, v.reason) for v in result.step_verdicts]
        results.append((result.consistency_score, steps))
    return results


async def test_loading_every_pool_keeps_every_golden_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    once = await _verdicts()
    monkeypatch.setattr(solver_mod, "_LOAD_ABOVE", 0)
    assert await _verdicts() == once


def test_concurrent_loads_reach_their_verdicts() -> None:
    premises, claim = _chain(_LONG)
    outcomes: list[SolverOutcome] = []
    workers = [
        threading.Thread(
            target=lambda: outcomes.append(Z3Solver(30.0).check_entailment(premises, claim))
        )
        for _ in range(3)
    ]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    assert [o.reason for o in outcomes] == ["not_entailed"] * 3
