"""AUDIT-16: tests that kill mutants which survived the existing suite (round 4).

Each plain test names the mutation it kills (see AUDIT-16.md, mutation table) and
passes on the current code. Each strict xfail is a repro for a finding.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime
from typing import Any, Final, cast
from uuid import uuid4

import pytest
import z3
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from autolab.agents.common import _param_symbol, design_claim, design_facts, protocol_figures
from autolab.agents.design import DesignAgent, build_design_prompt, build_design_trace_prompt
from autolab.monitoring.events import TraceEvent
from autolab.monitoring.tracer import RunTracer
from autolab.orchestrator.checkpoints import (
    HITLOutcome,
    cost_trigger,
    make_hitl_node,
    new_in_process_saver,
)
from autolab.orchestrator.graph import _hitl_observer
from autolab.state.models import (
    ExperimentDesign,
    HITLRequest,
    HITLResolution,
    ReasoningStep,
    VerificationContext,
)
from autolab.state.schema import AutoLabState, empty_state
from autolab.verification import solver as solver_mod
from autolab.verification import solver_workers
from autolab.verification.formalizer import (
    FormalizationError,
    Formalizer,
    Vocabulary,
    prompt_for,
)
from autolab.verification.llm import FakeLLMClient
from autolab.verification.solver import SmtFormula, SolverExecutor, Z3Solver
from autolab.verification.vericot import Verifier
from scripts.audit_load import NO_CUBES, POSITIVE, XYZ
from tests.fixtures.agent_helpers import register_failing_trace, register_trace
from tests.fixtures.golden_traces import GoldenStep
from tests.integration.orchestrator import test_graph as tg

_WAIT_S: Final = 10.0
"""Bounds a failing wait on an event; a passing run never waits this long."""
_HANG_S: Final = 20.0
"""Z3 timeout of the undecidable check: only an interrupt ends it sooner."""


# --------------------------------------------------------------------------
# AUDIT-16 #1 (high): `protocol_figures` misses durations written the usual
# adjectival way ("a 240-minute incubation", "a 12-hour soak") and in weeks.
# The AUDIT-12 #1 attack works again with one hyphen: an honest trace about a
# 240-minute protocol verifies at 1.0 and the 30-minute field skips the pause.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "protocol",
    ["incubate for a 240-minute period", "a 12-hour soak", "culture for 2 weeks", "5k USD"],
)
def test_protocol_figures_flags_hyphenated_durations_and_weeks(protocol: str) -> None:
    assert protocol_figures(protocol)


async def test_hyphenated_duration_in_the_protocol_cannot_skip_the_duration_pause() -> None:
    protocol = "Incubate the plate for a 240-minute period"
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
                "The incubation takes 240 minutes",
                "premise",
                "(declare-const minutes Real)\n(declare-const chosen Bool)",
                "(= minutes 240.0)",
            ),
            GoldenStep(
                "If the incubation takes 240 minutes, that protocol is chosen",
                "premise",
                "",
                "(=> (= minutes 240.0) chosen)",
            ),
            GoldenStep(design_claim(design), "conclusion", "", "chosen", (0, 1)),
        ),
        facts=design_facts(design),
    )
    agent = DesignAgent(verifier=Verifier(fake), llm=fake, hitl_trigger=cost_trigger(10.0, 60.0))
    state: AutoLabState = {**empty_state(tg.QUESTION), "hypotheses": [tg.HYPOTHESIS]}

    update = await agent(state)

    assert update["confidence"] == 1.0  # the trace is honest and fully verified
    # Today: no pause, and the 30-minute design is merged.
    assert update["hitl_pending"] is not None
    assert "current_experiment" not in update


# --------------------------------------------------------------------------
# F12: `")("` closes a term that never opened. Without the `depth < 0` guard the
# scanner accepts it and it reaches Z3's parser (which then rejects it).
# --------------------------------------------------------------------------


def _formalize_reply(assertion: str) -> tuple[Formalizer, ReasoningStep]:
    step = ReasoningStep(index=0, claim="claim", claim_type="premise")
    fake = FakeLLMClient()
    fake.register(
        "formalize",
        prompt_for(step, Vocabulary()),
        json.dumps(
            {
                "declarations": "(declare-const p Bool)\n(declare-const s String)",
                "assertion": assertion,
            }
        ),
    )
    return Formalizer(fake), step


async def test_a_term_closed_before_it_opens_is_refused_before_any_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assertion = ")("
    parses: list[str] = []
    monkeypatch.setattr(z3, "parse_smt2_string", lambda s, **_: parses.append(s))
    formalizer, step = _formalize_reply(assertion)
    with pytest.raises(FormalizationError, match="not exactly one term"):
        await formalizer.formalize(step)
    assert parses == []


# --------------------------------------------------------------------------
# F10: the `;` refusal is load-bearing. Z3 skips a `;` comment to the end of the
# line, but the scanner reads it as an atom and keeps counting parens inside it.
# A comment can therefore hide parens (so the scanner sees one balanced term) and
# put a newline between `(` and a command head (so the scanner never sees `(`
# directly before `set-option` or `declare-fun`). Without the refusal each of
# these is accepted as one term and runs its command in Z3 (AUDIT-9 #2 again).
# The SG15 table's only `;` case, "p ; comment", is refused by other rules too.
# --------------------------------------------------------------------------

_COMMENT_SMUGGLED: Final = (
    "(and p ;((\n)) (;\nset-option :rlimit 1) (;\nassert (and p) ;))\n",
    "(and p ;((\n)) (;\ndeclare-fun str.len (String) Int) (;\nassert (and p) ;))\n",
)


@pytest.mark.parametrize("assertion", _COMMENT_SMUGGLED)
async def test_a_command_hidden_by_comments_is_refused_before_any_parse(
    assertion: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    parses: list[str] = []
    monkeypatch.setattr(z3, "parse_smt2_string", lambda s, **_: parses.append(s))
    formalizer, step = _formalize_reply(assertion)
    with pytest.raises(FormalizationError, match="not exactly one term"):
        await formalizer.formalize(step)
    assert parses == []


def test_the_comment_smuggled_command_really_runs_in_z3() -> None:
    # The precondition of the test above: Z3 reads two assertions and the command.
    z3.reset_params()
    try:
        parsed = z3.parse_smt2_string(
            f"(declare-const p Bool)\n(assert {_COMMENT_SMUGGLED[0]})", ctx=z3.Context()
        )
        assert len(parsed) == 2
        assert z3.get_param("rlimit") == "1"
    finally:
        z3.reset_params()


# --------------------------------------------------------------------------
# V08: a premise that was UNGROUNDED in pass 1 (its consistency check was
# inconclusive) must keep that verdict in pass 2, like an INVALID one
# (AUDIT-9 #3). Treating only INVALID as "not VALID" lets the re-check, with the
# circular premise gone, assume it and prove the conclusion from it.
# --------------------------------------------------------------------------


class _InconclusiveWith(Z3Solver):
    """The premise-consistency check times out when the pool holds all of `together`."""

    def __init__(self, *together: str) -> None:
        super().__init__()
        self.together = set(together)

    def check_entailment(
        self, premises: list[SmtFormula], claim: SmtFormula
    ) -> solver_mod.SolverOutcome:
        pool = {p.assertion for p in premises}
        if claim.assertion == "true" and self.together <= pool:
            return solver_mod.SolverOutcome(
                verdict="UNGROUNDED", reason="timeout", details="unknown"
            )
        return super().check_entailment(premises, claim)


async def test_premise_ungrounded_in_pass_one_is_not_assumed_in_pass_two() -> None:
    claim = "C holds"
    fake = FakeLLMClient()
    trace = register_trace(
        fake,
        agent="t",
        trace_purpose="t.trace",
        trace_prompt="",
        steps=(
            GoldenStep(claim, "premise", "(declare-const c Bool)\n(declare-const a Bool)", "c"),
            GoldenStep("A holds", "premise", "", "a"),
            GoldenStep("A implies C", "premise", "", "(=> a c)"),
            GoldenStep(claim, "conclusion", "", "c", (0, 1, 2)),
        ),
    )
    verifier = Verifier(fake)
    verifier._solver = _InconclusiveWith("c", "a")

    result = await verifier.verify(trace, context=VerificationContext(payload_claim=claim))

    assert [f"{v.verdict}:{v.reason}" for v in result.step_verdicts] == [
        "UNGROUNDED:no_premises",
        "UNGROUNDED:timeout",
        "VALID:assumed",
        "INVALID:not_entailed",
    ]


# --------------------------------------------------------------------------
# S10, S11, S12, S13, S18: a cancel that lands before the check has started.
# In-process, Z3 dropped an interrupt sent before `Solver.check()` ran, so SG19
# re-sent it until the check ended (AUDIT-16 #2). Since SG29 a cancel kills the
# check's worker (ADR 0009), and there is no window to drop it in: here the request
# is held back until the caller has been cancelled, so the kill lands before the
# worker has the request, and the check must still stop at once and leave no thread.
# --------------------------------------------------------------------------

_CHECK_S: Final = 5.0
"""Z3 timeout of the held check; a killed check returns far sooner."""


class _HeldCheck:
    """Holds this test's request to its worker until its caller has been cancelled."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reached = threading.Event()
        self.cancelled = threading.Event()
        self.check_s: list[float] = []
        self.workers: list[solver_workers.Worker] = []
        real_call = solver_workers.Worker.call

        def call(worker: solver_workers.Worker, args: tuple[Any, ...], budget_s: float) -> Any:
            self.workers.append(worker)
            self.reached.set()
            self.cancelled.wait(_WAIT_S)
            started = time.perf_counter()
            try:
                return real_call(worker, args, budget_s)
            finally:
                self.check_s.append(time.perf_counter() - started)

        monkeypatch.setattr(solver_workers.Worker, "call", call)

    def undecidable(self) -> solver_mod.SolverOutcome:
        solver = Z3Solver(timeout_seconds=_CHECK_S)
        return solver.check_entailment([SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES))


async def _cancel_held_check(held: _HeldCheck, executor: SolverExecutor) -> None:
    task = asyncio.create_task(executor.run(held.undecidable))
    assert await asyncio.to_thread(held.reached.wait, _WAIT_S)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    held.cancelled.set()
    deadline = time.monotonic() + _WAIT_S
    while not held.check_s and time.monotonic() < deadline:
        await asyncio.sleep(0.01)


async def test_a_cancel_before_the_worker_has_the_request_stops_the_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = _HeldCheck(monkeypatch)
    executor = SolverExecutor(1)
    try:
        await _cancel_held_check(held, executor)
    finally:
        await asyncio.to_thread(executor.shutdown)
    assert held.check_s and held.check_s[0] < _CHECK_S / 2
    assert not held.workers[0].alive()


async def test_a_cancelled_check_leaves_no_thread_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = set(threading.enumerate())
    held = _HeldCheck(monkeypatch)
    executor = SolverExecutor(1)
    try:
        await _cancel_held_check(held, executor)
    finally:
        await asyncio.to_thread(executor.shutdown)
    left = [t for t in set(threading.enumerate()) - before if t.name.startswith("z3")]
    for t in left:
        await asyncio.to_thread(t.join, 1.0)
    assert not [t for t in left if t.is_alive()]


async def test_a_cancel_before_the_context_is_attached_refuses_the_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    real_call = solver_workers.Worker.call

    def call(worker: solver_workers.Worker, args: tuple[Any, ...], budget_s: float) -> Any:
        calls.append(args)
        return real_call(worker, args, budget_s)

    monkeypatch.setattr(solver_workers.Worker, "call", call)
    entered, go = threading.Event(), threading.Event()
    outcome: list[solver_mod.SolverOutcome] = []

    def late() -> None:
        entered.set()
        go.wait(_WAIT_S)  # the cancel lands here, before `check_entailment` attaches
        solver = Z3Solver(timeout_seconds=_CHECK_S)
        outcome.append(
            solver.check_entailment([SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES))
        )

    executor = SolverExecutor(1)
    try:
        task = asyncio.create_task(executor.run(late))
        assert await asyncio.to_thread(entered.wait, _WAIT_S)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        go.set()
    finally:
        await asyncio.to_thread(executor.shutdown)
    assert outcome and outcome[0].details == "interrupted"
    assert calls == []


# --------------------------------------------------------------------------
# S04: the solver names the negated claim `claim!` so that no formalized step can
# declare the same constant (a duplicate declaration makes every check of that
# trace a parse error). Pinned against the formalizer's own rule, so renaming it
# to any declarable identifier fails here.
# --------------------------------------------------------------------------


def test_the_negated_claim_name_cannot_be_declared_by_a_step() -> None:
    with pytest.raises(FormalizationError):
        Vocabulary().merged(f"(declare-const {solver_mod._NEGATED_CLAIM} Bool)")


# --------------------------------------------------------------------------
# A05, A06, A07: `protocol_figures` details the SG17 table does not pin. Every
# rejected example is in the regex's own case, so dropping IGNORECASE (A05) lets
# "500 usd" or "2 Hours" through. The reported figure is the whole number (A06),
# and a currency word must end at a word boundary (A07: "10 Eurofins primers" is
# a vendor, not a price).
# --------------------------------------------------------------------------


@pytest.mark.parametrize("protocol", ["spend 500 usd on reagent", "incubate 2 Hours", "wait 5 MIN"])
def test_protocol_figures_ignore_case(protocol: str) -> None:
    assert protocol_figures(protocol)


def test_protocol_figures_report_the_whole_number() -> None:
    assert protocol_figures("reagent at $1,200.50, then 1.5 hrs") == ("$1,200.50", "1.5 hrs")


def test_protocol_figures_need_a_whole_currency_word() -> None:
    assert protocol_figures("order 10 Eurofins primers") == ()


# --------------------------------------------------------------------------
# A11: `_param_symbol` disambiguates names that sanitize to the same symbol with a
# hash suffix. 20 such names collide under any suffix of 4 bits (pigeonhole) and
# very likely under 8; the SG17 test uses 3 names, so a 1-digit suffix passes it.
# --------------------------------------------------------------------------


def test_parameter_symbols_stay_distinct_for_many_names_with_one_sanitized_form() -> None:
    names = [f"a{c}b" for c in "-. /:;,+*=!?@#%&~^|<"]
    assert len({_param_symbol(n) for n in names}) == len(names)


# --------------------------------------------------------------------------
# D01: a Design that is already escalated (its trace did not verify) and whose
# protocol states figures must say so in the request. Otherwise the human approves
# a plain verification escalation and the cost policy, which reads only the fields,
# lets the priced protocol through.
# --------------------------------------------------------------------------


async def test_an_escalated_design_with_protocol_figures_names_them_in_the_request() -> None:
    priced = ExperimentDesign(
        hypothesis_id=tg.HYPOTHESIS.id,
        protocol="incubate 240 minutes with $500 of reagent",
        estimated_cost_usd=5.0,
        estimated_duration_minutes=30.0,
    )
    fake = FakeLLMClient()
    fake.register("design.experiment", build_design_prompt(tg.HYPOTHESIS), priced.model_dump_json())
    register_failing_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(tg.HYPOTHESIS, priced),
        conclusion=design_claim(priced),
        facts=design_facts(priced),
    )
    state: AutoLabState = {**empty_state(tg.QUESTION), "hypotheses": [tg.HYPOTHESIS]}

    update = await DesignAgent(verifier=Verifier(fake), llm=fake)(state)

    assert update["confidence"] < 1.0
    assert "current_experiment" not in update
    assert "protocol states cost or duration figures" in update["hitl_pending"].reason
    assert update["hitl_payload"]["current_experiment"].protocol == priced.protocol


# --------------------------------------------------------------------------
# K02, K03: the `hitl` event's `started_at` and `latency_ms` cover the
# resolution, including the agent's policy re-check. The SG18 tests never look at
# either, so a zero latency or a timestamp taken after the policy ran passes them.
# --------------------------------------------------------------------------

_POLICY_S: Final = 0.05


async def test_hitl_outcome_times_the_resolution_including_the_policy() -> None:
    policy_started: list[datetime] = []
    outcomes: list[HITLOutcome] = []

    async def slow_policy(merged: dict[str, Any], state: AutoLabState) -> str | None:
        policy_started.append(datetime.now(tz=UTC))
        await asyncio.sleep(_POLICY_S)
        return None

    async def observe(outcome: HITLOutcome, state: AutoLabState, config: Any) -> None:
        outcomes.append(outcome)

    graph: StateGraph[AutoLabState, AutoLabState, AutoLabState] = StateGraph(AutoLabState)
    graph.add_node(
        "hitl", make_hitl_node({"ghost": slow_policy}, observer=observe), destinations=(END,)
    )
    graph.add_edge(START, "hitl")
    app: Any = graph.compile(checkpointer=new_in_process_saver())
    config = {"configurable": {"thread_id": str(uuid4())}}
    state = empty_state(tg.QUESTION)
    state["hitl_pending"] = HITLRequest(
        agent="ghost", reason="r", payload={"trigger": "verification"}
    )

    await app.ainvoke(state, config=config)
    await app.ainvoke(Command(resume={"action": "approve"}), config=config)

    assert len(policy_started) == 1 and len(outcomes) == 1
    assert outcomes[0].latency_ms >= _POLICY_S * 1000 * 0.9
    assert outcomes[0].started_at <= policy_started[0]


# --------------------------------------------------------------------------
# G10, G11: the traced `hitl` event carries the outcome's `started_at` and
# `latency_ms`. Without them it gets the model defaults (construction time and,
# for latency, a constant), and every SG18 test still passes.
# --------------------------------------------------------------------------


class _RecordingTracer:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    async def record_node(self, event: TraceEvent, question: str, *states: AutoLabState) -> None:
        self.events.append(event)


async def test_hitl_event_keeps_the_outcomes_timing() -> None:
    tracer = _RecordingTracer()
    observe = _hitl_observer(cast(RunTracer, tracer), None)
    assert observe is not None
    started = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    command: Command[Any] = Command(goto=END, update={"hitl_pending": None})
    outcome = HITLOutcome(
        request=HITLRequest(agent="design", reason="r", payload={"trigger": "verification"}),
        resolution=HITLResolution(action="approve"),
        command=command,
        started_at=started,
        latency_ms=123.5,
    )
    config: Any = {"configurable": {"thread_id": str(uuid4())}}

    await observe(outcome, empty_state(tg.QUESTION), config)

    [event] = tracer.events
    assert (event.started_at, event.latency_ms) == (started, 123.5)
