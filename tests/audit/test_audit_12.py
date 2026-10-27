"""AUDIT-12: tests that kill mutants which survived the existing suite (round 3).

Each plain test names the mutation it kills (see AUDIT-12.md, mutation table) and
passes on the current code. The strict xfail is a repro for a weakened audit test.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import z3
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from autolab import cli
from autolab.agents.common import design_claim, design_facts, with_feedback
from autolab.agents.design import DesignAgent, build_design_prompt, build_design_trace_prompt
from autolab.agents.research import build_hypothesis_prompt, build_trace_prompt
from autolab.config import get_settings
from autolab.orchestrator import graph as graph_module
from autolab.orchestrator.checkpoints import (
    cost_trigger,
    make_hitl_node,
    merged_policy,
    new_in_process_saver,
)
from autolab.orchestrator.graph import build_graph
from autolab.state.models import (
    ExperimentDesign,
    HITLRequest,
    HITLResolution,
    Hypothesis,
    ReasoningStep,
)
from autolab.state.schema import AutoLabState, empty_state
from autolab.verification import formalizer as formalizer_mod
from autolab.verification import solver_workers
from autolab.verification.formalizer import FormalizationError, Formalizer, Vocabulary
from autolab.verification.llm import FakeLLMClient
from autolab.verification.solver import SmtFormula, SolverExecutor, Z3Solver
from autolab.verification.vericot import Verifier
from scripts.audit_load import NO_CUBES, POSITIVE, XYZ
from tests.fixtures.agent_helpers import register_failing_trace, register_trace
from tests.fixtures.golden_traces import GoldenStep
from tests.integration.orchestrator import test_graph as tg

# --------------------------------------------------------------------------
# F02-F14: `test_merge_rejects_reserved_symbols` is parametrized over
# `RESERVED_SYMBOLS` itself, so deleting an entry deletes its test case. This
# list is written out independently of the set.
# --------------------------------------------------------------------------

CORE_BUILTINS = (
    "true", "false", "not", "=>", "and", "or", "xor", "=", "distinct", "ite",
    "+", "-", "*", "/", "div", "mod", "abs", "<", "<=", ">", ">=",
    "to_real", "to_int", "is_int", "select", "store",
    "implies", "iff", "if", "rem", "^", "forall", "exists", "let", "assert",
)  # fmt: skip


@pytest.mark.parametrize("name", CORE_BUILTINS)
def test_each_core_builtin_is_reserved_by_name(name: str) -> None:
    with pytest.raises(FormalizationError, match="reserved symbol"):
        Vocabulary().merged(f"(declare-fun {name} (Real Real) Bool)")


# --------------------------------------------------------------------------
# F16: `Formalizer.formalize` must parse in its per-thread context. Only the
# subprocess crash test in test_audit_6 noticed a parse in Z3's global
# context, and only when the race happened to fire.
# --------------------------------------------------------------------------


async def test_formalize_parses_in_the_per_thread_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts: list[object] = []
    real_parse = z3.parse_smt2_string

    def spy(*args: Any, **kwargs: Any) -> Any:
        contexts.append(kwargs.get("ctx"))
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(z3, "parse_smt2_string", spy)
    fake = FakeLLMClient()
    step = ReasoningStep(index=0, claim="p holds", claim_type="premise")
    fake.register(
        "formalize",
        formalizer_mod.prompt_for(step, Vocabulary()),
        json.dumps({"declarations": "(declare-const p Bool)", "assertion": "p"}),
    )

    await Formalizer(fake).formalize(step)

    assert contexts == [formalizer_mod._parse_context()]
    assert contexts[0] is not z3.main_ctx()


# --------------------------------------------------------------------------
# H02: approving a verification escalation of an agent with no policy
# (execute, analysis) must merge, not call a missing policy.
# --------------------------------------------------------------------------


async def test_approving_an_agent_without_a_policy_merges_its_payload() -> None:
    graph: StateGraph[AutoLabState, AutoLabState, AutoLabState] = StateGraph(AutoLabState)
    cost = merged_policy(cost_trigger(10.0, 60.0), "current_experiment")
    graph.add_node("hitl", make_hitl_node({"design": cost}), destinations=("analysis", END))
    graph.add_node("analysis", lambda _s: {"current_agent": "analysis"})
    graph.add_edge(START, "hitl")
    graph.add_edge("analysis", END)
    app: Any = graph.compile(checkpointer=new_in_process_saver())
    config = {"configurable": {"thread_id": str(uuid4())}}
    state = empty_state("q")
    state["hitl_pending"] = HITLRequest(
        agent="execute", reason="blocked", payload={"trigger": "verification"}
    )
    state["hitl_payload"] = {"results": [tg.RESULT]}
    await app.ainvoke(state, config=config)

    final = await app.ainvoke(Command(resume={"action": "approve"}), config=config)

    assert "__interrupt__" not in final
    assert final["results"] == [tg.RESULT]
    assert final["current_agent"] == "analysis"


# --------------------------------------------------------------------------
# P01/P03: `build_graph` must give the hitl node the Research contradiction
# policy, read from the withheld `hypotheses` list. Only the Design policy was
# tested end to end (AUDIT-5 #3).
# --------------------------------------------------------------------------


async def test_approving_a_research_escalation_still_runs_the_contradiction_trigger() -> None:
    prior = Hypothesis(question=tg.QUESTION, statement="Y plays no role in X", rationale="r")
    fake = FakeLLMClient()
    tg._seed_full_run(fake)
    for feedback in ("", tg.FAILING_SUMMARY):
        fake.register(
            "research.hypothesis",
            with_feedback(build_hypothesis_prompt(tg.QUESTION), feedback),
            tg.HYPOTHESIS.model_dump_json(),
        )
        register_failing_trace(
            fake,
            agent="research",
            trace_purpose="research.trace",
            trace_prompt=with_feedback(build_trace_prompt(tg.QUESTION, tg.HYPOTHESIS), feedback),
            conclusion=tg.HYPOTHESIS.statement,
        )
    tg._seed_contradiction_check(fake, prior, "(not hyp_Y)")
    graph, config = tg._graph(fake)

    paused = await graph.ainvoke(tg._with_prior(prior), config=config)
    assert tg._interrupt(paused)["payload"]["trigger"] == "verification"
    assert tg._interrupt(paused)["agent"] == "research"

    after: dict[str, Any] = await graph.ainvoke(
        Command(resume=HITLResolution(action="approve")), config=config
    )

    pause = tg._interrupt(after)
    assert pause["payload"]["trigger"] == "policy"
    assert pause["reason"] == "hypothesis contradicts 1 prior hypothesis(es) in state"
    assert after["hypotheses"] == [prior]


# --------------------------------------------------------------------------
# P05: `hitl` routes to itself on a re-pause, so the compiled graph must list
# that edge (it is what `get_graph()` draws for an auditor).
# --------------------------------------------------------------------------


def test_compiled_graph_draws_the_hitl_self_edge() -> None:
    graph = build_graph(llm=FakeLLMClient(), retry_backoff_seconds=0)
    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    assert ("hitl", "hitl") in edges
    assert ("hitl", "execute") in edges


# --------------------------------------------------------------------------
# C01, C08, C11: the CLI's crash-resume contract. `tests/unit/test_cli.py`
# patches `uuid4` to a constant, so the printed id and the run's id can differ
# unseen; capsys never shows whether the line was flushed before the run.
# --------------------------------------------------------------------------


def _autolab(argv: list[str]) -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(cli.main, argv).result()


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[pytest.MonkeyPatch]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'store.db'}")
    monkeypatch.setattr(graph_module, "LabSimulator", lambda: tg.FixedResultSimulator(tg.RESULT))
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_printed_thread_id_is_the_one_the_run_checkpoints_under(
    cli_env: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = iter(UUID(int=i) for i in range(1, 100))
    cli_env.setattr(cli, "uuid4", lambda: next(ids))
    llm = tg.FlakyLLMClient("design.experiment", failures=3)
    tg._seed_full_run(llm)
    cli_env.setattr(cli, "_make_llm", lambda _key: llm)
    ckpt = str(tmp_path / "ckpt.db")

    with pytest.raises(ConnectionError):
        _autolab(["run", tg.QUESTION, "--checkpoints", ckpt])
    printed = capsys.readouterr().out.split()[1]

    _autolab(["resume", printed, "approve", "--checkpoints", ckpt])

    captured = capsys.readouterr()
    assert f"Run {printed} complete." in captured.out
    assert f"run {printed} is not paused; ignoring 'approve' and continuing" in captured.err


class _FlushRecorder(io.StringIO):
    """Records what had been written each time the stream was flushed."""

    def __init__(self) -> None:
        super().__init__()
        self.flushed: list[str] = []

    def flush(self) -> None:
        self.flushed.append(self.getvalue())
        super().flush()


def test_thread_id_is_flushed_before_the_graph_runs(
    cli_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = _FlushRecorder()
    seen_at_start: list[list[str]] = []

    async def drive(*_args: Any, **_kwargs: Any) -> None:
        seen_at_start.append(list(out.flushed))
        raise ConnectionError("outage")

    cli_env.setattr(cli, "_make_llm", lambda _key: FakeLLMClient())
    cli_env.setattr(cli, "uuid4", lambda: UUID(int=7))
    cli_env.setattr(cli, "_drive", drive)
    cli_env.setattr(sys, "stdout", out)

    with pytest.raises(ConnectionError):
        _autolab(["run", tg.QUESTION, "--checkpoints", str(tmp_path / "ckpt.db")])

    assert seen_at_start == [[f"Run {UUID(int=7)} started.\n"]]


# --------------------------------------------------------------------------
# AUDIT-12 #1: the AUDIT-5 #2 repro was weakened when its xfail was removed.
# Its trace concludes the bare protocol, so after SG12 it is merely unbound and
# never reaches the solver. A model that follows the prompt concludes
# `design_claim(design)` instead. The same "240 minutes / 500 USD" protocol,
# formalized under symbols unrelated to the code-built facts, then verifies at
# 1.0 and the $5 / 30 min fields skip the cost pause, which is what AUDIT-5 #2
# reported.
# --------------------------------------------------------------------------


async def test_design_concluding_the_bound_claim_cannot_hide_its_protocol_cost() -> None:
    protocol = "Incubate the plate for 240 minutes using 500 USD of reagent"
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
            GoldenStep(design_claim(design), "conclusion", "", "chosen", (0, 1)),
        ),
        facts=design_facts(design),
    )
    agent = DesignAgent(verifier=Verifier(fake), llm=fake, hitl_trigger=cost_trigger(10.0, 60.0))
    state: AutoLabState = {**empty_state(tg.QUESTION), "hypotheses": [tg.HYPOTHESIS]}

    update = await agent(state)

    # Before SG17: confidence 1.0, no pause, and the design was merged.
    assert update["hitl_pending"] is not None
    assert "current_experiment" not in update


# --------------------------------------------------------------------------
# AUDIT-12 #2: SG14's deterministic cancel/shutdown tests assert only that
# `z3.Context.interrupt()` was called, not that it stopped the check. Z3 drops
# an interrupt that arrives before `Solver.check()` starts, so a cancel that
# lands between the consistency check and the entailment check is lost and the
# worker stays busy until the Z3 timeout. The spy in `test_solver_executor.py`
# signals exactly in that window, which is why those tests sometimes take 20 s
# (the `_HANG_S` timeout) and still pass.
# --------------------------------------------------------------------------

_WAIT_S = 10.0


async def test_cancel_between_the_two_z3_checks_still_stops_the_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Since SG29 a cancel kills the check's worker (ADR 0009), wherever in the check
    # it lands; here it lands once the worker has the request and is solving it.
    executor = SolverExecutor(1)
    reached, done = threading.Event(), threading.Event()
    elapsed: list[float] = []
    real_call = solver_workers.Worker.call

    def call(worker: solver_workers.Worker, args: tuple[Any, ...], budget_s: float) -> Any:
        reached.set()
        started = time.perf_counter()
        try:
            return real_call(worker, args, budget_s)
        finally:
            elapsed.append(time.perf_counter() - started)
            done.set()

    monkeypatch.setattr(solver_workers.Worker, "call", call)

    def undecidable() -> object:
        solver = Z3Solver(timeout_seconds=3.0)
        return solver.check_entailment([SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES))

    try:
        task = asyncio.create_task(executor.run(undecidable))
        assert await asyncio.to_thread(reached.wait, _WAIT_S)
        await asyncio.sleep(0.1)  # inside the worker's checks
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(done.wait, _WAIT_S)
    finally:
        await asyncio.to_thread(executor.shutdown)

    # Before SG19: about 3.0 s, the full Z3 timeout; a cancelled check returns at once.
    assert elapsed[0] < 1.0
