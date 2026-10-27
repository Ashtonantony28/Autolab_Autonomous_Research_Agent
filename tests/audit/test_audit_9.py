"""AUDIT-9: encoding soundness, round 3. Wrong traces that are still admitted.

Every test asserts the *correct* behaviour and is a strict xfail until its
finding is fixed; see `AUDIT-9.md`. All of them run on `FakeLLMClient`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest
import z3

from autolab.agents.common import design_claim, design_facts
from autolab.agents.verification_monitor import AgentDecision, VerificationMonitor
from autolab.state.models import (
    ExperimentDesign,
    ExperimentParameter,
    FormalizedStep,
    ReasoningStep,
    ReasoningTrace,
    VerificationContext,
    VerificationResult,
)
from autolab.verification.faithfulness import faithfulness_prompt
from autolab.verification.formalizer import FormalizationError, Vocabulary, prompt_for
from autolab.verification.llm import FakeLLMClient
from autolab.verification.solver import SmtFormula
from autolab.verification.vericot import Verifier
from tests.fixtures.golden_traces import GoldenStep


def _register(
    fake: FakeLLMClient,
    steps: Sequence[GoldenStep],
    *,
    facts: Sequence[FormalizedStep] = (),
    faithful: Sequence[bool] | None = None,
) -> ReasoningTrace:
    """Register formalizations and judge verdicts exactly as the verifier will ask for them.

    A judge verdict of True is what an honest judge says: each assertion below states
    its claim under the declarations the judge is shown.
    """
    vocab = Vocabulary()
    for fact in facts:
        vocab.merge(fact.declarations)
    reasoning = []
    for i, gs in enumerate(steps):
        step = ReasoningStep(
            index=i,
            claim=gs.claim,
            claim_type=gs.claim_type,
            referenced_step_indices=gs.referenced_step_indices,
        )
        reasoning.append(step)
        fake.register(
            "formalize",
            prompt_for(step, vocab),
            json.dumps({"declarations": gs.declarations, "assertion": gs.assertion}),
        )
        try:
            candidate = vocab.merged(gs.declarations)
            parsed = z3.parse_smt2_string(
                f"{candidate.smt()}\n(assert {gs.assertion})", ctx=z3.Context()
            )
        except (FormalizationError, z3.Z3Exception):
            continue
        if len(parsed) != 1:  # the formalizer rejects an assertion that is not one term
            continue
        if faithful is not None:
            fake.register(
                "faithfulness",
                faithfulness_prompt(step, SmtFormula(candidate.smt(), gs.assertion)),
                json.dumps(
                    {"back_translation": gs.claim, "faithful": faithful[i], "explanation": ""}
                ),
            )
        vocab = candidate
    return ReasoningTrace(agent="audit", steps=tuple(reasoning))


def _passes(result: VerificationResult) -> bool:
    """What the monitor node and `BaseAgent` do with a verdict."""
    return VerificationMonitor().decide(result) is AgentDecision.PASS


ABSORBANCE = FormalizedStep(
    declarations="(declare-const absorbance Real)", assertion="(= absorbance 0.42)"
)
ACTIVE = "Compound K-17 is active"


# ---------------------------------------------------------------------------
# AUDIT-9 #1 (critical): `RESERVED_SYMBOLS` is hand-written, and Z3 builtins outside
# it (strings, sequences, bit-vectors, floating point, sets, trig, `~`, ...) can still
# be redeclared. The shadow applies to every check of the trace (AUDIT-5 #1 again).

SPECIFIC = "The primer binds specifically"


async def test_shadowed_str_len_hides_a_premise_that_contradicts_another() -> None:
    fake = FakeLLMClient()
    trace = _register(
        fake,
        [
            GoldenStep(
                "The primer sequence is GAATTCAAGG",
                "premise",
                "(declare-const primer String)",
                '(= primer "GAATTCAAGG")',
            ),
            # Contradicts step 0: that sequence has 10 bases.
            GoldenStep(
                "The primer is longer than 20 bases", "premise", "", "(> (str.len primer) 20)"
            ),
            GoldenStep(
                "If the primer is longer than 20 bases then it binds specifically",
                "premise",
                "(declare-const specific Bool)",
                "(=> (> (str.len primer) 20) specific)",
            ),
            # A later step redeclares the builtin `str.len`; `str.len` is not reserved.
            GoldenStep(
                "The PCR was run once",
                "premise",
                "(declare-const ran_once Bool)\n(declare-fun str.len (String) Int)",
                "ran_once",
            ),
            GoldenStep(SPECIFIC, "conclusion", "", "specific", (1, 2)),
        ],
        # Every judge verdict is honest: each assertion states its claim.
        faithful=(True, True, True, True, True),
    )
    context = VerificationContext(payload_claim=SPECIFIC)

    result = await Verifier(fake, check_faithfulness=True).verify(trace, context=context)

    # Today: step 1 is VALID/assumed, the conclusion is VALID, score 1.0, PASS.
    assert result.step_verdicts[1].reason == "premises_inconsistent"
    assert not _passes(result)


@pytest.mark.parametrize(
    "decl",
    [
        "(declare-fun str.len (String) Int)",
        "(declare-fun str.prefixof (String String) Bool)",
        "(declare-fun seq.len ((Seq Int)) Int)",
        "(declare-fun bvult ((_ BitVec 8) (_ BitVec 8)) Bool)",
        "(declare-fun fp.lt (Real Real) Bool)",
        "(declare-fun subset (Real Real) Bool)",
        "(declare-fun sin (Real) Real)",
        "(declare-fun ~ (Real) Real)",
    ],
)
def test_vocabulary_rejects_every_z3_builtin(decl: str) -> None:
    with pytest.raises(FormalizationError):
        Vocabulary().merged(decl)


# ---------------------------------------------------------------------------
# AUDIT-9 #2 (high): the `assertion` field can carry SMT commands. A premise is only
# ever parsed inside `(assert X)`, so `X = "a) (cmd ...) (assert b"` is accepted. The
# declaration-shape check (AUDIT-1 #2) and the reserved-symbol check (AUDIT-5 #1) look
# only at `declarations`.


async def test_assertion_cannot_smuggle_a_builtin_redeclaration() -> None:
    fake = FakeLLMClient()
    trace = _register(
        fake,
        [
            GoldenStep(
                "If absorbance exceeds 0.9 then compound K-17 is active",
                "premise",
                "(declare-const active Bool)",
                # Redeclares `=` after its own assertion: every later `=`, including the
                # code-built measurement fact, is now uninterpreted.
                "(=> (> absorbance 0.9) active)) (declare-fun = (Real Real) Bool) (assert true",
            ),
            # Contradicts the measured fact absorbance = 0.42.
            GoldenStep("Absorbance exceeds 0.9", "premise", "", "(> absorbance 0.9)"),
            GoldenStep(ACTIVE, "conclusion", "", "active", (0, 1)),
        ],
        facts=(ABSORBANCE,),
        # As in AUDIT-1 #2: the judge approves, because the asserted proposition does
        # state the claim.
        faithful=(True, True, True),
    )
    context = VerificationContext(facts=(ABSORBANCE,), payload_claim=ACTIVE)

    result = await Verifier(fake, check_faithfulness=True).verify(trace, context=context)

    # Today: every step is VALID, score 1.0, PASS.
    assert result.step_verdicts[1].reason == "premises_inconsistent"
    assert not _passes(result)


async def test_a_trace_cannot_change_z3_global_parameters() -> None:
    fake = FakeLLMClient()
    poisoned = _register(
        fake,
        [
            GoldenStep(
                "The plate was read once",
                "premise",
                "(declare-const read_once Bool)",
                # `set-option` in a parsed string sets Z3's process-wide parameters.
                "read_once) (set-option :rlimit 1) (assert read_once",
            ),
            GoldenStep("The plate was read", "conclusion", "", "read_once", (0,)),
        ],
    )
    # A later, unrelated and honest run in the same process.
    honest = _register(
        fake,
        [
            GoldenStep("The lid was closed", "premise", "(declare-const lid Bool)", "lid"),
            GoldenStep(
                "If the lid was closed then the sample stayed dry",
                "premise",
                "(declare-const dry Bool)",
                "(=> lid dry)",
            ),
            GoldenStep("The sample stayed dry", "conclusion", "", "dry", (0, 1)),
        ],
    )
    z3.reset_params()  # `_register`'s own parse check runs the set-option too.
    try:
        await Verifier(fake).verify(poisoned)
        later = await Verifier(fake).verify(honest)
    finally:
        z3.reset_params()

    # Today: rlimit stays 1 for the rest of the process, so every check of the honest
    # run returns `unknown` (UNGROUNDED/timeout).
    assert [v.verdict for v in later.step_verdicts] == ["VALID", "VALID", "VALID"]


# ---------------------------------------------------------------------------
# AUDIT-9 #3 (high): circularity overrides come from pass-1 verdicts only. A premise
# that was INVALID in pass 1 (it contradicted a premise that pass 2 drops) is
# VALID/`assumed` in the pass-2 re-check and is never checked for circularity. A drop
# with a non-blocking reason (`timeout`, i.e. Z3 `unknown`) lets that premise prove the
# payload claim (AUDIT-1 #1 again).


async def test_premise_inconsistent_in_pass_one_cannot_prove_the_claim_in_pass_two() -> None:
    fake = FakeLLMClient()
    trace = _register(
        fake,
        [
            # With the measurement this entails `active`; alone, ruling out `not active`
            # needs Z3 to decide 2^r = 3, which it answers `unknown` (non-blocking).
            GoldenStep(
                "The well is not contaminated, and either compound K-17 is active or the "
                "absorbance is not 0.42 and two to the power r equals three",
                "premise",
                "(declare-const active Bool)\n(declare-const contaminated Bool)\n"
                "(declare-const r Real)",
                "(and (not contaminated) (or active (and (not (= absorbance 0.42)) "
                "(= (^ 2.0 r) 3.0))))",
            ),
            # Contains the payload claim (circular), and contradicts step 0 in pass 1.
            GoldenStep(
                "The well is contaminated and compound K-17 is active",
                "premise",
                "",
                "(and contaminated active)",
            ),
            GoldenStep(ACTIVE, "conclusion", "", "active", (0, 1)),
        ],
        facts=(ABSORBANCE,),
        faithful=(True, True, True),
    )
    context = VerificationContext(facts=(ABSORBANCE,), payload_claim=ACTIVE)

    result = await Verifier(fake, check_faithfulness=True).verify(trace, context=context)

    # Today: [UNGROUNDED/timeout, VALID/assumed, VALID], score 0.83, PASS. Step 1 alone
    # entails the conclusion, and nothing ever checks it.
    assert result.step_verdicts[1].verdict != "VALID"
    assert not _passes(result)


# ---------------------------------------------------------------------------
# AUDIT-9 #4 (low): `design_facts` / `design_claim` go through `float(p.value)`, so an
# integer parameter above 2**53 is bound to a different value than the payload's, and
# one above ~1e308 raises `OverflowError`, which `escalate_permanent_errors` does not
# catch.

BIG = 2**53 + 1


def _design(value: int) -> ExperimentDesign:
    return ExperimentDesign(
        hypothesis_id="00000000-0000-0000-0000-000000000000",
        protocol="Seed cells and read the plate",
        parameters=(ExperimentParameter(name="cell_count", value=value),),
        estimated_cost_usd=5.0,
        estimated_duration_minutes=30.0,
    )


@pytest.mark.xfail(strict=True, reason="AUDIT-9 #4")
def test_integer_parameter_is_bound_to_its_exact_value() -> None:
    design = _design(BIG)
    fact = design_facts(design)[-1]

    # Today: "(= param_cell_count 9007199254740992.0)" and "... = 9007199254740992.0".
    assert str(BIG) in fact.assertion
    assert f"param_cell_count = {BIG}" in design_claim(design)


@pytest.mark.xfail(strict=True, reason="AUDIT-9 #4")
def test_huge_integer_parameter_is_a_permanent_error() -> None:
    # Today: OverflowError, which is not a ValueError, so the graph crashes instead of
    # pausing with an `error` request.
    with pytest.raises(ValueError):
        design_facts(_design(10**400))
