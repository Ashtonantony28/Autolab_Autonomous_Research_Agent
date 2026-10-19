"""SG7: a bound trace must prove its payload claim from consistent, non-circular premises."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from autolab.agents.research import ResearchAgent, build_hypothesis_prompt, build_trace_prompt
from autolab.agents.verification_monitor import AgentDecision, VerificationMonitor
from autolab.config import PASS_THRESHOLD
from autolab.state.models import (
    Citation,
    FormalizedStep,
    Hypothesis,
    ReasoningTrace,
    VerificationContext,
    VerificationResult,
)
from autolab.state.schema import empty_state
from autolab.verification.llm import FakeLLMClient
from autolab.verification.solver import SmtFormula, SolverOutcome, Z3Solver
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import content, register_trace
from tests.fixtures.golden_traces import GoldenStep

CLAIM = "C holds"
BOUND = VerificationContext(payload_claim=CLAIM)
_PC = "(declare-const p Bool)\n(declare-const c Bool)"
_P = GoldenStep("P holds", "premise", _PC, "p")
_P_IMPLIES_C = GoldenStep("P implies C", "premise", "", "(=> p c)")


def _trace(
    *steps: GoldenStep, facts: tuple[FormalizedStep, ...] = ()
) -> tuple[ReasoningTrace, FakeLLMClient]:
    fake = FakeLLMClient()
    trace = register_trace(
        fake, agent="t", trace_purpose="t.trace", trace_prompt="", steps=steps, facts=facts
    )
    return trace, fake


def _reasons(result: VerificationResult) -> list[str]:
    return [f"{v.verdict}:{v.reason}" for v in result.step_verdicts]


def _passes(result: VerificationResult) -> bool:
    return VerificationMonitor().decide(result) is AgentDecision.PASS


async def test_genuinely_entailed_conclusion_passes() -> None:
    trace, fake = _trace(_P, _P_IMPLIES_C, GoldenStep(CLAIM, "conclusion", "", "c", (0, 1)))
    result = await Verifier(fake).verify(trace, context=BOUND)
    assert _reasons(result) == ["VALID:assumed", "VALID:assumed", "VALID:entailed"]
    assert result.consistency_score == 1.0 and _passes(result)


@pytest.mark.parametrize(
    "premise",
    [
        GoldenStep("C is the case", "premise", _PC, "c"),
        GoldenStep("C and P", "premise", _PC, "(and c p)"),
        GoldenStep("not not C", "premise", _PC, "(not (not c))"),
    ],
    ids=["same-formula", "conjunction", "equivalent"],
)
async def test_premise_entailing_the_conclusion_alone_is_circular(premise: GoldenStep) -> None:
    trace, fake = _trace(premise, GoldenStep(CLAIM, "conclusion", "", "c", (0,)))
    result = await Verifier(fake).verify(trace, context=BOUND)
    assert _reasons(result) == ["UNGROUNDED:no_premises", "UNGROUNDED:no_premises"]
    assert "circular" in result.step_verdicts[0].details
    assert result.consistency_score < PASS_THRESHOLD and not _passes(result)


async def test_premise_restating_the_claim_text_is_circular_whatever_its_formula() -> None:
    trace, fake = _trace(
        GoldenStep("  c HOLDS ", "premise", _PC, "p"),
        _P_IMPLIES_C,
        GoldenStep(CLAIM, "conclusion", "", "c", (0, 1)),
    )
    result = await Verifier(fake).verify(trace, context=BOUND)
    assert result.step_verdicts[0].details == "premise restates the payload claim (circular)"
    assert _reasons(result)[2] == "INVALID:not_entailed"
    assert not _passes(result)


async def test_circular_premise_cannot_be_laundered_through_an_inference() -> None:
    trace, fake = _trace(
        GoldenStep("C is the case", "premise", _PC, "c"),
        GoldenStep("so C again", "inference", "", "c", (0,)),
        GoldenStep(CLAIM, "conclusion", "", "c", (1,)),
    )
    result = await Verifier(fake).verify(trace, context=BOUND)
    assert _reasons(result) == ["UNGROUNDED:no_premises"] * 3
    assert not _passes(result)


async def test_circular_shape_is_only_rejected_for_a_bound_trace() -> None:
    trace, fake = _trace(
        GoldenStep("C is the case", "premise", _PC, "c"), GoldenStep(CLAIM, "conclusion", "", "c")
    )
    result = await Verifier(fake).verify(trace)
    assert _reasons(result) == ["VALID:assumed", "VALID:entailed"]


_X = FormalizedStep(declarations="(declare-const x Real)", assertion="(= x 2.0)")


async def test_restating_a_measurement_fact_keeps_the_conclusion_proven_by_the_fact() -> None:
    trace, fake = _trace(
        GoldenStep("x was 2", "premise", "", "(= x 2.0)"),
        GoldenStep(CLAIM, "conclusion", "", "(> x 1.0)", (0,)),
        facts=(_X,),
    )
    result = await Verifier(fake).verify(
        trace, context=VerificationContext(facts=(_X,), payload_claim=CLAIM)
    )
    assert _reasons(result) == ["UNGROUNDED:no_premises", "VALID:entailed"]


async def test_tautological_conclusion_is_ungrounded_even_with_facts() -> None:
    trace, fake = _trace(
        GoldenStep(CLAIM, "conclusion", "", "(or (> x 0.0) (<= x 0.0))"), facts=(_X,)
    )
    result = await Verifier(fake).verify(
        trace, context=VerificationContext(facts=(_X,), payload_claim=CLAIM)
    )
    assert _reasons(result) == ["UNGROUNDED:formalization_failed"]
    assert result.step_verdicts[0].details == "formalization is a tautology"
    assert not _passes(result)


async def test_premise_consistent_with_facts_is_assumed_and_timed() -> None:
    trace, fake = _trace(
        GoldenStep("x is positive", "premise", "", "(> x 0.0)"),
        GoldenStep(CLAIM, "conclusion", "", "(> x 1.0)", (0,)),
        facts=(_X,),
    )
    result = await Verifier(fake).verify(
        trace, context=VerificationContext(facts=(_X,), payload_claim=CLAIM)
    )
    assert _reasons(result) == ["VALID:assumed", "VALID:entailed"]
    assert result.step_verdicts[0].solver_ms > 0


async def test_only_the_premise_that_introduces_the_inconsistency_is_invalid() -> None:
    trace, fake = _trace(
        _P,
        _P_IMPLIES_C,
        GoldenStep("not C", "premise", "", "(not c)"),
        GoldenStep(CLAIM, "conclusion", "", "c", (0, 1)),
    )
    result = await Verifier(fake).verify(trace, context=BOUND)
    assert _reasons(result) == [
        "VALID:assumed",
        "VALID:assumed",
        "INVALID:premises_inconsistent",
        "VALID:entailed",
    ]
    assert result.consistency_score < PASS_THRESHOLD and not _passes(result)


@pytest.mark.parametrize("pass_threshold", [0.2, 0.5, 0.9])
async def test_score_cap_follows_the_verifiers_configured_threshold(pass_threshold: float) -> None:
    trace, fake = _trace(
        *(GoldenStep(f"Q{i}", "premise", f"(declare-const q{i} Bool)", f"q{i}") for i in range(9)),
        GoldenStep(CLAIM, "conclusion", "(declare-const c Bool)", "c", (0,)),
    )
    verifier = Verifier(fake, pass_threshold=pass_threshold, retry_threshold=pass_threshold)
    result = await verifier.verify(trace, context=BOUND)
    assert result.step_verdicts[-1].verdict == "INVALID"
    assert result.consistency_score < pass_threshold


@dataclass
class _InconclusiveTautology(Z3Solver):
    """Times out on the tautology check (the claim against `true` alone)."""

    def check_entailment(self, premises: list[SmtFormula], claim: SmtFormula) -> SolverOutcome:
        if premises == [SmtFormula("", "true")]:
            return SolverOutcome(verdict="UNGROUNDED", reason="timeout", details="unknown")
        return super().check_entailment(premises, claim)


async def test_inconclusive_tautology_check_fails_closed() -> None:
    trace, fake = _trace(_P, _P_IMPLIES_C, GoldenStep(CLAIM, "conclusion", "", "c", (0, 1)))
    verifier = Verifier(fake)
    verifier._solver = _InconclusiveTautology()
    result = await verifier.verify(trace, context=BOUND)
    assert _reasons(result)[-1] == "UNGROUNDED:timeout"
    assert not _passes(result)


@dataclass
class _InconclusiveCircular(Z3Solver):
    """Times out only when checking whether one premise alone entails the conclusion."""

    def check_entailment(self, premises: list[SmtFormula], claim: SmtFormula) -> SolverOutcome:
        if len(premises) == 1 and premises[0].assertion != "true" and claim.assertion == "c":
            return SolverOutcome(verdict="UNGROUNDED", reason="timeout", details="unknown")
        return super().check_entailment(premises, claim)


async def test_inconclusive_circularity_check_fails_closed() -> None:
    trace, fake = _trace(_P, _P_IMPLIES_C, GoldenStep(CLAIM, "conclusion", "", "c", (0, 1)))
    verifier = Verifier(fake)
    verifier._solver = _InconclusiveCircular()
    result = await verifier.verify(trace, context=BOUND)
    assert _reasons(result)[:2] == ["UNGROUNDED:timeout", "UNGROUNDED:timeout"]
    assert "could not rule out a circular premise" in result.step_verdicts[0].details
    assert not _passes(result)


QUESTION = "Does C hold?"
HYPOTHESIS = Hypothesis(
    question=QUESTION,
    statement=CLAIM,
    rationale="r",
    citations=(Citation(source="arxiv", identifier="1", title="t"),),
)


async def test_agent_gate_withholds_a_high_scoring_trace_with_an_unproven_conclusion() -> None:
    fake = FakeLLMClient()
    fake.register(
        "research.hypothesis", build_hypothesis_prompt(QUESTION), HYPOTHESIS.model_dump_json()
    )
    register_trace(
        fake,
        agent="research",
        trace_purpose="research.trace",
        trace_prompt=build_trace_prompt(QUESTION, HYPOTHESIS),
        steps=(
            *(
                GoldenStep(f"Q{i}", "premise", f"(declare-const q{i} Bool)", f"q{i}")
                for i in range(4)
            ),
            GoldenStep(CLAIM, "conclusion", "", "(c", (0,)),
        ),
    )

    update = await ResearchAgent(verifier=Verifier(fake), llm=fake, hitl_threshold=0.5)(
        empty_state(QUESTION)
    )

    assert update["hitl_pending"] is not None
    assert update["hitl_pending"].reason == "final step 4 is UNGROUNDED (formalization_failed)"
    assert "hypotheses" not in update
    assert content(*update["hitl_payload"]["hypotheses"]) == content(HYPOTHESIS)
