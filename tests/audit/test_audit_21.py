"""AUDIT-21: encoding soundness, round 6. Wrong designs and traces that are still admitted.

Every test asserts the *correct* behaviour and is a strict xfail until its
finding is fixed; see `AUDIT-21.md`. All of them run on `FakeLLMClient`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest
from langgraph.types import Command

from autolab.agents.common import design_claim, design_facts
from autolab.agents.design import (
    DesignAgent,
    DesignEstimate,
    _disagreement,
    _stated_figures,
    build_design_prompt,
    build_design_trace_prompt,
)
from autolab.agents.verification_monitor import AgentDecision, VerificationMonitor
from autolab.orchestrator.checkpoints import cost_trigger
from autolab.state.models import ExperimentDesign, HITLResolution, ReasoningStep, ReasoningTrace
from autolab.state.schema import AutoLabState, empty_state
from autolab.verification.formalizer import Vocabulary, prompt_for
from autolab.verification.llm import FakeLLMClient
from autolab.verification.vericot import Verifier
from scripts.audit_load import POSITIVE, XYZ
from tests.fixtures.agent_helpers import register_estimate, register_trace
from tests.fixtures.golden_traces import GoldenStep
from tests.integration.orchestrator import test_graph as tg

# ---------------------------------------------------------------------------
# AUDIT-21 #1 (medium): a Design request withheld for its figures (a figure stated
# in the text, or an independent estimate far from the design's fields) is a
# `verification` request. `approve` re-runs the cost trigger on the design's own
# fields (`merged_policy(cost, "current_experiment")`), not on the larger figure
# SG25 says drives the trigger. A $1 / 5 min design whose protocol runs 48 h, or
# whose estimate is 240 min, is merged and executed with no cost/duration pause.

_STATED = tg.DESIGN.model_copy(
    update={"protocol": "Incubate the plate for 48 hours, then run a single measurement"}
)


async def _approve_figure_request(design: ExperimentDesign, estimate_minutes: float) -> Any:
    """Run the graph to the Design figure request, approve it, and return the next state."""
    fake = FakeLLMClient()
    tg._seed_full_run(fake, design)
    estimate = DesignEstimate(
        estimated_cost_usd=design.estimated_cost_usd,
        estimated_duration_minutes=estimate_minutes,
        rationale="read from the protocol, parameters and success criteria",
    )
    reason = _stated_figures(design) or _disagreement(design, estimate)
    assert reason is not None
    tg._seed_design(fake, design, feedback=reason)  # the monitor's retries
    register_estimate(fake, design, duration_minutes=estimate_minutes)
    graph, config = tg._graph(fake)

    paused = await graph.ainvoke(empty_state(tg.QUESTION), config=config)
    pause = tg._interrupt(paused)
    # Precondition: the retries are spent and the human is asked about the figures.
    assert pause["agent"] == "design"
    assert pause["payload"]["trigger"] == "verification"
    assert paused["retry_count"] == 2

    return await graph.ainvoke(Command(resume=HITLResolution(action="approve")), config=config)


@pytest.mark.xfail(strict=True, reason="AUDIT-21 #1")
@pytest.mark.parametrize(
    ("design", "estimate_minutes"),
    [(_STATED, 2880.0), (tg.DESIGN, 240.0)],
    ids=["stated-48-hours", "estimate-240-min"],
)
async def test_approving_a_figure_request_still_pauses_on_the_larger_figure(
    design: ExperimentDesign, estimate_minutes: float
) -> None:
    after = await _approve_figure_request(design, estimate_minutes)

    # Today: the run completes; Execute ran a 48 h (or 4 h) design at "5 min".
    assert "__interrupt__" in after
    assert tg._interrupt(after)["payload"]["trigger"] == "policy"


# ---------------------------------------------------------------------------
# AUDIT-21 #2 (low): when the design's own fields already fire the cost trigger,
# `_apply_policy` returns the base request unchanged. Its reason names only the
# design's figure, so the human approves "$15 > $10" for a run the estimate puts
# at 80 min (or $44), and `approve` of a `policy` request merges with no re-check.


async def _design_update(
    cost_usd: float, minutes: float, estimate: tuple[float, float]
) -> dict[str, Any]:
    """The Design agent's update for an honest, fully verified design and its estimate."""
    protocol = "Incubate the plate with the reagent kit and read absorbance"
    reply: dict[str, Any] = {
        "protocol": protocol,
        "parameters": [],
        "estimated_cost_usd": cost_usd,
        "estimated_duration_minutes": minutes,
        "success_criteria": [],
    }
    fake = FakeLLMClient()
    fake.register("design.experiment", build_design_prompt(tg.HYPOTHESIS), json.dumps(reply))
    design = ExperimentDesign(hypothesis_id=tg.HYPOTHESIS.id, **reply)
    facts = design_facts(design)
    conclusion = " ".join(["protocol_ok", *(f.assertion for f in facts)])
    register_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(tg.HYPOTHESIS, design),
        steps=(
            GoldenStep(
                f"The protocol is: {protocol}",
                "premise",
                "(declare-const protocol_ok Bool)",
                "protocol_ok",
            ),
            GoldenStep(design_claim(design), "conclusion", "", f"(and {conclusion})", (0,)),
        ),
        facts=facts,
    )
    register_estimate(fake, design, cost_usd=estimate[0], duration_minutes=estimate[1])
    agent = DesignAgent(verifier=Verifier(fake), llm=fake, hitl_trigger=cost_trigger(10.0, 60.0))
    state: AutoLabState = {**empty_state(tg.QUESTION), "hypotheses": [tg.HYPOTHESIS]}
    return await agent(state)


@pytest.mark.xfail(strict=True, reason="AUDIT-21 #2")
@pytest.mark.parametrize(
    "estimate", [(15.0, 80.0), (44.0, 30.0)], ids=["duration-80-min", "cost-44-usd"]
)
async def test_policy_pause_names_the_estimate_that_raised_a_figure(
    estimate: tuple[float, float],
) -> None:
    update = await _design_update(15.0, 30.0, estimate)

    pending = update["hitl_pending"]
    # Precondition: verified, paused on the design's own $15, estimate within 3x.
    assert update["confidence"] == 1.0
    assert pending is not None and pending.payload["trigger"] == "policy"
    # Today: the reason is "estimated cost $15.00 > $10.00" only.
    assert "independent estimate" in pending.reason


# ---------------------------------------------------------------------------
# AUDIT-21 #3 (low): a premise whose consistency check is inconclusive (timeout,
# or `interrupted` from `SolverExecutor.shutdown`) is UNGROUNDED, which blocks
# nothing, and it leaves the premise pool. A later premise that contradicts it is
# then assumed, and the trace passes. With the same premise decided in time, the
# later premise is INVALID/`premises_inconsistent` and the trace is blocked
# (AUDIT-1 #4). Whether a trace passes thus depends on wall-clock time.

_HARD = f"(and {POSITIVE} (= (+ (* x x x) (* y y y)) (* z z z)) (not ready))"
"""Fermat n=3 (unsat, beyond Z3's nonlinear tactics) and `not ready`: runs to its timeout."""


async def _verify_contradicting_premises(first: str, decls: str) -> AgentDecision:
    steps: Sequence[GoldenStep] = (
        GoldenStep("The well is not ready", "premise", decls, first),
        GoldenStep("The well is ready", "premise", "", "ready"),
        GoldenStep(
            "If the well is ready it is plated",
            "premise",
            "(declare-const plated Bool)",
            "(=> ready plated)",
        ),
        GoldenStep("The well is plated", "conclusion", "", "plated", (1, 2)),
    )
    fake = FakeLLMClient()
    vocab = Vocabulary()
    reasoning = tuple(
        ReasoningStep(
            index=i,
            claim=s.claim,
            claim_type=s.claim_type,
            referenced_step_indices=s.referenced_step_indices,
        )
        for i, s in enumerate(steps)
    )
    for step, spec in zip(reasoning, steps, strict=True):
        reply = {"declarations": spec.declarations, "assertion": spec.assertion}
        fake.register("formalize", prompt_for(step, vocab), json.dumps(reply))
        vocab.merge(spec.declarations)
    verifier = Verifier(fake, solver_timeout_seconds=1.0)
    result = await verifier.verify(ReasoningTrace(agent="design", steps=reasoning))
    return VerificationMonitor().decide(result)


async def test_contradicting_premises_are_blocked_when_decided_in_time() -> None:
    """Control: the same trace with the first premise decided is not admitted."""
    decision = await _verify_contradicting_premises("(not ready)", "(declare-const ready Bool)")

    assert decision is not AgentDecision.PASS


@pytest.mark.xfail(strict=True, reason="AUDIT-21 #3")
async def test_inconclusive_premise_does_not_let_a_contradicting_premise_pass() -> None:
    decision = await _verify_contradicting_premises(_HARD, f"{XYZ}\n(declare-const ready Bool)")

    # Today: PASS at 0.875. Step 0 is UNGROUNDED/timeout and step 1 is assumed.
    assert decision is not AgentDecision.PASS
