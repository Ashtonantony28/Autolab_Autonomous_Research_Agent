"""Golden reasoning traces for the verifier.

The original 20 (10 valid, 10 invalid) plus the SG1 soundness set:
explosion, dilution, poisoned chains, wrong citations, quantifier and
arithmetic edge cases, long valid chains and vocabulary conflicts.

Each entry is a self-contained scenario that the Verifier should classify
correctly, step by step, when given the FakeLLMClient that `build` seeds
from the same scenario's formalizations.

A scenario is a dataclass rather than YAML to keep types tight and editing
trivial. The on-disk YAML cost wasn't worth its weight in this phase.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import z3

from autolab.state.models import ClaimType, ReasoningStep, ReasoningTrace
from autolab.verification.formalizer import FormalizationError, Vocabulary, prompt_for
from autolab.verification.llm import FakeLLMClient


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
    # Optional per-step `StepVerdict.reason`; empty means "not checked".
    expected_reasons: tuple[str, ...] = ()


def build(golden: GoldenTrace, agent: str = "golden") -> tuple[ReasoningTrace, FakeLLMClient]:
    """Return the trace and a FakeLLMClient keyed on the prompts the formalizer will send.

    Mirrors the formalizer's vocabulary rule: a step's declarations join the
    vocabulary only if they merge cleanly and the step's SMT parses.
    """
    fake = FakeLLMClient()
    steps: list[ReasoningStep] = []
    vocab = Vocabulary()
    for i, gs in enumerate(golden.steps):
        step = ReasoningStep(
            index=i,
            claim=gs.claim,
            claim_type=gs.claim_type,
            referenced_step_indices=gs.referenced_step_indices,
        )
        steps.append(step)
        fake.register(
            "formalize",
            prompt_for(step, vocab),
            json.dumps({"declarations": gs.declarations, "assertion": gs.assertion}),
        )
        try:
            candidate = vocab.merged(gs.declarations)
            z3.parse_smt2_string(f"{candidate.smt()}\n(assert {gs.assertion})")
        except (FormalizationError, z3.Z3Exception):
            continue
        vocab = candidate
    return ReasoningTrace(agent=agent, steps=tuple(steps)), fake


def _overall(verdicts: tuple[str, ...]) -> str:
    return "INVALID" if "INVALID" in verdicts else "VALID"


def _g(
    id: str,
    description: str,
    verdicts: tuple[str, ...],
    *steps: GoldenStep,
    reasons: tuple[str, ...] = (),
) -> GoldenTrace:
    return GoldenTrace(id, description, _overall(verdicts), verdicts, steps, reasons)


def _p(decls: str, assertion: str, claim: str = "premise") -> GoldenStep:
    return GoldenStep(claim, "premise", decls, assertion)


def _i(assertion: str, *refs: int, decls: str = "", claim: str = "inference") -> GoldenStep:
    return GoldenStep(claim, "inference", decls, assertion, refs)


def _c(assertion: str, *refs: int, decls: str = "", claim: str = "conclusion") -> GoldenStep:
    return GoldenStep(claim, "conclusion", decls, assertion, refs)


def _bools(*names: str) -> str:
    return "\n".join(f"(declare-const {n} Bool)" for n in names)


_V, _I, _U = "VALID", "INVALID", "UNGROUNDED"
_PQ = _bools("P", "Q")
_PRED = "(declare-fun H (Int) Bool)\n(declare-fun M (Int) Bool)\n(declare-const a Int)"
_REL = "(declare-fun R (Int Int) Bool)"
_X_INT = "(declare-const x Int)"
_X_REAL = "(declare-const x Real)"


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


# --------------------------------------------------------------------------
# SG1 soundness set.
# --------------------------------------------------------------------------

_AS, _EN, _NE, _PI = "assumed", "entailed", "not_entailed", "premises_inconsistent"
_NP, _FF = "no_premises", "formalization_failed"

EXPLOSION: tuple[GoldenTrace, ...] = (
    _g(
        "e01-explosion-bool",
        "P and not P must not entail an unrelated Q.",
        (_V, _V, _I),
        _p(_PQ, "P"),
        _p("", "(not P)"),
        _c("Q"),
        reasons=(_AS, _AS, _PI),
    ),
    _g(
        "e02-explosion-arith",
        "x > 5 and x < 3 must not entail x = 100.",
        (_V, _V, _I),
        _p(_X_INT, "(> x 5)"),
        _p("", "(< x 3)"),
        _c("(= x 100)", 0, 1),
        reasons=(_AS, _AS, _PI),
    ),
    _g(
        "e03-explosion-quantifier",
        "forall x. H(x) together with not H(a) must not entail M(a).",
        (_V, _V, _I),
        _p(_PRED, "(forall ((x Int)) (H x))"),
        _p("", "(not (H a))"),
        _c("(M a)"),
        reasons=(_AS, _AS, _PI),
    ),
    _g(
        "e04-explosion-parity",
        "2x = 7 has no integer solution, so it must not entail x > 1000.",
        (_V, _I),
        _p(_X_INT, "(= (* 2 x) 7)"),
        _c("(> x 1000)", 0),
        reasons=(_AS, _PI),
    ),
)

DILUTION: tuple[GoldenTrace, ...] = (
    _g(
        "d01-dilution-five-premises",
        "Five true premises do not support an unrelated conclusion.",
        (_V, _V, _V, _V, _V, _I),
        _p(_bools("A", "B", "C", "D", "E", "F"), "A"),
        _p("", "B"),
        _p("", "C"),
        _p("", "D"),
        _p("", "E"),
        _c("F"),
    ),
    _g(
        "d02-dilution-arith",
        "Four positive numbers: x + y > z + w does not follow.",
        (_V, _V, _V, _V, _I),
        _p(
            "(declare-const x Int)\n(declare-const y Int)\n"
            "(declare-const z Int)\n(declare-const w Int)",
            "(> x 0)",
        ),
        _p("", "(> y 0)"),
        _p("", "(> z 0)"),
        _p("", "(> w 0)"),
        _c("(> (+ x y) (+ z w))"),
    ),
    _g(
        "d03-dilution-chain-overreach",
        "A, A->B, B->C, C->D entail D but not D and E.",
        (_V, _V, _V, _V, _I),
        _p(_bools("A", "B", "C", "D", "E"), "A"),
        _p("", "(=> A B)"),
        _p("", "(=> B C)"),
        _p("", "(=> C D)"),
        _c("(and D E)"),
    ),
    _g(
        "d04-dilution-hidden-converse",
        "A->B, C->D, B, D do not entail A or C (affirming the consequent).",
        (_V, _V, _V, _V, _I),
        _p(_bools("A", "B", "C", "D"), "(=> A B)"),
        _p("", "(=> C D)"),
        _p("", "B"),
        _p("", "D"),
        _c("(or A C)"),
    ),
    _g(
        "d05-dilution-valid-tail",
        "One bad inference among valid steps still sinks the trace.",
        (_V, _V, _V, _V, _V, _I, _V),
        _p(_bools("A", "B", "C", "D", "E"), "A"),
        _p("", "(=> A B)"),
        _p("", "C"),
        _p("", "D"),
        _i("B", 0, 1),
        _i("E"),
        _c("(and B C D)"),
    ),
)

POISONED: tuple[GoldenTrace, ...] = (
    _g(
        "p01-poisoned-restatement",
        "An unsupported inference Q cannot be restated as a conclusion.",
        (_V, _I, _I),
        _p(_PQ, "P"),
        _i("Q"),
        _c("Q"),
        reasons=(_AS, _NE, _NE),
    ),
    _g(
        "p02-poisoned-modus-ponens",
        "Q is not entailed, so Q -> R cannot carry R.",
        (_V, _V, _I, _I),
        _p(_bools("P", "Q", "R"), "P"),
        _p("", "(=> Q R)"),
        _i("Q"),
        _c("R"),
    ),
    _g(
        "p03-poisoned-arith",
        "x > 0; the unsupported x > 10 cannot be used to conclude x > 5.",
        (_V, _I, _I),
        _p(_X_INT, "(> x 0)"),
        _i("(> x 10)", 0),
        _c("(> x 5)"),
    ),
    _g(
        "p04-poisoned-cited",
        "Citing an INVALID step contributes nothing to the pool.",
        (_V, _I, _I),
        _p(_PQ, "P"),
        _i("Q", 0),
        _c("(and P Q)", 0, 1),
        reasons=(_AS, _NE, _NE),
    ),
    _g(
        "p05-poisoned-diluted",
        "A bad inference followed by a valid conclusion is still capped below pass.",
        (_V, _V, _V, _I, _V),
        _p(_bools("P", "Q", "R", "S"), "P"),
        _p("", "R"),
        _p("", "S"),
        _i("Q"),
        _c("P"),
    ),
)

WRONG_CITATIONS: tuple[GoldenTrace, ...] = (
    _g(
        "w01-cites-irrelevant-premise",
        "Q follows from steps 0 and 1, but the step cites only step 2.",
        (_V, _V, _V, _I),
        _p(_bools("P", "Q", "R"), "P"),
        _p("", "(=> P Q)"),
        _p("", "R"),
        _c("Q", 2),
        reasons=(_AS, _AS, _AS, _NE),
    ),
    _g(
        "w02-cites-wrong-bound",
        "x > 3 follows from x >= 5, but the step cites y < 0.",
        (_V, _V, _I),
        _p("(declare-const x Int)\n(declare-const y Int)", "(>= x 5)"),
        _p("", "(< y 0)"),
        _c("(> x 3)", 1),
    ),
    _g(
        "w03-cites-only-invalid-step",
        "The only cited step was refuted, so the conclusion has no premises.",
        (_V, _I, _U),
        _p(_PQ, "P"),
        _i("Q", 0),
        _c("Q", 1),
        reasons=(_AS, _NE, _NP),
    ),
    _g(
        "w04-correct-citations",
        "Q is entailed by the two steps it cites.",
        (_V, _V, _V, _V),
        _p(_bools("P", "Q", "R"), "P"),
        _p("", "(=> P Q)"),
        _p("", "R"),
        _c("Q", 0, 1),
        reasons=(_AS, _AS, _AS, _EN),
    ),
    _g(
        "w05-cites-incomplete-support",
        "(P and Q) -> R needs Q, which the step does not cite.",
        (_V, _V, _V, _I),
        _p(_bools("P", "Q", "R"), "P"),
        _p("", "Q"),
        _p("", "(=> (and P Q) R)"),
        _c("R", 0, 2),
    ),
)

QUANTIFIERS: tuple[GoldenTrace, ...] = (
    _g(
        "q01-universal-instantiation",
        "forall x. H(x) entails H(a).",
        (_V, _V),
        _p(_PRED, "(forall ((x Int)) (H x))"),
        _c("(H a)", 0),
    ),
    _g(
        "q02-existential-to-instance",
        "exists x. H(x) does not entail H(a).",
        (_V, _I),
        _p(_PRED, "(exists ((x Int)) (H x))"),
        _c("(H a)", 0),
    ),
    _g(
        "q03-exists-forall-swap",
        "exists y forall x R(x,y) entails forall x exists y R(x,y).",
        (_V, _V),
        _p(_REL, "(exists ((y Int)) (forall ((x Int)) (R x y)))"),
        _c("(forall ((x Int)) (exists ((y Int)) (R x y)))", 0),
    ),
    _g(
        "q04-forall-exists-swap",
        "forall x exists y R(x,y) does not entail exists y forall x R(x,y).",
        (_V, _I),
        _p(_REL, "(forall ((x Int)) (exists ((y Int)) (R x y)))"),
        _c("(exists ((y Int)) (forall ((x Int)) (R x y)))", 0),
    ),
    _g(
        "q05-vacuous-antecedent",
        "forall x. H(x) -> M(x) and not H(a) do not entail M(a).",
        (_V, _V, _I),
        _p(_PRED, "(forall ((x Int)) (=> (H x) (M x)))"),
        _p("", "(not (H a))"),
        _c("(M a)"),
    ),
    _g(
        "q06-universal-chain",
        "forall H and forall H -> M entail M(a) and then forall M.",
        (_V, _V, _V, _V),
        _p(_PRED, "(forall ((x Int)) (H x))"),
        _p("", "(forall ((x Int)) (=> (H x) (M x)))"),
        _i("(M a)", 0, 1),
        _c("(forall ((x Int)) (M x))", 0, 1),
    ),
)

ARITHMETIC: tuple[GoldenTrace, ...] = (
    _g(
        "a01-int-interval",
        "Over the integers, 2 < x < 4 entails x = 3.",
        (_V, _V),
        _p(_X_INT, "(and (> x 2) (< x 4))"),
        _c("(= x 3)", 0),
    ),
    _g(
        "a02-real-interval",
        "Over the reals, 2 < x < 4 does not entail x = 3.",
        (_V, _I),
        _p(_X_REAL, "(and (> x 2.0) (< x 4.0))"),
        _c("(= x 3.0)", 0),
    ),
    _g(
        "a03-linear-system",
        "x + y = 10 and x - y = 2 entail x = 6, then y = 4.",
        (_V, _V, _V, _V),
        _p("(declare-const x Int)\n(declare-const y Int)", "(= (+ x y) 10)"),
        _p("", "(= (- x y) 2)"),
        _i("(= x 6)", 0, 1),
        _c("(= y 4)", 0, 2),
    ),
    _g(
        "a04-square-nonnegative",
        "y = x * x over the reals entails y >= 0.",
        (_V, _V),
        _p("(declare-const x Real)\n(declare-const y Real)", "(= y (* x x))"),
        _c("(>= y 0.0)", 0),
    ),
    _g(
        "a05-off-by-one",
        "x >= 3 does not entail x > 3.",
        (_V, _I),
        _p(_X_INT, "(>= x 3)"),
        _c("(> x 3)", 0),
    ),
    _g(
        "a06-real-division",
        "2x = 7 over the reals entails x = 3.5.",
        (_V, _V),
        _p(_X_REAL, "(= (* 2.0 x) 7.0)"),
        _c("(= x 3.5)", 0),
    ),
    _g(
        "a07-int-strict-bound",
        "Over the integers, x > 3 entails x >= 4.",
        (_V, _V),
        _p(_X_INT, "(> x 3)"),
        _c("(>= x 4)", 0),
    ),
)

CHAINS: tuple[GoldenTrace, ...] = (
    _g(
        "m01-hypothetical-syllogism",
        "A, A->B, B->C; infer B, then C, then B and C.",
        (_V, _V, _V, _V, _V, _V),
        _p(_bools("A", "B", "C"), "A"),
        _p("", "(=> A B)"),
        _p("", "(=> B C)"),
        _i("B", 0, 1),
        _i("C", 2, 3),
        _c("(and B C)", 3, 4),
    ),
    _g(
        "m02-arith-chain",
        "x >= 2, y = 2x, z = y + 1; infer y >= 4, z >= 5, then z > x.",
        (_V, _V, _V, _V, _V, _V),
        _p("(declare-const x Int)\n(declare-const y Int)\n(declare-const z Int)", "(>= x 2)"),
        _p("", "(= y (* 2 x))"),
        _p("", "(= z (+ y 1))"),
        _i("(>= y 4)", 0, 1),
        _i("(>= z 5)", 2, 3),
        _c("(> z x)", 0, 1, 2),
    ),
    _g(
        "m03-quantified-chain",
        "All H are M, all M are D, a is H; infer M(a), D(a), then both.",
        (_V, _V, _V, _V, _V, _V),
        _p(
            _PRED + "\n(declare-fun D (Int) Bool)",
            "(forall ((x Int)) (=> (H x) (M x)))",
        ),
        _p("", "(forall ((x Int)) (=> (M x) (D x)))"),
        _p("", "(H a)"),
        _i("(M a)", 0, 2),
        _i("(D a)", 1, 3),
        _c("(and (M a) (D a))", 3, 4),
    ),
    _g(
        "m04-assay-chain-uncited",
        "Inhibitor binds -> activity drops -> signal drops; uncited steps use all prior.",
        (_V, _V, _V, _V, _V, _V),
        _p(_bools("Bind", "Drop", "Signal"), "Bind", claim="the compound binds the enzyme"),
        _p("", "(=> Bind Drop)", claim="binding reduces activity"),
        _p("", "(=> Drop Signal)", claim="reduced activity lowers the signal"),
        _i("Drop", claim="activity drops"),
        _i("Signal", claim="the signal drops"),
        _c("(and Bind Signal)", claim="binding explains the signal drop"),
    ),
    _g(
        "m05-mixed-bool-arith",
        "P, P -> x > 5; infer x > 5, then x > 0, then (not P) or x > 0.",
        (_V, _V, _V, _V, _V),
        _p("(declare-const P Bool)\n" + _X_INT, "P"),
        _p("", "(=> P (> x 5))"),
        _i("(> x 5)", 0, 1),
        _i("(> x 0)", 2),
        _c("(or (not P) (> x 0))", 3),
    ),
)

VOCAB_CONFLICTS: tuple[GoldenTrace, ...] = (
    _g(
        "c01-conflict-does-not-leak",
        "A step re-declaring P fails whole; its other symbol Q never joins the vocabulary.",
        (_V, _U, _I),
        _p("(declare-const P Bool)", "P"),
        _p("(declare-const P Int)\n(declare-const Q Bool)", "(=> (> P 0) Q)"),
        _c("Q", decls="(declare-const Q Bool)"),
        reasons=(_AS, _FF, _NE),
    ),
    _g(
        "c02-conflict-irrelevant-step",
        "A conflicting re-declaration on a side step leaves the main chain valid.",
        (_V, _V, _U, _V),
        _p(_PQ, "(=> P Q)"),
        _p("", "P"),
        _i("(> Q 0)", decls="(declare-const Q Int)"),
        _c("Q", 0, 1),
        reasons=(_AS, _AS, _FF, _EN),
    ),
    _g(
        "c03-parse-failure-does-not-leak",
        "A step whose SMT fails to parse does not add its symbol Z to the vocabulary.",
        (_V, _U, _V),
        _p("(declare-const P Bool)", "P"),
        _i("(and P Z", decls="(declare-const Z Bool)"),
        _c("(or P Z)", 0, decls="(declare-const Z Bool)"),
        reasons=(_AS, _FF, _EN),
    ),
)

SOUNDNESS: tuple[GoldenTrace, ...] = (
    *EXPLOSION,
    *DILUTION,
    *POISONED,
    *WRONG_CITATIONS,
    *QUANTIFIERS,
    *ARITHMETIC,
    *CHAINS,
    *VOCAB_CONFLICTS,
)

ALL: tuple[GoldenTrace, ...] = (*VALID, *INVALID, *SOUNDNESS)
