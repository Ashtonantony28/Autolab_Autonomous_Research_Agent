"""Twenty golden reasoning traces for the verifier — 10 valid, 10 invalid.

Each entry is a self-contained scenario that the Verifier should classify
correctly when given a FakeLLMClient pre-seeded from the same scenario's
formalizations.

A scenario is a dataclass rather than YAML to keep types tight and editing
trivial. The on-disk YAML cost wasn't worth its weight in this phase.
"""

from __future__ import annotations

from dataclasses import dataclass

from autolab.state.models import ClaimType


@dataclass(frozen=True)
class GoldenStep:
    claim: str
    claim_type: ClaimType
    declarations: str
    assertion: str
    referenced_step_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class GoldenTrace:
    id: str
    description: str
    expected_overall: str  # "VALID" or "INVALID"
    expected_step_verdicts: tuple[str, ...]
    steps: tuple[GoldenStep, ...]


# --------------------------------------------------------------------------
# 10 VALID traces — every non-premise step is entailed.
# --------------------------------------------------------------------------

VALID: tuple[GoldenTrace, ...] = (
    GoldenTrace(
        id="v01-modus-ponens",
        description="Classical modus ponens: P, P->Q ⊨ Q.",
        expected_overall="VALID",
        expected_step_verdicts=("VALID", "VALID", "VALID"),
        steps=(
            GoldenStep("P holds", "premise", "(declare-const P Bool)", "P"),
            GoldenStep("If P then Q", "premise", "(declare-const Q Bool)", "(=> P Q)"),
            GoldenStep("Therefore Q", "conclusion", "", "Q", referenced_step_indices=(0, 1)),
        ),
    ),
    GoldenTrace(
        id="v02-syllogism",
        description="Universal instantiation: all H are M; s is H; therefore s is M.",
        expected_overall="VALID",
        expected_step_verdicts=("VALID", "VALID", "VALID"),
        steps=(
            GoldenStep(
                "All humans are mortal",
                "premise",
                "(declare-fun Human (Int) Bool)\n(declare-fun Mortal (Int) Bool)",
                "(forall ((x Int)) (=> (Human x) (Mortal x)))",
            ),
            GoldenStep(
                "Socrates is human",
                "premise",
                "(declare-const socrates Int)",
                "(Human socrates)",
            ),
            GoldenStep(
                "Therefore Socrates is mortal",
                "conclusion",
                "",
                "(Mortal socrates)",
                referenced_step_indices=(0, 1),
            ),
        ),
    ),
    GoldenTrace(
        id="v03-transitivity",
        description="Transitivity of <.",
        expected_overall="VALID",
        expected_step_verdicts=("VALID", "VALID", "VALID"),
        steps=(
            GoldenStep(
                "a < b",
                "premise",
                "(declare-const a Int)\n(declare-const b Int)",
                "(< a b)",
            ),
            GoldenStep(
                "b < c",
                "premise",
                "(declare-const c Int)",
                "(< b c)",
            ),
            GoldenStep(
                "Therefore a < c",
                "conclusion",
                "",
                "(< a c)",
                referenced_step_indices=(0, 1),
            ),
        ),
    ),
    GoldenTrace(
        id="v04-arith-positive-plus-one",
        description="x > 0 entails x + 1 > 1.",
        expected_overall="VALID",
        expected_step_verdicts=("VALID", "VALID"),
        steps=(
            GoldenStep("x is positive", "premise", "(declare-const x Int)", "(> x 0)"),
            GoldenStep(
                "Therefore x + 1 > 1",
                "conclusion",
                "",
                "(> (+ x 1) 1)",
                referenced_step_indices=(0,),
            ),
        ),
    ),
    GoldenTrace(
        id="v05-and-elimination",
        description="From (P and Q) we may conclude P.",
        expected_overall="VALID",
        expected_step_verdicts=("VALID", "VALID"),
        steps=(
            GoldenStep(
                "P and Q hold",
                "premise",
                "(declare-const P Bool)\n(declare-const Q Bool)",
                "(and P Q)",
            ),
            GoldenStep("Therefore P holds", "conclusion", "", "P", referenced_step_indices=(0,)),
        ),
    ),
    GoldenTrace(
        id="v06-or-introduction",
        description="From P we may conclude (P or Q).",
        expected_overall="VALID",
        expected_step_verdicts=("VALID", "VALID"),
        steps=(
            GoldenStep("P holds", "premise", "(declare-const P Bool)\n(declare-const Q Bool)", "P"),
            GoldenStep(
                "Therefore P or Q", "conclusion", "", "(or P Q)", referenced_step_indices=(0,)
            ),
        ),
    ),
    GoldenTrace(
        id="v07-double-negation",
        description="Not (not P) entails P.",
        expected_overall="VALID",
        expected_step_verdicts=("VALID", "VALID"),
        steps=(
            GoldenStep("not (not P)", "premise", "(declare-const P Bool)", "(not (not P))"),
            GoldenStep("Therefore P", "conclusion", "", "P", referenced_step_indices=(0,)),
        ),
    ),
    GoldenTrace(
        id="v08-equality-substitution",
        description="x = y and (x > 0) entail (y > 0).",
        expected_overall="VALID",
        expected_step_verdicts=("VALID", "VALID", "VALID"),
        steps=(
            GoldenStep(
                "x = y",
                "premise",
                "(declare-const x Int)\n(declare-const y Int)",
                "(= x y)",
            ),
            GoldenStep("x > 0", "premise", "", "(> x 0)"),
            GoldenStep(
                "Therefore y > 0", "conclusion", "", "(> y 0)", referenced_step_indices=(0, 1)
            ),
        ),
    ),
    GoldenTrace(
        id="v09-contrapositive",
        description="P->Q and not Q entail not P.",
        expected_overall="VALID",
        expected_step_verdicts=("VALID", "VALID", "VALID"),
        steps=(
            GoldenStep(
                "P implies Q",
                "premise",
                "(declare-const P Bool)\n(declare-const Q Bool)",
                "(=> P Q)",
            ),
            GoldenStep("Q does not hold", "premise", "", "(not Q)"),
            GoldenStep(
                "Therefore P does not hold",
                "conclusion",
                "",
                "(not P)",
                referenced_step_indices=(0, 1),
            ),
        ),
    ),
    GoldenTrace(
        id="v10-nonempty-bound",
        description="x >= 5 entails x > 3.",
        expected_overall="VALID",
        expected_step_verdicts=("VALID", "VALID"),
        steps=(
            GoldenStep("x is at least 5", "premise", "(declare-const x Int)", "(>= x 5)"),
            GoldenStep(
                "Therefore x > 3", "conclusion", "", "(> x 3)", referenced_step_indices=(0,)
            ),
        ),
    ),
)


# --------------------------------------------------------------------------
# 10 INVALID traces — at least one non-premise step is NOT entailed.
# --------------------------------------------------------------------------

INVALID: tuple[GoldenTrace, ...] = (
    GoldenTrace(
        id="i01-affirming-the-consequent",
        description="P->Q, Q does NOT entail P.",
        expected_overall="INVALID",
        expected_step_verdicts=("VALID", "VALID", "INVALID"),
        steps=(
            GoldenStep(
                "P implies Q",
                "premise",
                "(declare-const P Bool)\n(declare-const Q Bool)",
                "(=> P Q)",
            ),
            GoldenStep("Q holds", "premise", "", "Q"),
            GoldenStep(
                "Therefore P holds",
                "conclusion",
                "",
                "P",
                referenced_step_indices=(0, 1),
            ),
        ),
    ),
    GoldenTrace(
        id="i02-denying-the-antecedent",
        description="P->Q, not P does NOT entail not Q.",
        expected_overall="INVALID",
        expected_step_verdicts=("VALID", "VALID", "INVALID"),
        steps=(
            GoldenStep(
                "P implies Q",
                "premise",
                "(declare-const P Bool)\n(declare-const Q Bool)",
                "(=> P Q)",
            ),
            GoldenStep("Not P", "premise", "", "(not P)"),
            GoldenStep(
                "Therefore not Q",
                "conclusion",
                "",
                "(not Q)",
                referenced_step_indices=(0, 1),
            ),
        ),
    ),
    GoldenTrace(
        id="i03-unrelated",
        description="From P alone we cannot conclude unrelated Q.",
        expected_overall="INVALID",
        expected_step_verdicts=("VALID", "INVALID"),
        steps=(
            GoldenStep("P holds", "premise", "(declare-const P Bool)\n(declare-const Q Bool)", "P"),
            GoldenStep("Therefore Q", "conclusion", "", "Q", referenced_step_indices=(0,)),
        ),
    ),
    GoldenTrace(
        id="i04-arith-wrong",
        description="x > 0 does NOT entail x > 5.",
        expected_overall="INVALID",
        expected_step_verdicts=("VALID", "INVALID"),
        steps=(
            GoldenStep("x > 0", "premise", "(declare-const x Int)", "(> x 0)"),
            GoldenStep(
                "Therefore x > 5", "conclusion", "", "(> x 5)", referenced_step_indices=(0,)
            ),
        ),
    ),
    GoldenTrace(
        id="i05-existential-overreach",
        description="Some H are M does not entail all H are M.",
        expected_overall="INVALID",
        expected_step_verdicts=("VALID", "INVALID"),
        steps=(
            GoldenStep(
                "Some humans are mortal",
                "premise",
                "(declare-fun Human (Int) Bool)\n(declare-fun Mortal (Int) Bool)",
                "(exists ((x Int)) (and (Human x) (Mortal x)))",
            ),
            GoldenStep(
                "Therefore all humans are mortal",
                "conclusion",
                "",
                "(forall ((x Int)) (=> (Human x) (Mortal x)))",
                referenced_step_indices=(0,),
            ),
        ),
    ),
    GoldenTrace(
        id="i06-and-from-or",
        description="(P or Q) does NOT entail (P and Q).",
        expected_overall="INVALID",
        expected_step_verdicts=("VALID", "INVALID"),
        steps=(
            GoldenStep(
                "P or Q",
                "premise",
                "(declare-const P Bool)\n(declare-const Q Bool)",
                "(or P Q)",
            ),
            GoldenStep(
                "Therefore P and Q",
                "conclusion",
                "",
                "(and P Q)",
                referenced_step_indices=(0,),
            ),
        ),
    ),
    GoldenTrace(
        id="i07-direction-reversed",
        description="x < y does NOT entail x > y.",
        expected_overall="INVALID",
        expected_step_verdicts=("VALID", "INVALID"),
        steps=(
            GoldenStep(
                "x < y",
                "premise",
                "(declare-const x Int)\n(declare-const y Int)",
                "(< x y)",
            ),
            GoldenStep(
                "Therefore x > y",
                "conclusion",
                "",
                "(> x y)",
                referenced_step_indices=(0,),
            ),
        ),
    ),
    GoldenTrace(
        id="i08-equality-flipped",
        description="x = y + 1 does NOT entail x = y.",
        expected_overall="INVALID",
        expected_step_verdicts=("VALID", "INVALID"),
        steps=(
            GoldenStep(
                "x = y + 1",
                "premise",
                "(declare-const x Int)\n(declare-const y Int)",
                "(= x (+ y 1))",
            ),
            GoldenStep(
                "Therefore x = y", "conclusion", "", "(= x y)", referenced_step_indices=(0,)
            ),
        ),
    ),
    GoldenTrace(
        id="i09-negation-confusion",
        description="P does NOT entail not P.",
        expected_overall="INVALID",
        expected_step_verdicts=("VALID", "INVALID"),
        steps=(
            GoldenStep("P holds", "premise", "(declare-const P Bool)", "P"),
            GoldenStep(
                "Therefore not P",
                "conclusion",
                "",
                "(not P)",
                referenced_step_indices=(0,),
            ),
        ),
    ),
    GoldenTrace(
        id="i10-bound-wrong-direction",
        description="x <= 10 does NOT entail x = 10.",
        expected_overall="INVALID",
        expected_step_verdicts=("VALID", "INVALID"),
        steps=(
            GoldenStep("x <= 10", "premise", "(declare-const x Int)", "(<= x 10)"),
            GoldenStep(
                "Therefore x = 10",
                "conclusion",
                "",
                "(= x 10)",
                referenced_step_indices=(0,),
            ),
        ),
    ),
)


ALL: tuple[GoldenTrace, ...] = (*VALID, *INVALID)
