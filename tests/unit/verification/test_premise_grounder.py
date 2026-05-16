"""Premise grounder tests."""

from __future__ import annotations

from autolab.state.models import ReasoningStep
from autolab.verification.premise_grounder import premises_for
from autolab.verification.solver import SmtFormula


def _step(index: int) -> ReasoningStep:
    return ReasoningStep(index=index, claim="c", claim_type="inference")


def test_only_prior_steps_are_included() -> None:
    formalized = {
        0: SmtFormula("(declare-const p Bool)", "p"),
        1: SmtFormula("(declare-const q Bool)", "q"),
        2: SmtFormula("(declare-const r Bool)", "r"),
    }
    out = premises_for(_step(2), formalized)
    assertions = [f.assertion for f in out]
    assert assertions == ["p", "q"]


def test_step_zero_has_no_premises() -> None:
    formalized: dict[int, SmtFormula] = {}
    assert premises_for(_step(0), formalized) == []


def test_extra_premises_appended_and_deduplicated() -> None:
    formalized = {0: SmtFormula("(declare-const p Bool)", "p")}
    extra = [
        SmtFormula("(declare-const c Bool)", "c"),
        SmtFormula("(declare-const p Bool)", "p"),  # duplicate of step 0
    ]
    out = premises_for(_step(1), formalized, extra=extra)
    assertions = [f.assertion for f in out]
    assert assertions == ["p", "c"]


def test_self_and_future_indices_ignored() -> None:
    formalized = {
        0: SmtFormula("(declare-const a Bool)", "a"),
        2: SmtFormula("(declare-const b Bool)", "b"),  # future
    }
    out = premises_for(_step(1), formalized)
    assertions = [f.assertion for f in out]
    assert assertions == ["a"]
