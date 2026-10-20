"""AUDIT-20: tests that kill mutants which survived the existing suite (round 5).

Each plain test names the mutation it kills (see AUDIT-20.md, mutation table) and
passes on the current code. Each strict xfail is a repro for a finding.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import Final, TypeVar, cast

import pytest
import z3
from sqlalchemy import text

from autolab.agents.common import _symbol, protocol_figures
from autolab.state.models import ReasoningStep
from autolab.state.store import SQLITE_BUSY_TIMEOUT_S, StateStore
from autolab.verification import formalizer
from autolab.verification import solver as solver_mod
from autolab.verification.formalizer import FormalizationError, Formalizer, prompt_for
from autolab.verification.llm import FakeLLMClient
from autolab.verification.solver import (
    SmtFormula,
    SolverExecutor,
    SolverOutcome,
    Z3Solver,
    _Interrupt,
)
from scripts.audit_load import NO_CUBES, POSITIVE, XYZ
from scripts.audit_load_2 import slow_check
from tests.audit import test_audit_13
from tests.audit import test_audit_14 as audit_14

T = TypeVar("T")

_WAIT_S: Final = 60.0
"""Upper bound on waiting for a precondition; reached only if the setup hangs."""


# --------------------------------------------------------------------------
# Finding #1 (high): the AUDIT-6 #7 repro is timing-dependent again after SG22.
# SG23 made it wait until both callers have submitted and one check is running.
# SG22 then let a check that runs past `_SLICE_S` raise the pool's thread limit. If
# the second caller submits after that, the pool starts a thread for it, nothing is
# queued, `shutdown` cancels nothing and the strict xfail XPASSes (seen in this
# audit's mutation pass under load). The bug is still there when a check queues.
# --------------------------------------------------------------------------


class _ObservedExecutor(SolverExecutor):
    """A `SolverExecutor` that signals once a check is running on its worker."""

    def __init__(self, slow_workers: int) -> None:
        super().__init__(1, slow_workers=slow_workers)
        self.running = threading.Event()

    def _call(self, fn: Callable[[], T], interrupt: _Interrupt) -> tuple[T, float]:
        def observed() -> T:
            self.running.set()
            return fn()

        return super()._call(observed, interrupt)


def _caller(executor: SolverExecutor, outcomes: list[str]) -> threading.Thread:
    def run() -> None:
        async def main() -> None:
            try:
                outcomes.append(await slow_check(executor, 3.0))
            except BaseException as exc:  # recorded: the assertion is about its type
                outcomes.append(type(exc).__name__)

        asyncio.run(main())

    thread = threading.Thread(target=run)
    thread.start()
    return thread


async def test_the_audit_6_7_setup_admits_a_schedule_where_nothing_is_queued() -> None:
    # The AUDIT-6 #7 repro's own executor, but the second caller submits once the
    # first check has left its slot. Both outcomes are `timeout`: the repro XPASSes.
    executor = _ObservedExecutor(slow_workers=1)
    outcomes: list[str] = []
    first = _caller(executor, outcomes)
    assert await asyncio.to_thread(executor.running.wait, _WAIT_S)
    while executor._slow == 0:
        await asyncio.sleep(0.01)
    second = _caller(executor, outcomes)
    while len(executor._running) < 2:
        await asyncio.sleep(0.01)
    await asyncio.to_thread(executor.shutdown)
    for t in (first, second):
        await asyncio.to_thread(t.join)
    assert outcomes == ["timeout", "timeout"]


async def test_shutdown_does_not_cancel_a_queued_caller_on_another_loop() -> None:
    # AUDIT-6 #7 with no slow slot, so the second check always waits in the pool.
    executor = _ObservedExecutor(slow_workers=0)
    outcomes: list[str] = []
    first = _caller(executor, outcomes)
    assert await asyncio.to_thread(executor.running.wait, _WAIT_S)
    second = _caller(executor, outcomes)
    while executor._pool._work_queue.qsize() == 0:
        await asyncio.sleep(0.01)
    await asyncio.to_thread(executor.shutdown)
    for t in (first, second):
        await asyncio.to_thread(t.join)
    assert len(outcomes) == 2
    assert "CancelledError" not in outcomes


# --------------------------------------------------------------------------
# Finding #2 (high): SG20 changed `_register_steps` in test_audit_13.py to skip any
# step whose assertion contains `:named`. The AUDIT-13 #2 repros then no longer
# register the attack's later steps. Against the pre-SG20 formalizer, or with the
# annotation ban removed (mutant F05), they fail with a FakeLLMClient KeyError,
# never on their assertions: they no longer show that a `:named` premise is
# caught. Here the ban is switched off and the repro must fail on its assertion.
# --------------------------------------------------------------------------


def _without_annotation_ban(monkeypatch: pytest.MonkeyPatch) -> None:
    real = formalizer._tokens

    def tokens(text: str) -> set[str]:
        return {t for t in real(text) if t != "!" and not t.startswith(":")}

    monkeypatch.setattr(formalizer, "_tokens", tokens)


@pytest.mark.parametrize(
    "repro",
    [
        test_audit_13.test_named_premise_cannot_redefine_false_for_later_steps,
        test_audit_13.test_named_prior_hypothesis_cannot_hide_a_contradiction,
    ],
)
async def test_audit_13_2_repros_fail_on_their_assertion_without_the_ban(
    monkeypatch: pytest.MonkeyPatch, repro: Callable[[], Awaitable[None]]
) -> None:
    _without_annotation_ban(monkeypatch)
    with pytest.raises(AssertionError):
        await repro()


# --------------------------------------------------------------------------
# Kill tests: solver.py (SG22). No wall-clock bound on the code under test: each
# waits on events or counts, and timing only sets margins of 0.1 s or more.
# --------------------------------------------------------------------------

_DECLS: Final = "(declare-const p Bool)"


def _hard(timeout_s: float) -> Callable[[], SolverOutcome]:
    """A check that runs to its timeout in Z3 (it holds a Z3 context the whole time)."""
    return partial(
        Z3Solver(timeout_s).check_entailment, [SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES)
    )


@pytest.mark.skipif(solver_mod.SOLVER_SLOW_WORKERS < 2, reason="fewer than 4 CPUs")
async def test_an_easy_check_overtakes_hard_ones_on_the_default_slow_workers() -> None:
    # S12, S24, S25: with the default `slow_workers`, two hard checks on a 1-slot pool
    # each leave the slot, so an easy check ends while both are still in Z3.
    executor = SolverExecutor(1)
    order: list[str] = []

    async def run(name: str, fn: Callable[[], object]) -> None:
        await executor.run(fn)
        order.append(name)

    try:
        hard = [asyncio.create_task(run("hard", _hard(3.0))) for _ in range(2)]
        while executor._slow < 2:
            await asyncio.sleep(0.01)
        await run("easy", lambda: None)
        await asyncio.gather(*hard)
    finally:
        executor.shutdown()
    assert order == ["easy", "hard", "hard"]


async def test_a_call_without_a_z3_context_never_leaves_its_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # S13, S26: `test_only_a_running_z3_check_gives_up_its_slot` sleeps 4 slices and
    # checks that the queued call has not run, so under load it passes either way.
    # Here every `_to_slow` decision is recorded and the first three must be "stay".
    executor = SolverExecutor(1)
    decisions: list[bool] = []
    three = threading.Event()
    real = executor._to_slow

    def to_slow(interrupt: _Interrupt) -> bool:
        out = real(interrupt)
        decisions.append(out)
        if len(decisions) >= 3:
            three.set()
        return out

    monkeypatch.setattr(executor, "_to_slow", to_slow)
    gate = threading.Event()
    try:
        held = asyncio.create_task(executor.run(partial(gate.wait, _WAIT_S)))
        assert await asyncio.to_thread(three.wait, _WAIT_S)
        gate.set()
        await held
    finally:
        gate.set()
        executor.shutdown()
    assert not any(decisions)
    assert executor._slow == 0


async def test_slow_checks_restore_the_pools_thread_limit() -> None:
    # S17 (and S18, S22): once the slow checks end, the pool's thread limit and the
    # slow count are back where they started.
    executor = SolverExecutor(1, slow_workers=2)
    try:
        await asyncio.gather(*(executor.run(_hard(0.3)) for _ in range(3)))
        limit, slow = executor._pool._max_workers, executor._slow
    finally:
        executor.shutdown()
    assert (limit, slow) == (1, 0)


async def test_a_caller_cancelled_while_its_check_is_queued_never_runs_it() -> None:
    # S21: the pool's only thread is busy, so the caller's check waits in the pool
    # queue. Cancelling the caller must drop it; it is not attached, so it never
    # leaves the `asyncio.wait` loop, where only `future.cancel()` drops it.
    executor = SolverExecutor(1)
    gate = threading.Event()
    blocker = executor._pool.submit(gate.wait, _WAIT_S)
    calls: list[int] = []
    try:
        task = asyncio.create_task(executor.run(lambda: calls.append(1)))
        while executor._pool._work_queue.qsize() == 0:
            await asyncio.sleep(0.01)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        gate.set()
        await asyncio.wrap_future(blocker)
        await executor.run(lambda: None)  # the queue has drained
    finally:
        gate.set()
        executor.shutdown()
    assert calls == []


def test_the_entailment_check_gets_the_time_left_not_the_initial_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # S09: the timeout is set again before the entailment check, to what is left.
    timeouts: list[int] = []
    real_set = z3.Solver.set
    real_check = z3.Solver.check

    def record(self: z3.Solver, *args: object, **kwargs: object) -> None:
        if args and args[0] == "timeout":
            timeouts.append(int(cast(int, args[1])))
        real_set(self, *args, **kwargs)

    def slow_check(self: z3.Solver, *args: z3.ExprRef) -> z3.CheckSatResult:
        out = real_check(self, *args)
        time.sleep(0.3)  # the consistency check takes 0.3 s of a 2 s budget
        return out

    monkeypatch.setattr(z3.Solver, "set", record)
    monkeypatch.setattr(z3.Solver, "check", slow_check)
    out = Z3Solver(2.0).check_entailment([SmtFormula(_DECLS, "p")], SmtFormula(_DECLS, "p"))
    assert out.verdict == "VALID"
    assert len(timeouts) == 2
    assert timeouts[1] <= timeouts[0] - 250


def test_the_entailment_check_asserts_the_negated_claim_instead_of_assuming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # S01 (SG19's assumption check restored): only the `perf` AUDIT-14 #1 repros see
    # it, by timing. Z3 runs an assumption check on LIA in a phase that polls neither
    # the timeout nor interrupts, so no `check` call may pass assumptions.
    calls: list[int] = []
    real_check = z3.Solver.check

    def check(self: z3.Solver, *assumptions: z3.ExprRef) -> z3.CheckSatResult:
        calls.append(len(assumptions))
        return real_check(self, *assumptions)

    monkeypatch.setattr(z3.Solver, "check", check)
    decls = "(declare-const x Int)"
    out = Z3Solver(5.0).check_entailment(
        [SmtFormula(decls, "(> x 1)")], SmtFormula(decls, "(> x 0)")
    )
    assert out.verdict == "VALID"
    assert calls == [0, 0]


# Finding #3 (high): SG22 placed the AUDIT-14 #1 timeout repro's deadline a quarter
# of the way into the entailment check, but the add-based check takes ~0.25 s on its
# 1600-premise pool, so 0.06 s is left after a ~0.45 s consistency check and the
# `_SETUP_SHARE` guard returns before the entailment check starts. The repro tests
# the guard, by timing (it caught S03 in 1 of 3 runs), and never times the check.


def test_the_audit_14_1_timeout_repro_reaches_the_entailment_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    premises, claim, timeout_s = audit_14._timeout_case()
    calls: list[int] = []
    real_check = z3.Solver.check

    def check(self: z3.Solver, *args: z3.ExprRef) -> z3.CheckSatResult:
        calls.append(1)
        return real_check(self, *args)

    monkeypatch.setattr(z3.Solver, "check", check)
    out = Z3Solver(timeout_s).check_entailment(premises, claim)
    assert out.reason == "timeout"
    # Before SG26: 1 call. The guard returned (or, on a slow run, the consistency check
    # itself timed out); either way no entailment check was timed.
    assert len(calls) == 2


class _Clock:
    """`solver.time` with a `monotonic` the test advances: the guard's arithmetic, no sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.perf_counter = time.perf_counter

    def monotonic(self) -> float:
        return self.now


def _guarded_run(
    monkeypatch: pytest.MonkeyPatch, *, budget_s: float, parse_s: float, consistency_s: float
) -> tuple[SolverOutcome, int]:
    """One `check_entailment` of `p` from `p` whose parse and first check take the given
    (fake) times; returns the outcome and the number of `Solver.check` calls."""
    clock = _Clock()
    calls: list[int] = []
    real_check = z3.Solver.check
    real_from_string = z3.Solver.from_string

    def check(self: z3.Solver, *args: z3.ExprRef) -> z3.CheckSatResult:
        calls.append(1)
        out = real_check(self, *args)
        if len(calls) == 1:
            clock.now += consistency_s
        return out

    def parse(self: z3.Solver, s: str) -> None:
        real_from_string(self, s)
        clock.now += parse_s

    monkeypatch.setattr(solver_mod, "time", clock)
    monkeypatch.setattr(z3.Solver, "check", check)
    monkeypatch.setattr(z3.Solver, "from_string", parse)
    out = Z3Solver(budget_s).check_entailment([SmtFormula(_DECLS, "p")], SmtFormula(_DECLS, "p"))
    return out, len(calls)


def test_the_setup_guard_stops_a_check_with_less_than_half_its_consistency_time_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # S03, S04, S06: 0.3 s of 1 s left after a 0.7 s consistency check is less than
    # half of 0.7 s. SG22's own test sleeps 0.15 s of a 0.2 s budget, so its margin
    # against a 0.25 share (S04) is 12.5 ms of wall clock.
    out, checks = _guarded_run(monkeypatch, budget_s=1.0, parse_s=0.0, consistency_s=0.7)
    assert (out.reason, out.details) == ("timeout", "too little time left for the entailment check")
    assert checks == 1


def test_an_entailment_check_with_enough_time_left_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    # S05: 0.7 s left after a 0.3 s consistency check is more than half of 0.3 s.
    out, checks = _guarded_run(monkeypatch, budget_s=1.0, parse_s=0.0, consistency_s=0.3)
    assert (out.verdict, out.reason) == ("VALID", "entailed")
    assert checks == 2


def test_a_slow_parse_does_not_count_as_consistency_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # S07: `_SETUP_SHARE` is a share of the consistency check, not of the parse. A
    # 0.7 s parse and an instant check leave 0.3 s of 1 s: the entailment check runs.
    out, checks = _guarded_run(monkeypatch, budget_s=1.0, parse_s=0.7, consistency_s=0.0)
    assert (out.verdict, out.reason) == ("VALID", "entailed")
    assert checks == 2


# --------------------------------------------------------------------------
# Kill tests: state/store.py (SG23).
# --------------------------------------------------------------------------


async def test_a_file_store_uses_wal_and_the_busy_timeout(tmp_path: Path) -> None:
    # T03: nothing else checks the journal mode; the busy timeout half is also held
    # by the AUDIT-6 #2 test (T01, T02, T05), but only through a 5.5 s lock.
    path = tmp_path / "s.db"
    store = await StateStore.create(f"sqlite+aiosqlite:///{path}")
    try:
        async with store.session() as session:
            busy_ms = (await session.execute(text("PRAGMA busy_timeout"))).scalar_one()
    finally:
        await store.dispose()
    with sqlite3.connect(path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    assert busy_ms == SQLITE_BUSY_TIMEOUT_S * 1000


# --------------------------------------------------------------------------
# Kill tests: formalizer.py (SG20).
# --------------------------------------------------------------------------

_GLUED: Final = "(! p:named false)"
"""Z3 reads `p:named` as `p :named`; since SG24 `_tokens` does too (it read one token)."""


def test_a_glued_named_annotation_really_redefines_false_in_z3() -> None:
    # Precondition of the next test: the glued form is a live AUDIT-13 #2 attack.
    # With `false` named for `p`, the second assertion `(= false true)` says `p`.
    smt = f"(declare-const p Bool)(assert {_GLUED})(assert (= false true))"
    second = z3.parse_smt2_string(smt, ctx=z3.Context())[1]
    s = z3.Solver(ctx=second.ctx)
    s.add(second)
    assert s.check() == z3.sat
    assert ":named" in formalizer._tokens(_GLUED)  # SG24: tokenized as Z3 lexes it


async def test_a_glued_named_annotation_is_refused() -> None:
    # F04: with only `:keyword` tokens refused, `(! p:named false)` passes, because
    # no token starts with `:`. Only the `!` rule refuses it.
    fake = FakeLLMClient()
    f = Formalizer(fake)
    step = ReasoningStep(index=0, claim="The plate was read", claim_type="premise")
    fake.register(
        "formalize",
        prompt_for(step, f.vocabulary),
        json.dumps({"declarations": "(declare-const p Bool)", "assertion": _GLUED}),
    )
    with pytest.raises(FormalizationError, match="annotation"):
        await f.formalize(step)


async def _formalize_all(steps: list[tuple[str, str]]) -> list[str | None]:
    """Formalize (declarations, assertion) pairs in order; None for success, else the error."""
    fake = FakeLLMClient()
    f = Formalizer(fake)
    out: list[str | None] = []
    for i, (decls, assertion) in enumerate(steps):
        step = ReasoningStep(index=i, claim=f"claim {i}", claim_type="premise")
        fake.register(
            "formalize",
            prompt_for(step, f.vocabulary),
            json.dumps({"declarations": decls, "assertion": assertion}),
        )
        try:
            await f.formalize(step)
        except FormalizationError as exc:
            out.append(str(exc))
        else:
            out.append(None)
    return out


async def test_the_earlier_use_rule_remembers_every_earlier_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # F09: the SG20 test declares each name in the step right after its use, so
    # remembering only the last step's tokens passes it. Here a step comes between.
    monkeypatch.setattr(formalizer, "_is_z3_builtin", lambda _name: False)
    monkeypatch.setattr(formalizer, "_applied_builtin", lambda *_: False)
    errors = await _formalize_all(
        [
            ("(declare-const a Real)", "(equals a 0.95)"),
            ("(declare-const q Bool)", "q"),
            ("(declare-fun equals (Real Real) Bool)", "(equals a 0.5)"),
        ]
    )
    assert errors[:2] == [None, None]
    assert errors[2] is not None and "meaning of an earlier step" in errors[2]


async def test_a_name_used_only_in_a_declaration_cannot_be_declared_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # F06: SG20's rule also covers the tokens of the vocabulary's declarations (here
    # the builtin sort `Float32`, which a probe that missed it would let through).
    monkeypatch.setattr(formalizer, "_is_z3_builtin", lambda _name: False)
    monkeypatch.setattr(formalizer, "_applied_builtin", lambda *_: False)
    errors = await _formalize_all(
        [
            ("(declare-const x Float32)", "(= x x)"),
            ("(declare-sort Float32 0)", "true"),
        ]
    )
    assert errors[0] is None
    assert errors[1] is not None and "meaning of an earlier step" in errors[1]


@pytest.mark.parametrize("decl", ["(declare-sort Set 1)", "(declare-sort List 1)"])
def test_the_applied_probe_refuses_a_parametric_builtin_sort_alone(
    monkeypatch: pytest.MonkeyPatch, decl: str
) -> None:
    # F12, F13: the bare sort probe also refuses these (an arity error), so the
    # applied half for `declare-sort` was untested. Here it has to work alone.
    monkeypatch.setattr(formalizer, "_is_z3_builtin", lambda _name: False)
    formalizer._applied_builtin.cache_clear()
    with pytest.raises(FormalizationError, match="Z3 builtin"):
        formalizer.Vocabulary().merged(decl)


def test_the_formalize_prompt_states_the_earlier_use_rule() -> None:
    # F23: `test_formalize_prompt_states_what_is_rejected` does not look for it.
    step = ReasoningStep(index=0, claim="the sample is hot", claim_type="premise")
    assert "earlier step already used" in prompt_for(step, formalizer.Vocabulary())


# --------------------------------------------------------------------------
# Kill tests: agents/common.py (SG21).
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "protocol", ["Incubate for Four hours", "Incubate for ½ hour", "Wait ³ days", "Hold for ¼ h"]
)
def test_protocol_figures_flag_capitalised_number_words_and_vulgar_numerals(
    protocol: str,
) -> None:
    # A17 (case of number words), A19 (superscripts and fractions as numerals): the
    # SG21 table has only lower-case number words and ASCII digits.
    assert protocol_figures(protocol)


def test_a_measurement_named_like_a_hashed_symbol_keeps_its_own_symbol() -> None:
    # A27: a name starting `m_` is hashed too, or a measurement literally named
    # after another one's hashed symbol would share its fact symbol.
    hashed = _symbol("OD 600")
    assert hashed.startswith("m_")
    assert _symbol(hashed) != hashed
