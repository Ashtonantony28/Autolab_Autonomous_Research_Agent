"""AUDIT-4: tests that kill mutants which survived the existing suite.

Each test names the mutation it kills (see AUDIT-4.md, mutation table).
All of them pass on the current code.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
import z3
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from autolab.agents.research import ResearchAgent, build_hypothesis_prompt, build_trace_prompt
from autolab.agents.verification_monitor import AgentDecision, VerificationMonitor
from autolab.config import PASS_THRESHOLD
from autolab.orchestrator.checkpoints import (
    contradiction_trace,
    contradiction_trigger,
    hitl_node,
    new_in_process_saver,
)
from autolab.state.models import HITLRequest, HITLResolution, Hypothesis
from autolab.state.schema import AutoLabState, empty_state
from autolab.verification.llm import FakeLLMClient
from autolab.verification.solver import SmtFormula, Z3Solver
from autolab.verification.vericot import Verifier
from tests.fixtures.agent_helpers import register_trace, register_trivial_trace
from tests.fixtures.golden_traces import GoldenStep
from tests.integration.orchestrator.test_graph import (
    FAILING_SUMMARY,
    HYPOTHESIS,
    QUESTION,
    _agents,
    _fail_design_trace,
    _graph,
    _interrupt,
    _seed_full_run,
)

# --------------------------------------------------------------------------
# M02: solver `unknown` on the entailment check mutated to VALID.
# Only wall-clock tests in test_latency.py caught it; these are deterministic.
# --------------------------------------------------------------------------

_PQ = SmtFormula("(declare-const P Bool)\n(declare-const Q Bool)", "P")


def _unknown_on_call(monkeypatch: pytest.MonkeyPatch, which: int) -> None:
    """Make the `which`-th (1-based) `z3.Solver.check` call of a check return `unknown`."""
    real = z3.Solver.check
    calls = 0

    def check(self: z3.Solver, *args: Any) -> z3.CheckSatResult:
        nonlocal calls
        calls += 1
        return z3.unknown if calls == which else real(self, *args)

    monkeypatch.setattr(z3.Solver, "check", check)


@pytest.mark.parametrize(
    ("which", "claim"),
    [(1, "P"), (2, "P"), (2, "Q")],
    ids=["consistency-unknown", "entailment-unknown-true-claim", "entailment-unknown-false-claim"],
)
def test_solver_unknown_is_never_valid(
    monkeypatch: pytest.MonkeyPatch, which: int, claim: str
) -> None:
    """Z3 `unknown` on either check is UNGROUNDED/timeout, whatever the claim (kills M02)."""
    _unknown_on_call(monkeypatch, which)

    out = Z3Solver().check_entailment([_PQ], SmtFormula(_PQ.declarations, claim))

    assert (out.verdict, out.reason) == ("UNGROUNDED", "timeout")


# --------------------------------------------------------------------------
# M07: BaseAgent `score < hitl_threshold` mutated to `<=`.
# --------------------------------------------------------------------------


def _seed_hypothesis(fake: FakeLLMClient) -> None:
    fake.register(
        "research.hypothesis", build_hypothesis_prompt(QUESTION), HYPOTHESIS.model_dump_json()
    )


async def test_agent_score_equal_to_threshold_passes() -> None:
    """A trace scoring exactly the threshold is admitted, not withheld (kills M07)."""
    fake = FakeLLMClient()
    _seed_hypothesis(fake)
    register_trivial_trace(
        fake,
        agent="research",
        trace_purpose="research.trace",
        trace_prompt=build_trace_prompt(QUESTION, HYPOTHESIS),
        conclusion=HYPOTHESIS.statement,
    )

    agent = ResearchAgent(verifier=Verifier(fake), llm=fake, hitl_threshold=1.0)
    update = await agent(empty_state(QUESTION))

    assert update["confidence"] == 1.0
    assert update["hitl_pending"] is None
    assert update["hypotheses"] == [HYPOTHESIS]


async def test_agent_and_monitor_agree_at_the_default_pass_threshold() -> None:
    """Score == PASS_THRESHOLD: the monitor says PASS, so the agent must not pause (kills M07).

    4 VALID + 6 UNGROUNDED (unparseable SMT) steps: (4 + 3) / 10 == 0.7. The conclusion
    is entailed by two premises jointly, so it is not circular (AUDIT-1 #1).
    """
    fake = FakeLLMClient()
    _seed_hypothesis(fake)
    noise = [GoldenStep(f"noise {i}", "inference", "", f"undeclared_{i}") for i in range(6)]
    register_trace(
        fake,
        agent="research",
        trace_purpose="research.trace",
        trace_prompt=build_trace_prompt(QUESTION, HYPOTHESIS),
        steps=(
            GoldenStep(
                "Y is present",
                "premise",
                "(declare-const audit_P Bool)\n(declare-const audit_Q Bool)",
                "audit_P",
            ),
            GoldenStep("Y implies the hypothesis", "premise", "", "(=> audit_P audit_Q)"),
            GoldenStep("Z is present", "premise", "(declare-const audit_R Bool)", "audit_R"),
            *noise,
            GoldenStep(HYPOTHESIS.statement, "conclusion", "", "audit_Q", (0, 1)),
        ),
    )

    update = await ResearchAgent(verifier=Verifier(fake), llm=fake)(empty_state(QUESTION))

    [verdict] = update["verification_history"]
    assert [v.verdict for v in verdict.step_verdicts] == [
        "VALID",
        "VALID",
        "VALID",
        *["UNGROUNDED"] * 6,
        "VALID",
    ]
    assert verdict.consistency_score == PASS_THRESHOLD
    assert VerificationMonitor().decide(verdict) is AgentDecision.PASS
    assert update["hitl_pending"] is None
    assert update["hypotheses"] == [HYPOTHESIS]


# --------------------------------------------------------------------------
# M14: contradiction trigger mutated to fire on any INVALID last step.
# --------------------------------------------------------------------------


async def test_contradiction_trigger_ignores_non_contradiction_failures() -> None:
    """Only `premises_inconsistent` is a contradiction; `not_entailed` is not (kills M14)."""
    prior = Hypothesis(question=QUESTION, statement="Y is present", rationale="r")
    fake = FakeLLMClient()
    trace = contradiction_trace([prior], HYPOTHESIS)
    register_trace(
        fake,
        agent=trace.agent,
        trace_purpose="unused",
        trace_prompt="unused",
        steps=(
            GoldenStep(prior.statement, "premise", "(declare-const hyp_Y Bool)", "hyp_Y"),
            GoldenStep(HYPOTHESIS.statement, "premise", "", "hyp_Y"),
            # The restated hypothesis formalizes to an unrelated atom: not entailed,
            # but the prior and new statements are jointly satisfiable.
            GoldenStep(
                HYPOTHESIS.statement, "inference", "(declare-const hyp_Z Bool)", "hyp_Z", (0, 1)
            ),
        ),
    )
    verifier = Verifier(fake)
    result = await verifier.verify(trace)
    assert (result.step_verdicts[-1].verdict, result.step_verdicts[-1].reason) == (
        "INVALID",
        "not_entailed",
    )

    state = empty_state(QUESTION)
    state["hypotheses"] = [prior]
    assert await contradiction_trigger(verifier)(HYPOTHESIS, state) is None


# --------------------------------------------------------------------------
# M16: hitl_node no longer resets `retry_count`.
# --------------------------------------------------------------------------


async def test_human_retry_restores_the_automatic_retry_budget() -> None:
    """After a human `retry`, the agent gets `max_retries` fresh automatic retries (kills M16)."""
    note = "try a different protocol"
    fake = FakeLLMClient()
    _seed_full_run(fake)
    _fail_design_trace(fake)
    _fail_design_trace(fake, feedback=FAILING_SUMMARY)
    _fail_design_trace(fake, feedback=note)
    graph, config = _graph(fake)

    first = await graph.ainvoke(empty_state(QUESTION), config=config)
    assert _agents(first).count("design") == 3
    second = await graph.ainvoke(
        Command(resume=HITLResolution(action="retry", note=note)), config=config
    )

    # note-driven attempt + 2 automatic retries, then escalate again.
    assert _agents(second).count("design") == 6
    assert second["retry_count"] == 2
    assert _interrupt(second)["agent"] == "design"


# --------------------------------------------------------------------------
# M21: hitl_node `retry` no longer checks the agent is a graph node.
# --------------------------------------------------------------------------


async def test_hitl_retry_for_unknown_agent_ends_the_run() -> None:
    """A retry for a request whose agent is not a node ends the run (kills M21)."""
    graph: StateGraph[AutoLabState, AutoLabState, AutoLabState] = StateGraph(AutoLabState)
    graph.add_node("hitl", hitl_node, destinations=(END,))
    graph.add_edge(START, "hitl")
    app: Any = graph.compile(checkpointer=new_in_process_saver())
    config = {"configurable": {"thread_id": str(uuid4())}}
    state = empty_state(QUESTION)
    state["hitl_pending"] = HITLRequest(agent="ghost", reason="r", payload={"trigger": "error"})

    paused = await app.ainvoke(state, config=config)
    assert _interrupt(paused)["agent"] == "ghost"
    final = await app.ainvoke(Command(resume={"action": "retry"}), config=config)

    # LangGraph only logs (does not raise) on a `goto` to an unknown node, so the
    # observable difference is that the abort path leaves no stale feedback.
    assert "__interrupt__" not in final
    assert final["hitl_pending"] is None
    assert final["current_agent"] == "hitl"
    assert final["feedback"] == ""
