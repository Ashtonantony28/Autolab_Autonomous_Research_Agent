"""Z3 wrapper behaviour."""

from __future__ import annotations

from autolab.verification.solver import SmtFormula, Z3Solver


def _bool(name: str) -> str:
    return f"(declare-const {name} Bool)"


def test_no_premises_returns_ungrounded() -> None:
    s = Z3Solver()
    out = s.check_entailment(premises=[], claim=SmtFormula(declarations=_bool("P"), assertion="P"))
    assert out.verdict == "UNGROUNDED"


def test_tautology_is_valid() -> None:
    s = Z3Solver()
    out = s.check_entailment(
        premises=[SmtFormula(_bool("P"), "P")],
        claim=SmtFormula(_bool("P"), "P"),
    )
    assert out.verdict == "VALID"


def test_modus_ponens() -> None:
    s = Z3Solver()
    decls = "\n".join((_bool("P"), _bool("Q")))
    out = s.check_entailment(
        premises=[
            SmtFormula(decls, "P"),
            SmtFormula(decls, "(=> P Q)"),
        ],
        claim=SmtFormula(decls, "Q"),
    )
    assert out.verdict == "VALID"


def test_invalid_inference_is_detected() -> None:
    s = Z3Solver()
    decls = "\n".join((_bool("P"), _bool("Q")))
    out = s.check_entailment(
        premises=[SmtFormula(decls, "P")],
        claim=SmtFormula(decls, "Q"),
    )
    assert out.verdict == "INVALID"
    assert out.counter_model  # Z3 returns a model


def test_contradictory_premises_entail_anything() -> None:
    # ex falso quodlibet: from a contradiction anything follows.
    s = Z3Solver()
    out = s.check_entailment(
        premises=[
            SmtFormula(_bool("P"), "P"),
            SmtFormula(_bool("P"), "(not P)"),
        ],
        claim=SmtFormula(_bool("Q") + "\n" + _bool("P"), "Q"),
    )
    assert out.verdict == "VALID"


def test_arithmetic_entailment() -> None:
    s = Z3Solver()
    decl = "(declare-const x Int)"
    out = s.check_entailment(
        premises=[SmtFormula(decl, "(> x 0)")],
        claim=SmtFormula(decl, "(> (+ x 1) 1)"),
    )
    assert out.verdict == "VALID"


def test_quantified_entailment() -> None:
    s = Z3Solver()
    decl = "(declare-fun Human (Int) Bool)\n(declare-fun Mortal (Int) Bool)"
    out = s.check_entailment(
        premises=[
            SmtFormula(decl, "(forall ((x Int)) (=> (Human x) (Mortal x)))"),
            SmtFormula(decl + "\n(declare-const socrates Int)", "(Human socrates)"),
        ],
        claim=SmtFormula(decl + "\n(declare-const socrates Int)", "(Mortal socrates)"),
    )
    assert out.verdict == "VALID"


def test_malformed_smt_returns_ungrounded() -> None:
    s = Z3Solver()
    out = s.check_entailment(
        premises=[SmtFormula(_bool("P"), "P")],
        claim=SmtFormula("", "this is not smt"),
    )
    assert out.verdict == "UNGROUNDED"
    assert "parse error" in out.details.lower()


def test_short_timeout_returns_ungrounded_on_hard_problem() -> None:
    # A non-linear integer arithmetic problem Z3 is likely to time out on quickly.
    s = Z3Solver(timeout_seconds=0.001)
    decl = "(declare-const x Int)\n(declare-const y Int)\n(declare-const z Int)"
    hard = "(and (> x 1) (> y 1) (> z 1) (= (+ (* x x x) (* y y y)) (* z z z)))"
    out = s.check_entailment(
        premises=[SmtFormula(decl, hard)],
        claim=SmtFormula(decl, "false"),
    )
    # Either UNGROUNDED (timeout) or INVALID (Z3 finds counter-model fast).
    # The test's intent is "no crash, no exception" rather than a specific verdict.
    assert out.verdict in {"UNGROUNDED", "INVALID"}
