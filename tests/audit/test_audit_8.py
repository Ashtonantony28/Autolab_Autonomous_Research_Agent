"""AUDIT-8: tests that kill mutants which survived the existing suite (round 2).

Each test names the mutation it kills (see AUDIT-8.md, mutation table).
All of them pass on the current code.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from typing import Any
from uuid import uuid4

import pytest

from autolab.agents.analysis import AnalysisAgent, build_analysis_prompt
from autolab.agents.analysis import build_analysis_trace_prompt as analysis_trace_prompt
from autolab.agents.common import measurement_facts
from autolab.agents.research import ResearchAgent, build_hypothesis_prompt, build_trace_prompt
from autolab.config import Settings
from autolab.orchestrator.graph import build_graph
from autolab.orchestrator.recovery import AgentNode, escalate_permanent_errors, with_retries
from autolab.state.models import (
    AnalysisReport,
    ExperimentResult,
    Interpretation,
    Measurement,
    VerificationContext,
)
from autolab.state.schema import AutoLabState, empty_state
from autolab.verification import solver as solver_mod
from autolab.verification.confidence import score_cap
from autolab.verification.formalizer import FormalizationError, Vocabulary
from autolab.verification.llm import FakeLLMClient
from autolab.verification.solver import SolverExecutor
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import register_failing_trace, register_trace
from tests.fixtures.golden_traces import GoldenStep
from tests.integration.orchestrator.test_graph import HYPOTHESIS, QUESTION

CLAIM = "C holds"
_PC = "(declare-const p Bool)\n(declare-const c Bool)"


def _trace(*steps: GoldenStep) -> tuple[Any, FakeLLMClient]:
    fake = FakeLLMClient()
    trace = register_trace(fake, agent="t", trace_purpose="t.trace", trace_prompt="", steps=steps)
    return trace, fake


# --------------------------------------------------------------------------
# V03: the circularity scan must strip premises only, not a VALID inference
# that (correctly) derives the conclusion.
# --------------------------------------------------------------------------


async def test_bound_trace_with_an_entailed_intermediate_inference_passes() -> None:
    trace, fake = _trace(
        GoldenStep("P holds", "premise", _PC, "p"),
        GoldenStep("P implies C", "premise", "", "(=> p c)"),
        GoldenStep("so C", "inference", "", "c", (0, 1)),
        GoldenStep(CLAIM, "conclusion", "", "c", (2,)),
    )
    result = await Verifier(fake).verify(trace, context=VerificationContext(payload_claim=CLAIM))
    assert [f"{v.verdict}:{v.reason}" for v in result.step_verdicts] == [
        "VALID:assumed",
        "VALID:assumed",
        "VALID:entailed",
        "VALID:entailed",
    ]
    assert result.consistency_score == 1.0


# --------------------------------------------------------------------------
# V08: a bound trace whose conclusion is not VALID is capped in the score itself
# (the persisted/displayed `confidence`), not only blocked by the monitor.
# --------------------------------------------------------------------------


async def test_unproven_bound_conclusion_is_capped_in_the_score() -> None:
    trace, fake = _trace(
        *(GoldenStep(f"Q{i}", "premise", f"(declare-const q{i} Bool)", f"q{i}") for i in range(9)),
        GoldenStep(CLAIM, "conclusion", "", "(c", (0,)),  # fails to formalize
    )
    result = await Verifier(fake).verify(trace, context=VerificationContext(payload_claim=CLAIM))
    assert result.step_verdicts[-1].reason == "formalization_failed"
    # Uncapped this would be (9 + 0.5) / 10 = 0.95.
    assert result.consistency_score == score_cap()


# --------------------------------------------------------------------------
# G01: build_graph passes the configured thresholds to the Verifier, so the
# cap on a refuted trace follows Settings (AUDIT-3 #1 end to end).
# --------------------------------------------------------------------------


async def test_graph_caps_refuted_traces_below_the_configured_thresholds() -> None:
    fake = FakeLLMClient()
    fake.register(
        "research.hypothesis", build_hypothesis_prompt(QUESTION), HYPOTHESIS.model_dump_json()
    )
    register_failing_trace(
        fake,
        agent="research",
        trace_purpose="research.trace",
        trace_prompt=build_trace_prompt(QUESTION, HYPOTHESIS),
        conclusion=HYPOTHESIS.statement,
    )
    cfg = Settings(pass_threshold=0.3, retry_threshold=0.2, max_retries=0)
    graph = build_graph(llm=fake, settings=cfg, retry_backoff_seconds=0.0)

    final = await graph.ainvoke(
        empty_state(QUESTION), {"configurable": {"thread_id": str(uuid4())}}
    )

    [verdict] = final["verification_history"]
    assert any(v.verdict == "INVALID" for v in verdict.step_verdicts)
    # Raw score 0.5; the default-threshold cap (0.39) would sit above pass_threshold=0.3.
    assert verdict.consistency_score == pytest.approx(score_cap(0.3, 0.2))
    assert verdict.consistency_score < cfg.retry_threshold


# --------------------------------------------------------------------------
# F01: a `;` comment inside a declaration form is rejected. Z3 would drop the
# rest of the line, including the form's closing parenthesis, so the next
# line of the SMT document would be read as part of this declaration.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("decl", ["(declare-const k Bool ; )", "(declare-fun f (Int ; ) Bool)"])
def test_comment_inside_a_declaration_form_is_rejected(decl: str) -> None:
    with pytest.raises(FormalizationError, match="unrecognized declaration"):
        Vocabulary().merge(decl)


# --------------------------------------------------------------------------
# S01: the per-loop semaphore bounds the number of checks *submitted* to the
# pool, not just the number running (backpressure: queued callers wait as
# coroutines, so cancelling them never leaves work in the pool's queue).
# --------------------------------------------------------------------------


async def test_at_most_workers_checks_are_ever_submitted_to_the_pool() -> None:
    executor = SolverExecutor(2)
    outstanding: list[Future[Any]] = []
    peak = 0
    real_submit = executor._pool.submit

    def submit(fn: Callable[..., Any], /, *args: Any) -> Future[Any]:
        nonlocal peak
        outstanding[:] = [f for f in outstanding if not f.done()]
        future = real_submit(fn, *args)
        outstanding.append(future)
        peak = max(peak, len(outstanding))
        return future

    setattr(executor._pool, "submit", submit)  # noqa: B010 - instance-level spy
    try:
        outs = await asyncio.gather(*(executor.run(lambda: time.sleep(0.01)) for _ in range(20)))
    finally:
        executor.shutdown()
    assert len(outs) == 20
    assert peak <= 2


# --------------------------------------------------------------------------
# S08: Z3 reads a timeout of 0 as "no limit"; an exhausted deadline must still
# give the second check a bounded (1 ms) budget.
# --------------------------------------------------------------------------


def test_exhausted_deadline_never_becomes_an_unlimited_z3_timeout() -> None:
    assert solver_mod._remaining_ms(time.monotonic() - 5.0) == 1
    assert solver_mod._remaining_ms(time.monotonic() + 2.0) > 1000


# --------------------------------------------------------------------------
# R08: backoff grows exponentially (the jitter is drawn from a growing range).
# --------------------------------------------------------------------------


@pytest.fixture
def upper_bound_sleeps(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Make the jitter draw its upper bound and record each sleep without sleeping."""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr("autolab.orchestrator.recovery.random.uniform", lambda a, b: b)
    yield recorded


async def test_backoff_range_doubles_each_attempt(upper_bound_sleeps: list[float]) -> None:
    async def op() -> None:
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        await with_retries(op, max_attempts=5, initial_backoff_seconds=0.5)
    assert upper_bound_sleeps == [0.5, 1.0, 2.0, 4.0]


# --------------------------------------------------------------------------
# R12 (hardening; the mutant was also killed, but only through the graph by
# test_transient_error_exhausting_retries_propagates): only the listed permanent
# errors become a HITL request. Exhausted non-HTTP transient errors and
# programming errors propagate (CLAUDE.md §9: no broad `except Exception`).
# --------------------------------------------------------------------------


def _raising(exc: BaseException) -> AgentNode:
    async def node(state: AutoLabState) -> dict[str, Any]:
        raise exc

    return node


@pytest.mark.parametrize(
    "exc", [ConnectionError("reset"), TimeoutError("slow"), TypeError("bug"), KeyError("k")]
)
async def test_non_permanent_errors_propagate_out_of_the_escalation_wrapper(
    exc: Exception,
) -> None:
    with pytest.raises(type(exc)):
        await escalate_permanent_errors("design", _raising(exc))(empty_state("q"))


# --------------------------------------------------------------------------
# K05: the report's result_id comes from the result the agent analysed (the
# latest), never from an older one.
# --------------------------------------------------------------------------


async def test_analysis_report_points_at_the_latest_result() -> None:
    older = ExperimentResult(
        design_id=uuid4(), status="success", measurements=(Measurement(name="m", value=1.0),)
    )
    latest = older.model_copy(update={"id": uuid4()})
    report = AnalysisReport(
        result_id=uuid4(),
        interpretations=(Interpretation(statement="The yield is positive", rank=0, likelihood=1),),
        summary="s",
    )
    fake = FakeLLMClient()
    fake.register("analysis.report", build_analysis_prompt(latest), report.model_dump_json())
    register_trace(
        fake,
        agent="analysis",
        trace_purpose="analysis.trace",
        trace_prompt=analysis_trace_prompt(latest, report),
        steps=(GoldenStep("The yield is positive", "conclusion", "", "(> m 0.0)"),),
        facts=measurement_facts(latest),
    )
    state = empty_state("Q?")
    state["results"] = [older, latest]

    update = await AnalysisAgent(verifier=Verifier(fake), llm=fake)(state)

    (out,) = update["analysis_reports"]
    assert out.result_id == latest.id != older.id


# --------------------------------------------------------------------------
# B01: the agent gate uses the monitor's blocking rule, not the score alone.
# With an agent threshold below the verifier's cap (0.39 at the default
# verifier thresholds), a refuted trace would otherwise be admitted on score.
# --------------------------------------------------------------------------


async def test_agent_withholds_a_refuted_trace_whose_capped_score_clears_its_gate() -> None:
    fake = FakeLLMClient()
    fake.register(
        "research.hypothesis", build_hypothesis_prompt(QUESTION), HYPOTHESIS.model_dump_json()
    )
    register_failing_trace(
        fake,
        agent="research",
        trace_purpose="research.trace",
        trace_prompt=build_trace_prompt(QUESTION, HYPOTHESIS),
        conclusion=HYPOTHESIS.statement,
    )

    update = await ResearchAgent(verifier=Verifier(fake), llm=fake, hitl_threshold=0.3)(
        empty_state(QUESTION)
    )

    assert update["confidence"] == pytest.approx(score_cap()) and score_cap() >= 0.3
    assert update["hitl_pending"] is not None
    assert update["hitl_pending"].payload["trigger"] == "verification"
    assert update["hitl_pending"].reason.startswith("step 1 is INVALID")
    assert "hypotheses" not in update
