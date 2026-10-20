"""AUDIT-13: encoding soundness, round 4. Wrong traces that are still admitted.

Every test asserts the *correct* behaviour and is a strict xfail until its
finding is fixed; see `AUDIT-13.md`. All of them run on `FakeLLMClient`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from autolab.agents.common import design_claim, design_facts
from autolab.agents.design import DesignAgent, build_design_prompt, build_design_trace_prompt
from autolab.agents.verification_monitor import AgentDecision, VerificationMonitor
from autolab.orchestrator.checkpoints import (
    contradiction_trace,
    contradiction_trigger,
    cost_trigger,
)
from autolab.state.models import (
    ExperimentDesign,
    FormalizedStep,
    Hypothesis,
    ReasoningStep,
    ReasoningTrace,
    VerificationContext,
    VerificationResult,
)
from autolab.state.schema import AutoLabState, empty_state
from autolab.verification.faithfulness import faithfulness_prompt
from autolab.verification.formalizer import (
    FormalizationError,
    Formalizer,
    Vocabulary,
    prompt_for,
)
from autolab.verification.llm import FakeLLMClient
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import register_trace
from tests.fixtures.golden_traces import GoldenStep
from tests.integration.orchestrator import test_graph as tg


async def _register_steps(
    fake: FakeLLMClient,
    steps: Sequence[ReasoningStep],
    formulas: Sequence[tuple[str, str]],
    *,
    facts: Sequence[FormalizedStep] = (),
) -> None:
    """Register each step's formalization and an honest judge verdict (faithful).

    The judge is shown the step's claim, the vocabulary so far and the assertion;
    each assertion below states its claim under those declarations. A shadow
    `Formalizer` decides which steps are accepted, so the registration follows
    the formalizer under test (AUDIT-20 #2).
    """
    shadow = FakeLLMClient()
    formalizer = Formalizer(shadow)
    for fact in facts:
        formalizer.vocabulary.merge(fact.declarations)
    for step, (decls, assertion) in zip(steps, formulas, strict=True):
        prompt = prompt_for(step, formalizer.vocabulary)
        reply = json.dumps({"declarations": decls, "assertion": assertion})
        fake.register("formalize", prompt, reply)
        shadow.register("formalize", prompt, reply)
        # As in the verifier, a rejected step is not judged and does not extend the vocabulary.
        try:
            formula = await formalizer.formalize(step)
        except FormalizationError:
            continue
        fake.register(
            "faithfulness",
            faithfulness_prompt(step, formula),
            json.dumps({"back_translation": step.claim, "faithful": True, "explanation": ""}),
        )


async def _register(
    fake: FakeLLMClient, steps: Sequence[GoldenStep], *, facts: Sequence[FormalizedStep] = ()
) -> ReasoningTrace:
    reasoning = tuple(
        ReasoningStep(
            index=i,
            claim=gs.claim,
            claim_type=gs.claim_type,
            referenced_step_indices=gs.referenced_step_indices,
        )
        for i, gs in enumerate(steps)
    )
    await _register_steps(
        fake, reasoning, [(gs.declarations, gs.assertion) for gs in steps], facts=facts
    )
    return ReasoningTrace(agent="audit", steps=reasoning)


def _passes(result: VerificationResult) -> bool:
    """What the monitor node and `BaseAgent` do with a verdict."""
    return VerificationMonitor().decide(result) is AgentDecision.PASS


ABSORBANCE = FormalizedStep(
    declarations="(declare-const absorbance Real)", assertion="(= absorbance 0.42)"
)
ACTIVE = "Compound K-17 is active"


# ---------------------------------------------------------------------------
# AUDIT-13 #1 (critical): `_is_z3_builtin` probes a name only as a constant and as
# a sort. Z3's `equals` and `equiv` are builtin aliases of `=` that exist only with
# arguments, so both probes say "unknown" and the name may be declared. A later
# step's declaration then turns every `equals` in the trace into an uninterpreted
# predicate (AUDIT-5 #1 / AUDIT-9 #1 again).


async def test_shadowed_equals_hides_a_premise_that_contradicts_a_measurement() -> None:
    fake = FakeLLMClient()
    trace = await _register(
        fake,
        [
            # Contradicts the measured fact absorbance = 0.42. When step 0 is
            # formalized, `equals` is Z3's builtin equality.
            GoldenStep("Absorbance is 0.95", "premise", "", "(equals absorbance 0.95)"),
            GoldenStep(
                "If absorbance is 0.95 then compound K-17 is active",
                "premise",
                "(declare-const active Bool)",
                "(=> (equals absorbance 0.95) active)",
            ),
            # A later step redeclares the builtin `equals`.
            GoldenStep(
                "The plate was read once",
                "premise",
                "(declare-const read_once Bool)\n(declare-fun equals (Real Real) Bool)",
                "read_once",
            ),
            GoldenStep(ACTIVE, "conclusion", "", "active", (0, 1)),
        ],
        facts=(ABSORBANCE,),
    )
    context = VerificationContext(facts=(ABSORBANCE,), payload_claim=ACTIVE)

    result = await Verifier(fake, check_faithfulness=True).verify(trace, context=context)

    # Today: every step is VALID, score 1.0, PASS.
    assert result.step_verdicts[0].reason == "premises_inconsistent"
    assert not _passes(result)


@pytest.mark.parametrize(
    "decl",
    [
        "(declare-fun equals (Real Real) Bool)",
        "(declare-fun equiv (Real Real) Bool)",
        "(declare-fun if_then_else (Bool Bool Bool) Bool)",
    ],
)
def test_vocabulary_rejects_builtins_that_only_exist_with_arguments(decl: str) -> None:
    with pytest.raises(FormalizationError):
        Vocabulary().merged(decl)


# ---------------------------------------------------------------------------
# AUDIT-13 #2 (critical): `_is_single_term` accepts `(! t :named n)` for any n.
# In the solver document a `:named` premise defines `n` for every assertion after
# it, and Z3 lets that definition shadow the builtin constant `false` (also `pi`,
# `euler`, rounding modes). The formalizer parses each step alone, so the judge
# and the parse check see Z3's `false`; the checks see the premise's term.


async def test_named_premise_cannot_redefine_false_for_later_steps() -> None:
    fake = FakeLLMClient()
    trace = await _register(
        fake,
        [
            # The proposition asserted is `plate_read`; `:named` only labels it.
            GoldenStep(
                "The plate was read",
                "premise",
                "(declare-const plate_read Bool)",
                "(! plate_read :named false)",
            ),
            GoldenStep(
                "The plate reader was calibrated",
                "premise",
                "(declare-const calibrated Bool)",
                "calibrated",
            ),
            # Under Z3's `false` this is just `active`, which steps 0-1 do not entail.
            GoldenStep(
                ACTIVE,
                "conclusion",
                "(declare-const active Bool)",
                "(or active (and false calibrated))",
                (0, 1),
            ),
        ],
    )
    context = VerificationContext(payload_claim=ACTIVE)

    result = await Verifier(fake, check_faithfulness=True).verify(trace, context=context)

    # Today: [VALID/assumed, VALID/assumed, VALID], score 1.0, PASS.
    assert result.step_verdicts[-1].verdict != "VALID"
    assert not _passes(result)


async def test_named_prior_hypothesis_cannot_hide_a_contradiction() -> None:
    prior = Hypothesis(question=tg.QUESTION, statement=ACTIVE, rationale="r")
    new = Hypothesis(question=tg.QUESTION, statement="Compound K-17 is not active", rationale="r")
    trace = contradiction_trace([prior], new)
    fake = FakeLLMClient()
    await _register_steps(
        fake,
        trace.steps,
        [
            ("(declare-const active Bool)", "(! active :named false)"),
            ("", "(= active false)"),
            ("", "(= active false)"),
        ],
    )
    trigger = contradiction_trigger(Verifier(fake, check_faithfulness=True))
    state: AutoLabState = {**empty_state(tg.QUESTION), "hypotheses": [prior]}

    # Today: None. The two statements are jointly unsat, yet no principle-7c pause.
    assert await trigger(new, state) is not None


# ---------------------------------------------------------------------------
# AUDIT-13 #3 (high): `protocol_figures` (SG17) misses common ways of writing a
# duration or a cost: compound adjectives ("240-minute"), weeks, a thousands
# suffix ("5k USD"), "US dollars", a trailing "$" and currencies outside its list.
# The AUDIT-12 #1 attack then admits a $5 / 30 min design whose protocol costs and
# takes far more, and the cost/duration pause (principle 7b) never fires.


@pytest.mark.parametrize(
    "protocol",
    [
        "Incubate the plate in one 240-minute run using the reagent kit",
        "Culture the cells for 2 weeks using the reagent kit",
        "Incubate the plate once using 5k USD of reagent",
        "Incubate the plate once using 500 US dollars of reagent",
        "Incubate the plate once using 500$ of reagent",
        "Incubate the plate once using 40000 INR of reagent",
    ],
)
async def test_protocol_figures_cannot_hide_cost_or_duration(protocol: str) -> None:
    reply: dict[str, Any] = {
        "protocol": protocol,
        "estimated_cost_usd": 5.0,
        "estimated_duration_minutes": 30.0,
    }
    fake = FakeLLMClient()
    fake.register("design.experiment", build_design_prompt(tg.HYPOTHESIS), json.dumps(reply))
    design = ExperimentDesign(hypothesis_id=tg.HYPOTHESIS.id, **reply)
    register_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(tg.HYPOTHESIS, design),
        steps=(
            GoldenStep(
                "The protocol takes 240 minutes and its reagent costs 500 USD",
                "premise",
                "(declare-const minutes Real)\n(declare-const usd Real)\n"
                "(declare-const chosen Bool)",
                "(and (= minutes 240.0) (= usd 500.0))",
            ),
            GoldenStep(
                "If the protocol takes 240 minutes and costs 500 USD, it is chosen",
                "premise",
                "",
                "(=> (and (= minutes 240.0) (= usd 500.0)) chosen)",
            ),
            GoldenStep(design_claim(design), "conclusion", "", "chosen", (0, 1)),
        ),
        facts=design_facts(design),
    )
    agent = DesignAgent(verifier=Verifier(fake), llm=fake, hitl_trigger=cost_trigger(10.0, 60.0))
    state: AutoLabState = {**empty_state(tg.QUESTION), "hypotheses": [tg.HYPOTHESIS]}

    update = await agent(state)

    # Today: confidence 1.0, no pause, and the $5 / 30 min design is merged.
    assert update["hitl_pending"] is not None
    assert "current_experiment" not in update
