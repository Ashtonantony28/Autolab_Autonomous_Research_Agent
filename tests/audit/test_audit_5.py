"""AUDIT-5: encoding soundness, round 2. Wrong traces that are still admitted.

Every test asserts the *correct* behaviour and is a strict xfail until its
finding is fixed; see `AUDIT-5.md`. All of them run on `FakeLLMClient`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest
import z3
from langgraph.types import Command

from autolab.agents.common import design_claim, design_facts, with_feedback
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
    HITLResolution,
    Hypothesis,
    ReasoningStep,
    ReasoningTrace,
    VerificationContext,
    VerificationResult,
)
from autolab.state.schema import AutoLabState, empty_state
from autolab.verification.faithfulness import faithfulness_prompt
from autolab.verification.formalizer import FormalizationError, Vocabulary, prompt_for
from autolab.verification.llm import FakeLLMClient
from autolab.verification.solver import SmtFormula
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import register_failing_trace, register_trace
from tests.fixtures.golden_traces import GoldenStep
from tests.integration.orchestrator import test_graph as tg


def _register(
    fake: FakeLLMClient,
    steps: Sequence[GoldenStep],
    *,
    facts: Sequence[FormalizedStep] = (),
    faithful: Sequence[bool] | None = None,
    agent: str = "audit",
) -> ReasoningTrace:
    """Register formalizations and judge verdicts exactly as the verifier will ask for them.

    A judge verdict of True is what an honest judge says: each assertion below is a
    verbatim encoding of its claim under the declarations the judge is shown.
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
            z3.parse_smt2_string(f"{candidate.smt()}\n(assert {gs.assertion})")
        except (FormalizationError, z3.Z3Exception):
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
    return ReasoningTrace(agent=agent, steps=tuple(reasoning))


def _passes(result: VerificationResult) -> bool:
    """What the monitor node and `BaseAgent` do with a verdict."""
    return VerificationMonitor().decide(result) is AgentDecision.PASS


# ---------------------------------------------------------------------------
# AUDIT-5 #1 (critical): a declaration may redeclare an SMT-LIB builtin (`>`, `<`,
# `not`, `=`, `and`, `=>`, ...). Z3 accepts it and the uninterpreted symbol shadows
# the builtin in every check, because each check declares the trace's final
# vocabulary, including the declarations of later and rejected steps.

ABSORBANCE = FormalizedStep(
    declarations="(declare-const absorbance Real)", assertion="(= absorbance 0.42)"
)
ACTIVE = "Compound K-17 is active"


async def test_shadowed_gt_hides_a_premise_that_contradicts_the_measurement() -> None:
    fake = FakeLLMClient()
    trace = _register(
        fake,
        [
            GoldenStep(
                "If absorbance exceeds 0.9 then compound K-17 is active",
                "premise",
                "(declare-const active Bool)",
                "(=> (> absorbance 0.9) active)",
            ),
            # Contradicts the measured fact absorbance = 0.42.
            GoldenStep("Absorbance exceeds 0.9", "premise", "", "(> absorbance 0.9)"),
            # The judge rejects this step, but its declarations stay in the vocabulary
            # and `>` is now uninterpreted in every check of the trace.
            GoldenStep(
                "The plate was read once",
                "premise",
                "(declare-const read_once Bool)\n(declare-fun > (Real Real) Bool)",
                "read_once",
            ),
            GoldenStep(ACTIVE, "conclusion", "", "active", (0, 1)),
        ],
        facts=(ABSORBANCE,),
        faithful=(True, True, False, True),
    )
    context = VerificationContext(facts=(ABSORBANCE,), payload_claim=ACTIVE)

    result = await Verifier(fake, check_faithfulness=True).verify(trace, context=context)

    # Today: step 1 is VALID/assumed, score 0.875, the monitor says PASS.
    assert result.step_verdicts[1].verdict == "INVALID"
    assert not _passes(result)


async def test_shadowed_lt_in_the_ignored_final_step_defeats_the_contradiction_trigger() -> None:
    prior = Hypothesis(question="q", statement="Yield exceeds 0.5", rationale="r")
    new = Hypothesis(question="q", statement="Yield is below 0.2", rationale="r")
    fake = FakeLLMClient()
    trace = contradiction_trace([prior], new)
    _register(
        fake,
        [
            GoldenStep(prior.statement, "premise", "(declare-const yld Real)", "(> yld 0.5)"),
            GoldenStep(new.statement, "premise", "", "(< yld 0.2)"),
            # The trigger ignores the final step's verdict, but not its declarations.
            GoldenStep(
                new.statement,
                "inference",
                "(declare-fun < (Real Real) Bool)",
                "(< yld 0.2)",
                (0, 1),
            ),
        ],
        faithful=(True, True, False),
    )
    assert [s.claim for s in trace.steps] == [prior.statement, new.statement, new.statement]
    state: AutoLabState = {**empty_state("q"), "hypotheses": [prior]}

    reason = await contradiction_trigger(Verifier(fake, check_faithfulness=True))(new, state)

    # Today: None, so a hypothesis that contradicts the one in state is admitted.
    assert reason is not None


async def test_shadowed_not_lets_a_restated_payload_premise_prove_itself() -> None:
    claim = "Compound K-17 cures disease D in humans"
    fake = FakeLLMClient()
    trace = _register(
        fake,
        [
            # A law of logic, encoded verbatim.
            GoldenStep(
                "For every proposition b, if b holds then not-b is false",
                "premise",
                "",
                "(forall ((b Bool)) (=> b (= (not b) false)))",
            ),
            # A paraphrase of the payload claim: circular (AUDIT-1 #1).
            GoldenStep(
                "K-17 cures disease D in people", "premise", "(declare-const cures Bool)", "cures"
            ),
            # Makes `not`, and so the solver's own `(assert (not claim))`, uninterpreted.
            GoldenStep(
                "The trial was registered",
                "premise",
                "(declare-const registered Bool)\n(declare-fun not (Bool) Bool)",
                "registered",
            ),
            GoldenStep(claim, "conclusion", "", "cures", (0, 1)),
        ],
        faithful=(True, True, False, True),
    )

    result = await Verifier(fake, check_faithfulness=True).verify(
        trace, context=VerificationContext(payload_claim=claim)
    )

    # Today: the circularity check finds no single premise that entails the
    # conclusion (`cures and not(cures)` is satisfiable), so the trace passes.
    assert not _passes(result)


# ---------------------------------------------------------------------------
# AUDIT-5 #2 (high): payload binding covers one text field. The Design trace
# proves `protocol`; the numeric fields the cost/duration trigger (principle 7b)
# and the simulator read are never checked against it.


async def test_design_cost_fields_that_contradict_the_verified_protocol_skip_the_pause() -> None:
    protocol = "Incubate the plate for 240 minutes using 500 USD of reagent"
    reply: dict[str, Any] = {
        "protocol": protocol,
        "estimated_cost_usd": 5.0,  # the protocol says 500 USD
        "estimated_duration_minutes": 30.0,  # the protocol says 240 minutes
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
                "The incubation takes 240 minutes and the reagent costs 500 USD",
                "premise",
                "(declare-const minutes Real)\n(declare-const usd Real)\n"
                "(declare-const chosen Bool)",
                "(and (= minutes 240.0) (= usd 500.0))",
            ),
            GoldenStep(
                "If the incubation takes 240 minutes and costs 500 USD, that protocol is chosen",
                "premise",
                "",
                "(=> (and (= minutes 240.0) (= usd 500.0)) chosen)",
            ),
            GoldenStep(protocol, "conclusion", "", "chosen", (0, 1)),
        ),
    )
    agent = DesignAgent(verifier=Verifier(fake), llm=fake, hitl_trigger=cost_trigger(10.0, 60.0))
    state: AutoLabState = {**empty_state(tg.QUESTION), "hypotheses": [tg.HYPOTHESIS]}

    update = await agent(state)

    # Before SG12: the trace verifies (score 1.0), the trigger reads $5 / 30 min, there is
    # no pause, and a design whose own verified protocol costs $500 over 4 h is merged.
    # SG12 binds the trace to `design_claim`, which this bare-protocol conclusion no
    # longer states, so the assertion `== 1.0` became `< 1.0` (see AUDIT-5.md #2).
    assert update["confidence"] < 1.0
    assert update["hitl_pending"] is not None
    assert "current_experiment" not in update


# ---------------------------------------------------------------------------
# AUDIT-5 #3 (medium): `approve` on a verification escalation merges the payload
# without running the agent's policy trigger, so a $50 design that failed
# verification reaches Execute with no cost pause ever raised.


def _fail_expensive_design(fake: FakeLLMClient, feedback: str = "") -> None:
    fake.register(
        "design.experiment",
        with_feedback(build_design_prompt(tg.HYPOTHESIS), feedback),
        tg.EXPENSIVE.model_dump_json(),
    )
    register_failing_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=with_feedback(
            build_design_trace_prompt(tg.HYPOTHESIS, tg.EXPENSIVE), feedback
        ),
        conclusion=design_claim(tg.EXPENSIVE),
        facts=design_facts(tg.EXPENSIVE),
    )


async def test_approving_a_verification_escalation_still_applies_the_cost_trigger() -> None:
    fake = FakeLLMClient()
    tg._seed_full_run(fake, tg.EXPENSIVE)
    _fail_expensive_design(fake)
    _fail_expensive_design(fake, feedback=tg.FAILING_SUMMARY)
    graph, config = tg._graph(fake)

    paused = await graph.ainvoke(empty_state(tg.QUESTION), config=config)
    pause = tg._interrupt(paused)
    assert pause["payload"]["trigger"] == "verification"
    assert "$" not in pause["reason"]  # the human is asked about the reasoning only

    after: dict[str, Any] = await graph.ainvoke(
        Command(resume=HITLResolution(action="approve")), config=config
    )

    # Before SG12: the run completes; Execute ran a $50 design and no cost pause was raised.
    assert "__interrupt__" in after
    assert tg._interrupt(after)["payload"]["trigger"] == "policy"


# ---------------------------------------------------------------------------
# AUDIT-5 #4 (medium): the contradiction trigger only finds a contradiction that
# is unsat in the formalizer's vocabulary. Faithful atomic encodings of opposite
# effects ("increases" / "decreases") share no symbol and no background axiom, so
# a hypothesis that flatly contradicts the one in state is admitted.


@pytest.mark.xfail(strict=True, reason="AUDIT-5 #4")
async def test_opposite_effect_hypothesis_is_flagged_as_a_contradiction() -> None:
    prior = Hypothesis(question="q", statement="Additive X increases crystal yield", rationale="r")
    new = Hypothesis(question="q", statement="Additive X decreases crystal yield", rationale="r")
    fake = FakeLLMClient()
    _register(
        fake,
        [
            GoldenStep(
                prior.statement,
                "premise",
                "(declare-const x_increases_yield Bool)",
                "x_increases_yield",
            ),
            GoldenStep(
                new.statement,
                "premise",
                "(declare-const x_decreases_yield Bool)",
                "x_decreases_yield",
            ),
            GoldenStep(new.statement, "inference", "", "x_decreases_yield", (0, 1)),
        ],
        faithful=(True, True, True),
    )
    state: AutoLabState = {**empty_state("q"), "hypotheses": [prior]}

    reason = await contradiction_trigger(Verifier(fake, check_faithfulness=True))(new, state)

    # Today: None. Every formula is faithful and the premises are jointly satisfiable.
    assert reason is not None


# ---------------------------------------------------------------------------
# AUDIT-5 #5 (low): payload binding casefolds, but case carries meaning in
# scientific text (Co cobalt / CO carbon monoxide, mS / ms, BRCA1 / Brca1). A
# trace that proves the carbon monoxide claim is bound to the cobalt hypothesis.


@pytest.mark.xfail(strict=True, reason="AUDIT-5 #5")
async def test_binding_does_not_conflate_cobalt_with_carbon_monoxide() -> None:
    payload = "Co exposure inhibits enzyme E"  # cobalt
    proved = "CO exposure inhibits enzyme E"  # carbon monoxide
    fake = FakeLLMClient()
    trace = _register(
        fake,
        [
            GoldenStep(
                "CO binds the heme site of enzyme E",
                "premise",
                "(declare-const co_binds_heme Bool)\n(declare-const co_inhibits_e Bool)",
                "co_binds_heme",
            ),
            GoldenStep(
                "If CO binds the heme site of enzyme E, CO exposure inhibits enzyme E",
                "premise",
                "",
                "(=> co_binds_heme co_inhibits_e)",
            ),
            GoldenStep(proved, "conclusion", "", "co_inhibits_e", (0, 1)),
        ],
        faithful=(True, True, True),
    )

    result = await Verifier(fake, check_faithfulness=True).verify(
        trace, context=VerificationContext(payload_claim=payload)
    )

    # Today: the conclusion is treated as the payload claim and the trace scores 1.0.
    assert not _passes(result)
