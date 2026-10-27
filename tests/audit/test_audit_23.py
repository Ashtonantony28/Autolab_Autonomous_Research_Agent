"""AUDIT-23 repros: API/SDK consistency, round 6 (see AUDIT-23.md).

Every test asserts the *correct* behaviour and is a strict xfail, so it
starts to XPASS, and fails the suite, once its finding is fixed. No network:
every LLM is a seeded `FakeLLMClient`.
"""

from __future__ import annotations

from typing import Any, cast

import pydantic_core
import pytest
from langchain_core.runnables import RunnableConfig

import autolab.orchestrator.checkpoints as checkpoints
from autolab.agents.common import design_claim, design_facts
from autolab.agents.design import (
    DesignAgent,
    DesignEstimate,
    build_design_prompt,
    build_design_trace_prompt,
    build_estimate_prompt,
)
from autolab.agents.verification_monitor import blocking_reason
from autolab.config import HITL_COST_USD, HITL_DURATION_MIN
from autolab.orchestrator.checkpoints import cost_trigger, make_hitl_node, merged_policy
from autolab.state.models import ExperimentDesign, Hypothesis
from autolab.state.schema import AutoLabState, empty_state
from autolab.verification.llm import FakeLLMClient, system_prompt
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import register_failing_trace, register_trivial_trace

HYPOTHESIS = Hypothesis(question="Q?", statement="S", rationale="R")
PROTOCOL = "Screen the compound library on the plate reader in triplicate"
RATIONALE = "AUDIT-23 estimator basis: 384-well plates, two reads"


def _design() -> ExperimentDesign:
    # Cheap and short by its own figures: the cost/duration trigger alone never fires.
    return ExperimentDesign(
        hypothesis_id=HYPOTHESIS.id,
        protocol=PROTOCOL,
        estimated_cost_usd=5.0,
        estimated_duration_minutes=30.0,
    )


def _fake(
    design: ExperimentDesign, *, passing: bool, cost_usd: float, duration_minutes: float = 30.0
) -> FakeLLMClient:
    fake = FakeLLMClient()
    fake.register("design.experiment", build_design_prompt(HYPOTHESIS), design.model_dump_json())
    register = register_trivial_trace if passing else register_failing_trace
    register(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(HYPOTHESIS, design),
        conclusion=design_claim(design),
        facts=design_facts(design),
    )
    estimate = DesignEstimate(
        estimated_cost_usd=cost_usd,
        estimated_duration_minutes=duration_minutes,
        rationale=RATIONALE,
    )
    fake.register("design.estimate", build_estimate_prompt(design), estimate.model_dump_json())
    return fake


def _state() -> AutoLabState:
    return {**empty_state("Q?"), "hypotheses": [HYPOTHESIS]}


def _agent(fake: FakeLLMClient) -> DesignAgent:
    return DesignAgent(
        verifier=Verifier(fake),
        llm=fake,
        hitl_trigger=cost_trigger(HITL_COST_USD, HITL_DURATION_MIN),
    )


# --- #1 (medium): approving an estimate disagreement skips the cost pause --------


@pytest.mark.xfail(strict=True, reason="AUDIT-23 #1")
async def test_approving_a_disagreement_still_pauses_on_the_estimated_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The design says $5; the independent estimate says $50 (10x apart). The agent
    # withholds it as a `verification` request; once the retries are spent a human
    # approves it. `make_hitl_node` then re-runs the cost trigger on the design's own
    # $5 and merges it, so a run the estimator prices at $50 proceeds to `execute`
    # with no policy pause (principle 7b). ADR-0008: "When the estimate alone would
    # fire the trigger, the request's reason says so" -- not on this path.
    design = _design()
    update = await _agent(_fake(design, passing=True, cost_usd=50.0))(_state())
    pending = update["hitl_pending"]
    assert pending is not None and pending.payload["trigger"] == "verification"
    assert "50" in pending.reason

    monkeypatch.setattr(checkpoints, "interrupt", lambda _value: {"action": "approve"})
    hitl = make_hitl_node(
        {
            "design": merged_policy(
                cost_trigger(HITL_COST_USD, HITL_DURATION_MIN), "current_experiment"
            )
        }
    )
    state = cast(AutoLabState, {**_state(), **update})
    command = await hitl(state, RunnableConfig())

    assert command.goto == "hitl", f"approved straight to {command.goto!r}"


# --- #2 (medium): the independent estimate is never recorded --------------------


def _recorded(update: dict[str, Any]) -> str:
    return pydantic_core.to_json(update).decode()


@pytest.mark.xfail(strict=True, reason="AUDIT-23 #2")
@pytest.mark.parametrize(
    ("cost_usd", "outcome"),
    [(8.0, "admitted"), (14.0, "policy")],
    ids=["admitted-at-8", "policy-pause-at-14"],
)
async def test_the_estimate_is_recorded_with_the_decision(cost_usd: float, outcome: str) -> None:
    # Principle 8: every decision writes a structured trace. The estimate decides
    # whether a design pauses, but the update the agent returns (and so the state,
    # the checkpoint, the store and the dashboard) holds neither its figures nor its
    # basis. At $8 the design is admitted with no trace of the second opinion; at
    # $14 the pause reason is the only place the figure appears, as prose.
    design = _design()
    update = await _agent(_fake(design, passing=True, cost_usd=cost_usd))(_state())
    pending = update["hitl_pending"]
    if outcome == "admitted":
        assert pending is None
    else:
        assert pending is not None and pending.payload["trigger"] == "policy"

    assert RATIONALE in _recorded(update), "the estimate's basis is not in the update"


# --- #3 (medium): a blocked trace's reason is replaced ---------------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-23 #3")
async def test_a_blocked_trace_keeps_its_blocking_reason_beside_the_disagreement() -> None:
    # The trace is blocked (its conclusion is not entailed) and the estimate also
    # disagrees. `DesignAgent.__call__` builds a new request whose reason is only the
    # disagreement, so a reviewer reading `reason` (CLI, dashboard, hitl event) sees
    # a cost gap and not that the verifier rejected the trace.
    design = _design()
    update = await _agent(_fake(design, passing=False, cost_usd=50.0))(_state())
    verdict = update["verification_history"][-1]
    blocked = blocking_reason(verdict)
    assert blocked is not None
    pending = update["hitl_pending"]
    assert pending is not None and "independent estimate" in pending.reason

    assert blocked in pending.reason, pending.reason


# --- #4 (low): the estimator is prompted as the Design agent ---------------------


@pytest.mark.xfail(strict=True, reason="AUDIT-23 #4")
def test_the_estimate_has_its_own_system_prompt() -> None:
    # `system_prompt` routes by the purpose's family, so `design.estimate` gets the
    # Design agent's role ("Turn the given hypothesis into one concrete, safe
    # experiment ...") plus the reasoning-trace rules: the "independent" estimator
    # is told it is the agent whose figures it checks, and is given rules for a
    # trace it is never asked for.
    assert system_prompt("design.estimate") != system_prompt("design.experiment")
