"""End-to-end StateGraph runs: every monitor and HITL edge, on `FakeLLMClient` only."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from langgraph.types import Command

from autolab.agents.analysis import build_analysis_prompt, build_analysis_trace_prompt
from autolab.agents.common import (
    design_claim,
    design_facts,
    measurement_facts,
    result_claim,
    with_feedback,
)
from autolab.agents.design import build_design_prompt, build_design_trace_prompt
from autolab.agents.execute import build_execute_trace_prompt
from autolab.agents.research import build_hypothesis_prompt, build_trace_prompt
from autolab.orchestrator.checkpoints import contradiction_trace
from autolab.orchestrator.graph import build_graph
from autolab.state.models import (
    AnalysisReport,
    Citation,
    ExperimentDesign,
    ExperimentResult,
    HITLResolution,
    Hypothesis,
    Interpretation,
    Measurement,
)
from autolab.state.schema import AutoLabState, empty_state
from autolab.tools.lab_hardware.simulator import LabSimulator
from autolab.verification.llm import FakeLLMClient, LLMPurpose
from tests.fixtures.agent_helpers import (
    content,
    register_estimate,
    register_failing_trace,
    register_trace,
    register_trivial_trace,
)
from tests.fixtures.golden_traces import GoldenStep


class FixedResultSimulator(LabSimulator):
    """Simulator that returns a pre-built ExperimentResult (deterministic IDs)."""

    def __init__(self, result: ExperimentResult) -> None:
        super().__init__()
        self._result = result

    def run(self, design: ExperimentDesign) -> ExperimentResult:
        return self._result


class FlakyLLMClient(FakeLLMClient):
    """Raises `ConnectionError` on the first `failures` calls for `purpose`."""

    def __init__(self, purpose: LLMPurpose, failures: int) -> None:
        super().__init__()
        self._purpose = purpose
        self.failures_left = failures

    async def call(self, purpose: LLMPurpose, prompt: str) -> str:
        if purpose == self._purpose and self.failures_left > 0:
            self.failures_left -= 1
            raise ConnectionError("connection reset")
        return await super().call(purpose, prompt)


QUESTION = "Why does X happen?"
HYPOTHESIS = Hypothesis(
    question=QUESTION,
    statement="X happens because Y",
    rationale="Z supports it",
    citations=(Citation(source="arxiv", identifier="42", title="paper"),),
)
DESIGN = ExperimentDesign(
    hypothesis_id=HYPOTHESIS.id,
    protocol="run a single measurement",
    estimated_cost_usd=1.0,
    estimated_duration_minutes=5.0,
)
EXPENSIVE = DESIGN.model_copy(update={"estimated_cost_usd": 50.0})
SLOW = DESIGN.model_copy(update={"estimated_duration_minutes": 120.0})
RESULT = ExperimentResult(
    design_id=DESIGN.id,
    status="success",
    measurements=(Measurement(name="m", value=1.0),),
    notes="stub",
)
REPORT = AnalysisReport(
    result_id=RESULT.id,
    interpretations=(Interpretation(statement="x", rank=0, likelihood=0.9),),
    summary="ok",
)
FAILING_SUMMARY = "1 valid, 1 invalid, 0 ungrounded across 2 steps"


def _seed_design(fake: FakeLLMClient, design: ExperimentDesign, feedback: str = "") -> None:
    fake.register(
        "design.experiment",
        with_feedback(build_design_prompt(HYPOTHESIS), feedback),
        design.model_dump_json(),
    )
    register_trivial_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=with_feedback(build_design_trace_prompt(HYPOTHESIS, design), feedback),
        conclusion=design_claim(design),
        facts=design_facts(design),
    )
    register_estimate(fake, design)


def _seed_full_run(fake: FakeLLMClient, design: ExperimentDesign = DESIGN) -> None:
    """Register every LLM response needed for a clean four-agent run."""
    fake.register(
        "research.hypothesis", build_hypothesis_prompt(QUESTION), HYPOTHESIS.model_dump_json()
    )
    register_trivial_trace(
        fake,
        agent="research",
        trace_purpose="research.trace",
        trace_prompt=build_trace_prompt(QUESTION, HYPOTHESIS),
        conclusion=HYPOTHESIS.statement,
    )
    _seed_design(fake, design)
    register_trivial_trace(
        fake,
        agent="execute",
        trace_purpose="execute.trace",
        trace_prompt=build_execute_trace_prompt(design, RESULT),
        conclusion=result_claim(RESULT),
        facts=measurement_facts(RESULT),
    )
    fake.register("analysis.report", build_analysis_prompt(RESULT), REPORT.model_dump_json())
    register_trivial_trace(
        fake,
        agent="analysis",
        trace_purpose="analysis.trace",
        trace_prompt=build_analysis_trace_prompt(RESULT, REPORT),
        conclusion=REPORT.interpretations[0].statement,
        facts=measurement_facts(RESULT),
    )


def _fail_design_trace(fake: FakeLLMClient, feedback: str = "") -> None:
    fake.register(
        "design.experiment",
        with_feedback(build_design_prompt(HYPOTHESIS), feedback),
        DESIGN.model_dump_json(),
    )
    register_failing_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=with_feedback(build_design_trace_prompt(HYPOTHESIS, DESIGN), feedback),
        conclusion=design_claim(DESIGN),
        facts=design_facts(DESIGN),
    )
    register_estimate(fake, DESIGN)


def _graph(fake: FakeLLMClient) -> tuple[Any, dict[str, Any]]:
    graph = build_graph(llm=fake, simulator=FixedResultSimulator(RESULT), retry_backoff_seconds=0)
    return graph, {"configurable": {"thread_id": str(uuid4())}}


def _interrupt(state: dict[str, Any]) -> dict[str, Any]:
    """The value the `hitl` node passed to `interrupt()`."""
    (pause,) = state["__interrupt__"]
    value: dict[str, Any] = pause.value
    return value


def _agents(state: dict[str, Any]) -> list[str]:
    return [t.agent for t in state["reasoning_traces"]]


def _assert_completed(state: dict[str, Any]) -> None:
    assert "__interrupt__" not in state
    assert state["results"] == [RESULT]
    assert content(*state["analysis_reports"]) == content(REPORT)
    assert state["analysis_reports"][0].result_id == RESULT.id
    assert state["hitl_pending"] is None
    assert state["hitl_payload"] is None
    assert state["current_agent"] == "analysis"


async def test_full_run_reaches_end_with_all_agents_passing() -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake)
    graph, config = _graph(fake)

    final = await graph.ainvoke(empty_state(QUESTION), config=config)

    _assert_completed(final)
    assert content(*final["hypotheses"]) == content(HYPOTHESIS)
    assert _agents(final) == ["research", "design", "execute", "analysis"]
    assert all(v.consistency_score >= 0.7 for v in final["verification_history"])
    assert final["retry_count"] == 0
    assert final["feedback"] == ""


async def test_retry_then_pass_reruns_agent_with_verdict_feedback() -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake)
    _fail_design_trace(fake)
    _seed_design(fake, DESIGN, feedback=FAILING_SUMMARY)
    graph, config = _graph(fake)

    final = await graph.ainvoke(empty_state(QUESTION), config=config)

    _assert_completed(final)
    assert _agents(final) == ["research", "design", "design", "execute", "analysis"]
    first, second = final["verification_history"][1:3]
    assert first.summary == FAILING_SUMMARY
    assert first.consistency_score < 0.7 <= second.consistency_score
    assert final["retry_count"] == 0  # reset once the agent passes


async def test_unbound_conclusion_is_retried_not_escalated() -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake)
    register_trivial_trace(
        fake,
        agent="research",
        trace_purpose="research.trace",
        trace_prompt=build_trace_prompt(QUESTION, HYPOTHESIS),
        conclusion="something else entirely",
    )
    feedback = (
        f"trace does not conclude payload claim {HYPOTHESIS.statement!r}; "
        "0 valid, 0 invalid, 3 ungrounded across 3 steps"
    )
    fake.register(
        "research.hypothesis",
        with_feedback(build_hypothesis_prompt(QUESTION), feedback),
        HYPOTHESIS.model_dump_json(),
    )
    register_trivial_trace(
        fake,
        agent="research",
        trace_purpose="research.trace",
        trace_prompt=with_feedback(build_trace_prompt(QUESTION, HYPOTHESIS), feedback),
        conclusion=HYPOTHESIS.statement,
    )
    graph, config = _graph(fake)

    final = await graph.ainvoke(empty_state(QUESTION), config=config)

    _assert_completed(final)
    assert final["verification_history"][0].consistency_score == 0.0
    assert _agents(final)[:2] == ["research", "research"]
    assert content(*final["hypotheses"]) == content(HYPOTHESIS)


async def test_retries_exhausted_escalates_then_approve_merges_withheld_payload() -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake)
    _fail_design_trace(fake)
    _fail_design_trace(fake, feedback=FAILING_SUMMARY)
    graph, config = _graph(fake)

    paused = await graph.ainvoke(empty_state(QUESTION), config=config)

    assert _agents(paused) == ["research", "design", "design", "design"]  # 1 + max_retries
    assert paused["retry_count"] == 2
    assert paused["current_experiment"] is None
    withheld = paused["hitl_payload"]["current_experiment"]
    assert content(withheld) == content(DESIGN)
    assert withheld.hypothesis_id == paused["hypotheses"][0].id
    pause = _interrupt(paused)
    assert pause["agent"] == "design"
    assert pause["payload"]["trigger"] == "verification"
    assert pause["payload"]["verdict_summary"] == FAILING_SUMMARY

    final = await graph.ainvoke(Command(resume=HITLResolution(action="approve")), config=config)

    _assert_completed(final)
    assert final["retry_count"] == 0


async def test_cost_trigger_pauses_and_approve_continues() -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake, EXPENSIVE)
    graph, config = _graph(fake)

    paused = await graph.ainvoke(empty_state(QUESTION), config=config)

    pause = _interrupt(paused)
    assert pause["agent"] == "design"
    assert pause["reason"] == "estimated cost $50.00 > $10.00"
    assert pause["payload"]["trigger"] == "policy"
    assert paused["verification_history"][-1].consistency_score >= 0.7
    assert paused["current_experiment"] is None  # withheld until approved

    final = await graph.ainvoke(Command(resume={"action": "approve"}), config=config)

    _assert_completed(final)
    assert final["current_experiment"] is None  # consumed by execute


async def test_duration_trigger_pauses() -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake, SLOW)
    graph, config = _graph(fake)

    paused = await graph.ainvoke(empty_state(QUESTION), config=config)

    assert _interrupt(paused)["reason"] == "estimated duration 120 min > 60 min"


async def test_hitl_retry_reruns_agent_with_human_note() -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake, EXPENSIVE)
    note = "find a protocol under $10"
    # A new protocol: the independent estimate reads the protocol, not the figures.
    cheap = DESIGN.model_copy(update={"protocol": "run a single small measurement"})
    _seed_design(fake, cheap, feedback=note)
    register_trivial_trace(
        fake,
        agent="execute",
        trace_purpose="execute.trace",
        trace_prompt=build_execute_trace_prompt(cheap, RESULT),
        conclusion=result_claim(RESULT),
        facts=measurement_facts(RESULT),
    )
    graph, config = _graph(fake)

    await graph.ainvoke(empty_state(QUESTION), config=config)
    final = await graph.ainvoke(
        Command(resume=HITLResolution(action="retry", note=note)), config=config
    )

    _assert_completed(final)
    assert _agents(final) == ["research", "design", "design", "execute", "analysis"]


async def test_hitl_abort_ends_the_run() -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake, EXPENSIVE)
    graph, config = _graph(fake)

    await graph.ainvoke(empty_state(QUESTION), config=config)
    final = await graph.ainvoke(Command(resume={"action": "abort"}), config=config)

    assert "__interrupt__" not in final
    assert final["hitl_pending"] is None
    assert final["hitl_payload"] is None
    assert final["current_agent"] == "hitl"
    assert final["current_experiment"] is None
    assert final["results"] == []


def _seed_contradiction_check(fake: FakeLLMClient, prior: Hypothesis, prior_smt: str) -> None:
    trace = contradiction_trace([prior], HYPOTHESIS)
    register_trace(
        fake,
        agent=trace.agent,
        trace_purpose="unused",
        trace_prompt="unused",
        steps=(
            GoldenStep(prior.statement, "premise", "(declare-const hyp_Y Bool)", prior_smt),
            GoldenStep(HYPOTHESIS.statement, "premise", "", "hyp_Y"),
            GoldenStep(HYPOTHESIS.statement, "inference", "", "hyp_Y", (0, 1)),
        ),
    )


def _with_prior(prior: Hypothesis) -> AutoLabState:
    state = empty_state(QUESTION)
    state["hypotheses"] = [prior]
    return state


async def test_contradicting_hypothesis_pauses() -> None:
    prior = Hypothesis(question=QUESTION, statement="Y plays no role in X", rationale="r")
    fake = FakeLLMClient()
    _seed_full_run(fake)
    _seed_contradiction_check(fake, prior, "(not hyp_Y)")
    graph, config = _graph(fake)

    paused = await graph.ainvoke(_with_prior(prior), config=config)

    pause = _interrupt(paused)
    assert pause["agent"] == "research"
    assert pause["reason"] == "hypothesis contradicts 1 prior hypothesis(es) in state"
    assert paused["hypotheses"] == [prior]
    assert content(*paused["hitl_payload"]["hypotheses"]) == content(HYPOTHESIS)


async def test_consistent_hypothesis_does_not_pause() -> None:
    prior = Hypothesis(question=QUESTION, statement="Y is present", rationale="r")
    fake = FakeLLMClient()
    _seed_full_run(fake)
    _seed_contradiction_check(fake, prior, "hyp_Y")
    graph, config = _graph(fake)

    final = await graph.ainvoke(_with_prior(prior), config=config)

    _assert_completed(final)
    assert final["hypotheses"][0] == prior
    assert content(*final["hypotheses"]) == content(prior, HYPOTHESIS)


async def test_malformed_llm_response_escalates_instead_of_crashing() -> None:
    fake = FakeLLMClient()
    fake.register("research.hypothesis", build_hypothesis_prompt(QUESTION), '{"statement": 1}')
    graph, config = _graph(fake)

    paused = await graph.ainvoke(empty_state(QUESTION), config=config)

    pause = _interrupt(paused)
    assert pause["agent"] == "research"
    assert pause["payload"] == {"trigger": "error", "error": "ValidationError"}
    assert pause["reason"].startswith("ValidationError:")
    assert paused["reasoning_traces"] == []
    assert paused["hitl_payload"] is None

    final = await graph.ainvoke(Command(resume={"action": "abort"}), config=config)
    assert final["hitl_pending"] is None


async def test_model_supplied_ids_are_replaced_in_code() -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake)
    stray = DESIGN.model_copy(update={"hypothesis_id": uuid4()})
    fake.register("design.experiment", build_design_prompt(HYPOTHESIS), stray.model_dump_json())
    graph, config = _graph(fake)

    final = await graph.ainvoke(empty_state(QUESTION), config=config)

    _assert_completed(final)
    (hypothesis,) = final["hypotheses"]
    assert hypothesis.id != HYPOTHESIS.id and hypothesis.created_at > HYPOTHESIS.created_at
    (report,) = final["analysis_reports"]
    assert report.id != REPORT.id and report.result_id == RESULT.id


async def test_transient_error_is_retried() -> None:
    fake = FlakyLLMClient("research.hypothesis", failures=2)
    _seed_full_run(fake)
    graph, config = _graph(fake)

    final = await graph.ainvoke(empty_state(QUESTION), config=config)

    _assert_completed(final)
    assert fake.failures_left == 0


async def test_transient_error_exhausting_retries_propagates() -> None:
    fake = FlakyLLMClient("research.hypothesis", failures=3)
    _seed_full_run(fake)
    graph, config = _graph(fake)

    with pytest.raises(ConnectionError):
        await graph.ainvoke(empty_state(QUESTION), config=config)


async def test_compiled_graph_is_reusable_across_threads() -> None:
    fake = FakeLLMClient()
    _seed_full_run(fake)
    graph, cfg1 = _graph(fake)
    cfg2 = {"configurable": {"thread_id": str(uuid4())}}

    f1 = await graph.ainvoke(empty_state(QUESTION), config=cfg1)
    f2 = await graph.ainvoke(empty_state(QUESTION), config=cfg2)

    assert content(*f1["hypotheses"], *f2["hypotheses"]) == content(HYPOTHESIS, HYPOTHESIS)
    assert f1["hypotheses"][0].id != f2["hypotheses"][0].id
