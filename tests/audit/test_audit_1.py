"""AUDIT-1: encoding soundness. Wrong traces the verifier currently admits.

Every test asserts the *correct* behavior and is a strict xfail: it fails
today, and it passing means the finding is fixed. See `AUDIT-1.md`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

import pytest
import z3

from autolab.agents.common import measurement_facts, result_claim
from autolab.agents.research import ResearchAgent, build_hypothesis_prompt, build_trace_prompt
from autolab.agents.verification_monitor import AgentDecision, VerificationMonitor
from autolab.config import PASS_THRESHOLD
from autolab.orchestrator.checkpoints import contradiction_trace, contradiction_trigger
from autolab.orchestrator.graph import build_graph
from autolab.state.models import (
    Citation,
    ExperimentResult,
    FormalizedStep,
    Hypothesis,
    Measurement,
    ReasoningStep,
    ReasoningTrace,
    VerificationContext,
    VerificationResult,
)
from autolab.state.schema import empty_state
from autolab.verification.faithfulness import faithfulness_prompt
from autolab.verification.formalizer import FormalizationError, Vocabulary, prompt_for
from autolab.verification.llm import FakeLLMClient, LLMPurpose
from autolab.verification.solver import SmtFormula
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import register_failing_trace, register_trivial_trace
from tests.fixtures.golden_traces import GoldenStep
from tests.integration.orchestrator import test_graph as tg

_FEEDBACK_MARKER = "\n\nfeedback_on_previous_attempt: "

CLAIM = "Compound K-17 cures disease D in humans"
QUESTION = "Does compound K-17 cure disease D?"
HYPOTHESIS = Hypothesis(
    question=QUESTION,
    statement=CLAIM,
    rationale="none",
    citations=(Citation(source="arxiv", identifier="0000.0000", title="unrelated"),),
)


def _register(
    fake: FakeLLMClient,
    steps: Sequence[GoldenStep],
    *,
    facts: Sequence[FormalizedStep] = (),
    faithful: Sequence[bool] | None = None,
) -> ReasoningTrace:
    """Register formalizations (and optional judge verdicts) exactly as the verifier asks."""
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
            formula = SmtFormula(candidate.smt(), gs.assertion)
            fake.register(
                "faithfulness",
                faithfulness_prompt(step, formula),
                json.dumps(
                    {"back_translation": gs.claim, "faithful": faithful[i], "explanation": ""}
                ),
            )
        vocab = candidate
    return ReasoningTrace(agent="audit", steps=tuple(reasoning))


def _admitted(result: VerificationResult) -> bool:
    """What BaseAgent and VerificationMonitor do with a verdict: admit iff score >= threshold."""
    return (
        result.consistency_score >= PASS_THRESHOLD
        and VerificationMonitor().decide(result) is AgentDecision.PASS
    )


_BOUND = VerificationContext(payload_claim=CLAIM)
_TESTED = GoldenStep("K-17 was tested in a cell assay", "premise", "(declare-const t Bool)", "t")


# ---------------------------------------------------------------------------
# AUDIT-1 #1 (critical): the payload claim asserted as a premise, then concluded.


@pytest.mark.xfail(strict=True, reason="AUDIT-1 #1")
async def test_circular_premise_restating_payload_is_not_admitted() -> None:
    fake = FakeLLMClient()
    fake.register(
        "research.hypothesis", build_hypothesis_prompt(QUESTION), HYPOTHESIS.model_dump_json()
    )
    register_trivial_trace(
        fake,
        agent="research",
        trace_purpose="research.trace",
        trace_prompt=build_trace_prompt(QUESTION, HYPOTHESIS),
        conclusion=CLAIM,
    )

    update = await ResearchAgent(verifier=Verifier(fake), llm=fake)(empty_state(QUESTION))

    # Today: confidence 1.0, hitl_pending None, the hypothesis is merged.
    assert update["hitl_pending"] is not None
    assert "hypotheses" not in update


class _RetryFake(FakeLLMClient):
    """On a miss for a prompt carrying retry feedback, answer from `retry` keyed on the base."""

    def __init__(self) -> None:
        super().__init__()
        self.retry: dict[tuple[LLMPurpose, str], str] = {}

    async def call(self, purpose: LLMPurpose, prompt: str) -> str:
        try:
            return await super().call(purpose, prompt)
        except KeyError:
            base = prompt.split(_FEEDBACK_MARKER, 1)[0]
            if (purpose, base) in self.retry:
                return self.retry[(purpose, base)]
            raise


@pytest.mark.xfail(strict=True, reason="AUDIT-1 #1")
async def test_retry_loop_does_not_launder_refuted_claim_into_premise() -> None:
    fake = _RetryFake()
    tg._seed_full_run(fake)
    base_trace_prompt = build_trace_prompt(tg.QUESTION, tg.HYPOTHESIS)
    # Attempt 1: the hypothesis is concluded from an unrelated premise and refuted (INVALID).
    register_failing_trace(
        fake,
        agent="research",
        trace_purpose="research.trace",
        trace_prompt=base_trace_prompt,
        conclusion=tg.HYPOTHESIS.statement,
    )
    # Every retry: the same claim moved into a premise and concluded from itself.
    trivial = FakeLLMClient()
    register_trivial_trace(
        trivial,
        agent="research",
        trace_purpose="research.trace",
        trace_prompt="",
        conclusion=tg.HYPOTHESIS.statement,
    )
    circular = await trivial.call("research.trace", "")
    fake.retry[("research.trace", base_trace_prompt)] = circular
    fake.retry[("research.hypothesis", build_hypothesis_prompt(tg.QUESTION))] = (
        tg.HYPOTHESIS.model_dump_json()
    )
    graph = build_graph(llm=fake, simulator=tg.FixedResultSimulator(tg.RESULT))
    config: dict[str, Any] = {"configurable": {"thread_id": str(uuid4())}}

    final = await graph.ainvoke(empty_state(tg.QUESTION), config=config)

    first = final["verification_history"][0]
    assert any(v.verdict == "INVALID" for v in first.step_verdicts)
    # Today: no human ever sees it; the refuted hypothesis is admitted on retry.
    assert final["hypotheses"] == []


# ---------------------------------------------------------------------------
# AUDIT-1 #2 (critical): SMT commands smuggled inside `declarations`.


@pytest.mark.xfail(strict=True, reason="AUDIT-1 #2")
@pytest.mark.parametrize(
    "decls",
    [
        "(declare-const cures Bool) (assert cures)",
        "(declare-const k Bool) (define-fun cures () Bool true)",
    ],
    ids=["assert", "define-fun"],
)
async def test_declarations_cannot_smuggle_assertions(decls: str) -> None:
    fake = FakeLLMClient()
    trace = _register(
        fake,
        [_TESTED, GoldenStep(CLAIM, "conclusion", decls, "cures", (0,))],
        # The judge approves: the *assertion* `cures` does state the claim.
        faithful=[True, True],
    )

    result = await Verifier(fake, check_faithfulness=True).verify(trace, context=_BOUND)

    # Today: the conclusion is "entailed" (by itself), score 1.0.
    assert result.step_verdicts[1].verdict != "VALID"
    assert not _admitted(result)


# ---------------------------------------------------------------------------
# AUDIT-1 #3 (high): an UNGROUNDED payload conclusion is admitted (score 0.5 weight).


def _premises(n: int) -> list[GoldenStep]:
    return [
        GoldenStep(f"observation {i} holds", "premise", f"(declare-const o{i} Bool)", f"o{i}")
        for i in range(n)
    ]


@pytest.mark.xfail(strict=True, reason="AUDIT-1 #3")
async def test_conclusion_that_fails_to_formalize_is_not_admitted() -> None:
    fake = FakeLLMClient()
    trace = _register(fake, [*_premises(1), GoldenStep(CLAIM, "conclusion", "", "(cures", (0,))])

    result = await Verifier(fake).verify(trace, context=_BOUND)

    assert result.step_verdicts[1].reason == "formalization_failed"
    # Today: (1 + 0.5) / 2 = 0.75 >= 0.7, admitted.
    assert not _admitted(result)


@pytest.mark.xfail(strict=True, reason="AUDIT-1 #3")
async def test_conclusion_rejected_by_faithfulness_judge_is_not_admitted() -> None:
    fake = FakeLLMClient()
    steps = [
        *_premises(2),
        GoldenStep(CLAIM, "conclusion", "(declare-const cures Bool)", "(=> o0 o1)", (0, 1)),
    ]
    trace = _register(fake, steps, faithful=[True, True, False])

    result = await Verifier(fake, check_faithfulness=True).verify(trace, context=_BOUND)

    assert result.step_verdicts[2].reason == "formalization_failed"
    # Today: the judge says the SMT does not state the claim, yet (2 + 0.5) / 3 = 0.83.
    assert not _admitted(result)


@pytest.mark.xfail(strict=True, reason="AUDIT-1 #3")
async def test_conclusion_that_times_out_is_not_admitted() -> None:
    fake = FakeLLMClient()
    xyz = "(declare-const x Int)\n(declare-const y Int)\n(declare-const z Int)"
    steps = [
        GoldenStep("x, y, z are positive", "premise", xyz, "(and (> x 0) (> y 0) (> z 0))"),
        GoldenStep(CLAIM, "conclusion", "", "(not (= (+ (* x x x) (* y y y)) (* z z z)))", (0,)),
    ]
    trace = _register(fake, steps)

    result = await Verifier(fake, solver_timeout_seconds=0.2).verify(trace, context=_BOUND)

    assert result.step_verdicts[1].reason == "timeout"
    # Today: the payload conclusion was never checked (solver gave up), yet it scores 0.75.
    assert not _admitted(result)


@pytest.mark.xfail(strict=True, reason="AUDIT-1 #3")
async def test_conclusion_with_no_premises_is_not_admitted() -> None:
    fake = FakeLLMClient()
    steps = [
        *_premises(3),
        GoldenStep("so q", "inference", "(declare-const q Bool)", "(q", (0,)),
        GoldenStep(CLAIM, "conclusion", "(declare-const cures Bool)", "cures", (3,)),
    ]
    trace = _register(fake, steps)

    result = await Verifier(fake).verify(trace, context=_BOUND)

    assert result.step_verdicts[4].reason == "no_premises"
    # Today: (3 + 0.5 + 0.5) / 5 = 0.8, and the monitor's no_premises rework rule is
    # never reached because PASS is decided on score first.
    assert not _admitted(result)


# ---------------------------------------------------------------------------
# AUDIT-1 #4 (medium): premise steps are never checked against facts or each other.

_RESULT = ExperimentResult(
    design_id=uuid4(),
    status="success",
    measurements=(Measurement(name="absorbance", value=0.3),),
)


@pytest.mark.xfail(strict=True, reason="AUDIT-1 #4")
async def test_premise_contradicting_a_measurement_is_not_valid() -> None:
    fake = FakeLLMClient()
    facts = measurement_facts(_RESULT)
    steps = [
        GoldenStep("The measured absorbance was 5.0", "premise", "", "(= absorbance 5.0)"),
        GoldenStep("The plate reader ran", "premise", "(declare-const ran Bool)", "ran"),
        GoldenStep(result_claim(_RESULT), "conclusion", "", "(= absorbance 0.3)", (1,)),
    ]
    trace = _register(fake, steps, facts=facts)
    context = VerificationContext(facts=facts, payload_claim=result_claim(_RESULT))

    result = await Verifier(fake).verify(trace, context=context)

    # Today: step 0 contradicts the code-built fact (= absorbance 0.3) but is VALID/assumed,
    # and the trace scores 1.0.
    assert result.step_verdicts[0].verdict != "VALID"
    assert not _admitted(result)


@pytest.mark.xfail(strict=True, reason="AUDIT-1 #4")
async def test_mutually_contradictory_premises_are_not_admitted() -> None:
    fake = FakeLLMClient()
    steps = [
        GoldenStep("K-17 is toxic", "premise", "(declare-const toxic Bool)", "toxic"),
        GoldenStep("K-17 is not toxic", "premise", "", "(not toxic)"),
        GoldenStep(CLAIM, "conclusion", "", "toxic", (0,)),
    ]
    trace = _register(fake, steps)

    result = await Verifier(fake).verify(trace, context=_BOUND)

    # Today: P and not P are both VALID/assumed and the trace scores 1.0.
    assert not _admitted(result)


# ---------------------------------------------------------------------------
# AUDIT-1 #5 (medium): a tautological conclusion is VALID whenever faithfulness is off.


@pytest.mark.xfail(strict=True, reason="AUDIT-1 #5")
@pytest.mark.parametrize("assertion", ["true", "(or t (not t))"])
async def test_tautological_conclusion_is_not_valid(assertion: str) -> None:
    fake = FakeLLMClient()
    trace = _register(fake, [_TESTED, GoldenStep(CLAIM, "conclusion", "", assertion, (0,))])

    # Default construction: faithfulness is off for any client without `live = True`.
    result = await Verifier(fake).verify(trace, context=_BOUND)

    # Today: the payload claim "binds" by text, its formula says nothing, score 1.0.
    assert result.step_verdicts[1].verdict != "VALID"
    assert not _admitted(result)


# ---------------------------------------------------------------------------
# AUDIT-1 #6 (medium): the contradiction trigger fails open.

_PRIOR = Hypothesis(question=QUESTION, statement="K-17 is toxic to human cells", rationale="r")
_NEW = Hypothesis(question=QUESTION, statement="K-17 is not toxic to human cells", rationale="r")


@pytest.mark.xfail(strict=True, reason="AUDIT-1 #6")
async def test_contradiction_trigger_fires_when_reformalization_fails() -> None:
    fake = FakeLLMClient()
    trace = contradiction_trace([_PRIOR], _NEW)
    _register(
        fake,
        [
            GoldenStep(_PRIOR.statement, "premise", "(declare-const toxic Bool)", "toxic"),
            GoldenStep(_NEW.statement, "premise", "", "(not toxic)"),
            # The redundant re-formalization of the new statement is malformed.
            GoldenStep(_NEW.statement, "inference", "", "(not toxic", (0, 1)),
        ],
    )
    assert [s.claim for s in trace.steps] == [_PRIOR.statement, _NEW.statement, _NEW.statement]
    trigger = contradiction_trigger(Verifier(fake))
    state = empty_state(QUESTION)
    state["hypotheses"] = [_PRIOR]

    # Today: steps 0 and 1 are jointly unsat, but the last step never reaches the solver,
    # so the trigger returns None and the contradictory hypothesis is admitted.
    assert await trigger(_NEW, state) is not None


@pytest.mark.xfail(strict=True, reason="AUDIT-1 #6")
async def test_contradiction_trigger_fails_closed_when_prior_cannot_be_formalized() -> None:
    fake = FakeLLMClient()
    _register(
        fake,
        [
            GoldenStep(_PRIOR.statement, "premise", "", "(toxic"),
            GoldenStep(_NEW.statement, "premise", "(declare-const toxic Bool)", "(not toxic)"),
            GoldenStep(_NEW.statement, "inference", "", "(not toxic)", (0, 1)),
        ],
    )
    trigger = contradiction_trigger(Verifier(fake))
    state = empty_state(QUESTION)
    state["hypotheses"] = [_PRIOR]

    # Today: the prior is silently dropped from the premise pool; the check passes.
    assert await trigger(_NEW, state) is not None
