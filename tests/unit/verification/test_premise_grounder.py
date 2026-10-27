"""Premise grounder tests."""

from __future__ import annotations

from autolab.state.models import ReasoningStep
from autolab.verification.premise_grounder import premises_for
from autolab.verification.solver import SmtFormula


def _step(index: int, refs: tuple[int, ...] = ()) -> ReasoningStep:
    return ReasoningStep(
        index=index, claim="c", claim_type="inference", referenced_step_indices=refs
    )


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


def test_references_restrict_pool_to_cited_steps() -> None:
    formalized = {
        0: SmtFormula("(declare-const a Bool)", "a"),
        1: SmtFormula("(declare-const b Bool)", "b"),
        2: SmtFormula("(declare-const c Bool)", "c"),
    }
    extra = [SmtFormula("(declare-const f Bool)", "f")]
    out = premises_for(_step(3, refs=(2, 0)), formalized, extra=extra)
    assert [f.assertion for f in out] == ["a", "c", "f"]


def test_references_to_unverified_steps_are_dropped() -> None:
    # Step 1 is absent from `formalized` (it was not VALID), so citing it adds nothing.
    formalized = {0: SmtFormula("(declare-const a Bool)", "a")}
    assert premises_for(_step(2, refs=(1,)), formalized) == []
