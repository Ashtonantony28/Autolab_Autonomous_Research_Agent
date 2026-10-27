"""SG25: the Design agent's independent cost and duration estimate."""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import BaseModel, ValidationError

from autolab.agents.common import design_claim, design_facts, with_feedback
from autolab.agents.design import (
    ESTIMATE_DISAGREEMENT_RATIO,
    DesignAgent,
    DesignEstimate,
    build_design_prompt,
    build_design_trace_prompt,
    build_estimate_prompt,
)
from autolab.agents.verification_monitor import VerificationMonitor
from autolab.orchestrator.checkpoints import cost_trigger
from autolab.orchestrator.routing import make_monitor_node
from autolab.state.models import ExperimentDesign, ExperimentParameter, Hypothesis
from autolab.state.schema import AutoLabState, empty_state
from autolab.verification.llm import FakeLLMClient, LLMPurpose, SchemaT
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import (
    content,
    register_estimate,
    register_failing_trace,
    register_trivial_trace,
)

HYPOTHESIS = Hypothesis(question="Q?", statement="S", rationale="R")
DESIGN = ExperimentDesign(
    hypothesis_id=HYPOTHESIS.id,
    protocol="Incubate the plate with the reagent kit and read absorbance",
    parameters=(ExperimentParameter(name="volume", value=50.0, unit="uL"),),
    estimated_cost_usd=5.0,
    estimated_duration_minutes=30.0,
    success_criteria=("absorbance rises",),
)


class RecordingLLM(FakeLLMClient):
    """Records each structured call's purpose and schema."""

    def __init__(self) -> None:
        super().__init__()
        self.structured: list[tuple[LLMPurpose, type[BaseModel]]] = []

    async def call_structured(
        self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        self.structured.append((purpose, schema))
        return await super().call_structured(purpose, prompt, schema)


def _fake(
    design: ExperimentDesign = DESIGN,
    estimate: tuple[float, float] | None = None,
    *,
    passing: bool = True,
) -> RecordingLLM:
    fake = RecordingLLM()
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
    cost, minutes = estimate or (None, None)
    register_estimate(fake, design, cost_usd=cost, duration_minutes=minutes)
    return fake


def _state() -> AutoLabState:
    return {**empty_state("Q?"), "hypotheses": [HYPOTHESIS]}


async def _run(fake: FakeLLMClient, *, trigger: bool = True) -> dict[str, Any]:
    agent = DesignAgent(
        verifier=Verifier(fake),
        llm=fake,
        hitl_trigger=cost_trigger(10.0, 60.0) if trigger else None,
    )
    return await agent(_state())


# --- the call and its prompt ------------------------------------------------


async def test_the_estimate_is_a_structured_call_with_its_own_schema() -> None:
    fake = _fake()

    update = await _run(fake)

    assert update["hitl_pending"] is None
    assert content(update["current_experiment"]) == content(DESIGN)
    assert fake.structured[-1] == ("design.estimate", DesignEstimate)


def test_the_estimate_prompt_reads_the_text_but_not_the_designs_own_figures() -> None:
    design = DESIGN.model_copy(
        update={"estimated_cost_usd": 1234.5, "estimated_duration_minutes": 678.0}
    )
    prompt = build_estimate_prompt(design)

    assert DESIGN.protocol in prompt
    assert "- volume = 50.0 uL" in prompt
    assert "- absorbance rises" in prompt
    assert "1234" not in prompt and "678" not in prompt
    assert "estimated_cost_usd" not in prompt
    assert prompt == build_estimate_prompt(DESIGN)


def test_the_estimate_prompt_marks_empty_parameters_and_criteria() -> None:
    bare = DESIGN.model_copy(update={"parameters": (), "success_criteria": ()})
    assert build_estimate_prompt(bare).count("- none") == 2


@pytest.mark.parametrize(
    "reply",
    [
        {"estimated_cost_usd": -1.0, "estimated_duration_minutes": 1.0, "rationale": "r"},
        {"estimated_cost_usd": 1.0, "estimated_duration_minutes": float("inf"), "rationale": "r"},
        {"estimated_cost_usd": float("nan"), "estimated_duration_minutes": 1.0, "rationale": "r"},
        {"estimated_cost_usd": 1.0, "estimated_duration_minutes": 1.0},
        {"estimated_cost_usd": 1, "estimated_duration_minutes": 1, "rationale": "r", "x": 1},
    ],
)
def test_the_estimate_schema_rejects_unusable_replies(reply: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DesignEstimate.model_validate(reply)


async def test_a_malformed_estimate_reply_raises_for_the_error_escalation() -> None:
    fake = _fake()
    fake.register("design.estimate", build_estimate_prompt(DESIGN), '{"rationale": "r"}')
    with pytest.raises(ValidationError):
        await _run(fake)


# --- disagreement ----------------------------------------------------------


@pytest.mark.parametrize(
    "estimate",
    [(500.0, 30.0), (5.0, 2880.0), (0.5, 30.0), (1000.0, 10080.0)],
    ids=["cost-under", "duration-under", "cost-over", "both"],
)
async def test_a_large_disagreement_is_withheld_and_names_the_gap(
    estimate: tuple[float, float],
) -> None:
    update = await _run(_fake(estimate=estimate))

    assert update["confidence"] == 1.0
    assert "current_experiment" not in update
    assert content(update["hitl_payload"]["current_experiment"]) == content(DESIGN)
    pending = update["hitl_pending"]
    assert pending.payload["trigger"] == "verification"
    assert pending.payload["feedback"] == pending.reason
    assert "independent estimate" in pending.reason


async def test_the_gap_names_each_field_that_disagrees() -> None:
    update = await _run(_fake(estimate=(500.0, 30.0)))
    reason = update["hitl_pending"].reason
    assert "estimated_cost_usd = 5 but an independent estimate gives 500" in reason
    assert "estimated_duration_minutes" not in reason.split("(")[0]


@pytest.mark.parametrize("factor", [1 / ESTIMATE_DISAGREEMENT_RATIO, 0.5, 1.0, 2.0])
async def test_figures_within_the_ratio_and_under_the_trigger_are_admitted(factor: float) -> None:
    # $5 / 30 min against an estimate within 3x that stays at or under $10 / 60 min.
    update = await _run(_fake(estimate=(5.0 * factor, 30.0 * factor)))
    assert update["hitl_pending"] is None


async def test_figures_below_the_floors_compare_as_the_floor() -> None:
    tiny = DESIGN.model_copy(update={"estimated_cost_usd": 0.1, "estimated_duration_minutes": 1.0})
    update = await _run(_fake(tiny, estimate=(0.9, 9.0)))
    assert update["hitl_pending"] is None


async def test_a_disagreement_is_withheld_even_without_a_policy_trigger() -> None:
    update = await _run(_fake(estimate=(500.0, 30.0)), trigger=False)
    assert update["hitl_pending"].payload["trigger"] == "verification"


# --- the larger figure drives the cost/duration trigger --------------------


@pytest.mark.parametrize(
    ("estimate", "expected"),
    [((12.0, 30.0), "estimated cost $12.00"), ((5.0, 80.0), "estimated duration 80 min")],
)
async def test_the_larger_figure_drives_the_trigger(
    estimate: tuple[float, float], expected: str
) -> None:
    update = await _run(_fake(estimate=estimate))

    assert "current_experiment" not in update
    pending = update["hitl_pending"]
    assert pending.payload["trigger"] == "policy"
    assert pending.reason.startswith("independent estimate: ") and expected in pending.reason
    assert "feedback" not in pending.payload


async def test_a_larger_estimate_without_a_trigger_is_admitted() -> None:
    update = await _run(_fake(estimate=(12.0, 80.0)), trigger=False)
    assert update["hitl_pending"] is None


async def test_a_design_already_paused_by_its_own_figures_pauses_once() -> None:
    costly = DESIGN.model_copy(update={"estimated_cost_usd": 20.0})
    update = await _run(_fake(costly, estimate=(25.0, 80.0)))
    pending = update["hitl_pending"]
    assert pending.payload["trigger"] == "policy"
    assert pending.reason == "estimated cost $20.00 > $10.00"


async def test_a_blocked_trace_names_the_estimates_policy_for_the_reviewer() -> None:
    update = await _run(_fake(estimate=(12.0, 30.0), passing=False))
    pending = update["hitl_pending"]
    assert pending.payload["trigger"] == "verification"
    assert pending.reason.endswith("; independent estimate: estimated cost $12.00 > $10.00")
    assert "feedback" not in pending.payload


async def test_a_blocked_trace_with_an_agreeing_estimate_keeps_its_reason() -> None:
    update = await _run(_fake(passing=False))
    assert "estimate" not in update["hitl_pending"].reason


# --- retried with the reason before escalating -------------------------------


async def test_a_disagreement_is_retried_with_its_reason_before_escalating() -> None:
    update = await _run(_fake(estimate=(5.0, 2880.0)))
    node = make_monitor_node(VerificationMonitor(), max_retries=2)

    # The monitor node sees the agent's update merged into the state.
    retry = node(cast(AutoLabState, {**_state(), **update}))
    exhausted = node(cast(AutoLabState, {**_state(), **update, "retry_count": 2}))

    assert retry.goto == "design"
    assert isinstance(retry.update, dict)
    assert "an independent estimate gives 2880" in retry.update["feedback"]
    assert exhausted.goto == "hitl"


async def test_a_retried_design_sees_the_gap_and_its_fix_is_admitted() -> None:
    fake = _fake(estimate=(5.0, 2880.0))
    first = await _run(fake)
    feedback = first["hitl_pending"].payload["feedback"]
    honest = DESIGN.model_copy(
        update={
            "protocol": f"{DESIGN.protocol} once the cells are confluent",
            "estimated_duration_minutes": 2880,
        }
    )
    fake.register(
        "design.experiment",
        with_feedback(build_design_prompt(HYPOTHESIS), feedback),
        honest.model_dump_json(),
    )
    register_trivial_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=with_feedback(build_design_trace_prompt(HYPOTHESIS, honest), feedback),
        conclusion=design_claim(honest),
        facts=design_facts(honest),
    )
    register_estimate(fake, honest, cost_usd=5.0, duration_minutes=2880.0)
    agent = DesignAgent(verifier=Verifier(fake), llm=fake, hitl_trigger=cost_trigger(10.0, 60.0))

    second = await agent({**_state(), "feedback": feedback})

    # The gap is gone; the honest 48 h figure now pauses on the duration policy.
    assert second["hitl_pending"].payload["trigger"] == "policy"
    assert second["hitl_pending"].reason == "estimated duration 2880 min > 60 min"


async def test_a_figure_rejection_skips_the_estimate() -> None:
    priced = DESIGN.model_copy(update={"protocol": "spend $500 on reagent"})
    fake = RecordingLLM()
    fake.register("design.experiment", build_design_prompt(HYPOTHESIS), priced.model_dump_json())
    register_trivial_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(HYPOTHESIS, priced),
        conclusion=design_claim(priced),
        facts=design_facts(priced),
    )

    update = await _run(fake)

    assert "$500" in update["hitl_pending"].payload["feedback"]
    assert "design.estimate" not in [p for p, _ in fake.structured]
