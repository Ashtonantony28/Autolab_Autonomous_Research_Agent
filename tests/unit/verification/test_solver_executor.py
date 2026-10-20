"""SG10: the bounded Z3 executor (AUDIT-2 #1, #5, #6)."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from functools import partial
from types import SimpleNamespace
from typing import Any, Final

import pytest
import z3

from autolab.verification import solver as solver_mod
from autolab.verification import solver_workers
from autolab.verification.llm import FakeLLMClient, LLMPurpose, SchemaT
from autolab.verification.solver import (
    SmtFormula,
    SolverExecutor,
    Z3Solver,
    default_executor,
    shutdown_default_executor,
)
from autolab.verification.vericot import Verifier
from scripts.audit_load import NO_CUBES, POSITIVE, XYZ, LatencyLLM, chain, pathological
from scripts.audit_load_2 import CountingExecutor
from scripts.audit_load_4 import fast_trace_under_slow_runs
from tests.fixtures.golden_traces import build

_SLOW_S: Final = 0.2
_SLACK_S: Final = 0.5
_WAIT_S: Final = 10.0
"""Bounds a failing wait on an event; a passing run never waits this long."""
_HANG_S: Final = 20.0
"""Z3 timeout of the undecidable check: only an interrupt ends it sooner."""

ExecutorFactory = Callable[[int], SolverExecutor]


@pytest.fixture
def make_executor() -> Iterator[ExecutorFactory]:
    made: list[SolverExecutor] = []

    def make(workers: int) -> SolverExecutor:
        made.append(SolverExecutor(workers))
        return made[-1]

    yield make
    for executor in made:
        executor.shutdown()


def _slow_verify(executor: SolverExecutor, timeout_s: float = _SLOW_S) -> Verifier:
    _, fake = pathological()
    return Verifier(fake, solver_timeout_seconds=timeout_s, solver_executor=executor)


@pytest.mark.perf
async def test_default_pool_stays_responsive_under_fifty_slow_checks(
    make_executor: ExecutorFactory,
) -> None:
    executor = make_executor(8)
    built = [pathological() for _ in range(50)]
    load_started = time.perf_counter()
    load = asyncio.gather(
        *(
            Verifier(f, solver_timeout_seconds=_SLOW_S, solver_executor=executor).verify(t)
            for t, f in built
        )
    )
    await asyncio.sleep(0.1)
    loop = asyncio.get_running_loop()
    started = time.perf_counter()
    await asyncio.to_thread(lambda: None)
    await loop.getaddrinfo("localhost", 443)
    waited = time.perf_counter() - started
    assert not load.done()
    results = await load
    wall_ms = (time.perf_counter() - load_started) * 1000
    # On a starved default pool the probe waits for most of the 7 rounds (~1.2 s).
    assert waited < 0.5
    # Under CPU contention a 0.2 s premise check can time out too (`no_premises`).
    assert {r.step_verdicts[-1].reason for r in results} <= {"timeout", "no_premises"}
    worst_ms = max(v.solver_ms for r in results for v in r.step_verdicts)
    assert worst_ms < (_SLOW_S + _SLACK_S) * 1000 + 1000
    # 7 rounds share 8 workers: queueing counted in `solver_ms` would approach the wall time.
    assert worst_ms < 0.5 * wall_ms


@pytest.mark.perf
async def test_solver_ms_excludes_queueing(make_executor: ExecutorFactory) -> None:
    executor = make_executor(1)
    executor.slow_workers = 0  # every check holds the only slot, so the others queue
    # Fresh callers with no short check behind them, so no stuck slot lends them a place.
    hard = partial(
        Z3Solver(_SLOW_S).check_entailment, [SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES)
    )
    started = time.perf_counter()
    results = await asyncio.gather(*(executor.run(hard) for _ in range(4)))
    wall_ms = (time.perf_counter() - started) * 1000
    slow = [ms for _, ms in results]
    assert wall_ms >= 4 * _SLOW_S * 1000  # one worker: the checks queued
    assert all(_SLOW_S * 1000 * 0.9 <= ms < (_SLOW_S + _SLACK_S) * 1000 for ms in slow)


@pytest.mark.perf
async def test_backpressure_bounds_work_in_the_pool(make_executor: ExecutorFactory) -> None:
    executor = make_executor(2)
    lock = threading.Lock()
    active, peak = 0, 0

    def work() -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return 1

    outs = await asyncio.gather(*(executor.run(work) for _ in range(40)))
    assert [r for r, _ in outs] == [1] * 40
    assert all(15 <= ms < 200 for _, ms in outs)
    assert peak == 2
    assert len(executor._pool._threads) == 2
    assert executor._pool._work_queue.empty()


@pytest.mark.perf
async def test_cancelled_verify_interrupts_its_check(make_executor: ExecutorFactory) -> None:
    executor = make_executor(1)
    trace, _ = pathological()
    task = asyncio.create_task(_slow_verify(executor, timeout_s=3.0).verify(trace))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The only worker is free again well before the 3 s Z3 timeout.
    started = time.perf_counter()
    trace, fake = build(chain(4, cite=True))
    result = await Verifier(fake, solver_executor=executor).verify(trace)
    assert result.consistency_score == 1.0
    assert time.perf_counter() - started < 0.5


async def test_interrupt_before_the_check_starts(make_executor: ExecutorFactory) -> None:
    executor = make_executor(1)
    solver = Z3Solver(timeout_seconds=3.0)
    started, cancelled, finished = threading.Event(), threading.Event(), threading.Event()
    outcome: list[str] = []

    def late_check() -> None:
        started.set()
        assert cancelled.wait(_WAIT_S)
        out = solver.check_entailment([SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES))
        outcome.append(out.details)
        finished.set()

    task = asyncio.create_task(executor.run(late_check))
    assert await asyncio.to_thread(started.wait, _WAIT_S)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cancelled.set()
    assert await asyncio.to_thread(finished.wait, _WAIT_S)
    assert outcome == ["interrupted"]


async def test_cancel_while_queued_submits_nothing(make_executor: ExecutorFactory) -> None:
    executor = make_executor(1)
    gate = threading.Event()
    running = asyncio.create_task(executor.run(partial(gate.wait, _WAIT_S)))
    calls: list[int] = []
    queued = asyncio.create_task(executor.run(lambda: calls.append(1)))
    await asyncio.sleep(0)  # both tasks start; the second waits for the only worker
    queued.cancel()
    gate.set()
    await running
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert calls == []


@pytest.mark.perf
async def test_hard_runs_do_not_starve_an_easy_verify_on_one_worker() -> None:
    # A 1-CPU host: one slot. Each hard check leaves it after `_SLICE_S` (AUDIT-14 #2).
    timeout_s = 0.5
    _, loaded = await fast_trace_under_slow_runs(workers=1, slow_runs=2, timeout_s=timeout_s)
    assert loaded < timeout_s


async def test_slow_checks_are_bounded_by_slow_workers() -> None:
    executor = SolverExecutor(1, slow_workers=1)
    lock = threading.Lock()
    running, peak = 0, 0

    def hard() -> solver_mod.SolverOutcome:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        try:
            return Z3Solver(0.3).check_entailment(
                [SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES)
            )
        finally:
            with lock:
                running -= 1

    try:
        outs = await asyncio.gather(*(executor.run(hard) for _ in range(3)))
        threads = len(executor._pool._threads)
    finally:
        executor.shutdown()
    assert [o.reason for o, _ in outs] == ["timeout"] * 3
    # One check in the slot and one slow check; the third waits for a slow one to end.
    assert peak == 2
    assert threads <= 2
    assert executor._slow == 0


async def test_only_a_running_z3_check_gives_up_its_slot(make_executor: ExecutorFactory) -> None:
    executor = make_executor(1)
    gate = threading.Event()
    held = asyncio.create_task(executor.run(partial(gate.wait, _WAIT_S)))
    calls: list[int] = []
    queued = asyncio.create_task(executor.run(lambda: calls.append(1)))
    await asyncio.sleep(4 * solver_mod._SLICE_S)
    assert calls == []
    gate.set()
    await asyncio.gather(held, queued)
    assert calls == [1]


@pytest.mark.perf
async def test_shutdown_interrupts_running_checks() -> None:
    executor = SolverExecutor(1)
    trace, fake = pathological()
    task = asyncio.create_task(
        Verifier(fake, solver_timeout_seconds=3.0, solver_executor=executor).verify(trace)
    )
    await asyncio.sleep(0.2)
    started = time.perf_counter()
    await asyncio.to_thread(executor.shutdown)
    result = await task
    assert time.perf_counter() - started < 0.5
    assert result.step_verdicts[-1].reason == "timeout"
    with pytest.raises(RuntimeError):
        await executor.run(lambda: None)


async def test_default_executor_is_lazy_and_replaceable() -> None:
    first = default_executor()
    assert default_executor() is first
    assert first.workers == solver_mod.SOLVER_WORKERS
    shutdown_default_executor()
    assert first._pool._shutdown
    second = default_executor()
    assert second is not first
    trace, fake = build(chain(4, cite=True))
    assert (await Verifier(fake).verify(trace)).consistency_score == 1.0


@pytest.mark.perf
async def test_judges_overlap_but_formalization_stays_ordered() -> None:
    delay_s = 0.1
    trace, fake = build(chain(6, cite=True))
    order: list[str] = []

    class Recording(LatencyLLM):
        async def call_structured(
            self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
        ) -> SchemaT:
            if purpose == "formalize":
                order.append(prompt.split("step_index: ")[1].split("\n")[0])
            return await super().call_structured(purpose, prompt, schema)

    llm = Recording(fake, delay_s)
    started = time.perf_counter()
    result = await Verifier(llm, check_faithfulness=True).verify(trace)
    wall = time.perf_counter() - started
    assert result.consistency_score == 1.0
    assert order == [str(i) for i in range(6)]
    assert wall < (6 + 2) * delay_s


@pytest.mark.perf
async def test_formalize_error_cancels_pending_judges() -> None:
    trace, fake = build(chain(4, cite=True))
    judges_cancelled: list[bool] = []

    class FailsOnThirdStep(FakeLLMClient):
        async def call_structured(
            self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
        ) -> SchemaT:
            if purpose == "formalize":
                await asyncio.sleep(0.01)  # lets the earlier judges start
            if purpose == "faithfulness":
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    judges_cancelled.append(True)
                    raise
            if "step_index: 2" in prompt:
                raise ConnectionError("reset")
            return await fake.call_structured(purpose, prompt, schema)

    started = time.perf_counter()
    with pytest.raises(ConnectionError):
        await Verifier(FailsOnThirdStep(), check_faithfulness=True).verify(trace)
    assert time.perf_counter() - started < 1.0
    assert judges_cancelled == [True, True]


# --- Deterministic counterparts of the timing tests above (AUDIT-8 H1) ----------
# Checks run in worker processes (ADR 0009). A cancel kills the check's worker, so
# where in the check it lands does not matter: these tests wait until the check is
# attached to its worker, then assert how it ended and that the worker is gone.


class _Undecidable:
    """An entailment check only a cancel ends before `_HANG_S`; records how it ended."""

    def __init__(self) -> None:
        self.outcome: solver_mod.SolverOutcome | None = None
        self.elapsed_s = 0.0
        self.done = threading.Event()

    def __call__(self) -> solver_mod.SolverOutcome:
        solver = Z3Solver(timeout_seconds=_HANG_S)
        started = time.perf_counter()
        try:
            self.outcome = solver.check_entailment(
                [SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES)
            )
            return self.outcome
        finally:
            self.elapsed_s = time.perf_counter() - started
            self.done.set()


async def attached_worker(executor: SolverExecutor) -> solver_workers.Worker:
    """The worker of the executor's running check, once its request has been sent."""
    deadline = time.monotonic() + _WAIT_S
    while time.monotonic() < deadline:
        for interrupt in list(executor._running):
            target = interrupt._target
            if isinstance(target, solver_workers.Worker) and target._ready:
                return target
        await asyncio.sleep(0.005)
    raise AssertionError("no check attached to a worker")


async def test_cancelling_a_caller_kills_its_worker(make_executor: ExecutorFactory) -> None:
    executor = make_executor(1)
    check = _Undecidable()
    task = asyncio.create_task(executor.run(check))
    worker = await attached_worker(executor)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(check.done.wait, _WAIT_S)
    assert check.outcome == solver_mod._INTERRUPTED
    assert check.elapsed_s < _HANG_S / 2
    assert not worker.alive()
    assert (await executor.run(lambda: "free"))[0] == "free"


async def test_shutdown_kills_the_running_worker(make_executor: ExecutorFactory) -> None:
    executor = make_executor(1)
    check = _Undecidable()
    task = asyncio.create_task(executor.run(check))
    worker = await attached_worker(executor)
    await asyncio.to_thread(executor.shutdown)
    outcome, _ = await task
    assert (outcome.verdict, outcome.reason, outcome.details) == (
        "UNGROUNDED",
        "timeout",
        "interrupted",
    )
    assert check.elapsed_s < _HANG_S / 2
    assert not worker.alive()


async def test_cancel_during_a_check_returns_it_promptly(make_executor: ExecutorFactory) -> None:
    executor = make_executor(1)
    check = _Undecidable()
    task = asyncio.create_task(executor.run(check))
    await asyncio.sleep(0.2)  # well inside the entailment check
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(check.done.wait, _WAIT_S)
    assert check.outcome is not None
    assert check.outcome.details == "interrupted"


async def test_interrupt_never_reaches_a_later_check(make_executor: ExecutorFactory) -> None:
    executor = make_executor(1)
    check = _Undecidable()
    task = asyncio.create_task(executor.run(check))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(check.done.wait, _WAIT_S)
    trace, fake = build(chain(4, cite=True))
    result = await Verifier(fake, solver_executor=executor).verify(trace)
    assert result.consistency_score == 1.0


def test_a_check_past_its_timeout_and_grace_is_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    # No grace and no time: the parent stops waiting at once and kills the worker.
    monkeypatch.setattr(solver_mod, "_grace_s", lambda timeout_s: -timeout_s)
    pool = solver_mod._workers()
    seen: list[solver_workers.Worker] = []
    real_call = solver_workers.Worker.call

    def call(worker: solver_workers.Worker, args: tuple[Any, ...], budget_s: float) -> Any:
        seen.append(worker)
        assert budget_s == 0
        return real_call(worker, args, budget_s)

    monkeypatch.setattr(solver_workers.Worker, "call", call)
    out = Z3Solver(_HANG_S).check_entailment([SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES))
    assert out == solver_mod._KILLED
    assert out.verdict == "UNGROUNDED"
    assert not seen[0].alive()
    assert seen[0] not in pool._idle


def test_a_check_whose_worker_dies_is_never_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    # The worker dies after the request is sent; the claim is entailed, so only a reply
    # that never came could have made it VALID.
    real_call = solver_workers.Worker.call

    def dying(worker: solver_workers.Worker, args: tuple[Any, ...], budget_s: float) -> Any:
        worker.process.kill()
        worker.process.join()
        return real_call(worker, args, budget_s)

    monkeypatch.setattr(solver_workers.Worker, "call", dying)
    decls = "(declare-const p Bool)"
    out = Z3Solver().check_entailment([SmtFormula(decls, "p")], SmtFormula(decls, "p"))
    assert (out.verdict, out.reason) == ("UNGROUNDED", "timeout")


def test_a_check_that_raises_in_its_worker_raises_in_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(worker: solver_workers.Worker, args: tuple[Any, ...], budget_s: float) -> Any:
        return (False, "Z3Exception: model is not available")

    monkeypatch.setattr(solver_workers.Worker, "call", failing)
    decls = "(declare-const p Bool)"
    with pytest.raises(solver_mod.SolverWorkerError, match="model is not available"):
        Z3Solver().check_entailment([SmtFormula(decls, "p")], SmtFormula(decls, "q"))


def test_each_check_starts_from_default_global_parameters() -> None:
    decls = "(declare-const p Bool)\n(declare-const q Bool)"
    premises, claim = (
        [SmtFormula(decls, "p"), SmtFormula(decls, "(=> p q)")],
        SmtFormula(decls, "q"),
    )
    z3.set_param("rlimit", 1)  # what a `(set-option :rlimit 1)` parsed anywhere would leave
    try:
        here = Z3Solver()._check_here(premises, claim)  # the worker's own process
    finally:
        z3.reset_params()
    assert here.verdict == "VALID"
    assert Z3Solver().check_entailment(premises, claim).verdict == "VALID"


def test_no_entailment_check_starts_without_the_time_the_consistency_check_took(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    real_check = z3.Solver.check

    def slow_check(self: z3.Solver, *args: object) -> object:
        calls.append(1)
        out = real_check(self, *args)
        time.sleep(0.15)  # the consistency check takes 0.15 s of a 0.2 s budget
        return out

    monkeypatch.setattr(z3.Solver, "check", slow_check)
    decls = "(declare-const p Bool)"
    out = Z3Solver(0.2)._check_here([SmtFormula(decls, "p")], SmtFormula(decls, "p"))
    assert (out.verdict, out.reason) == ("UNGROUNDED", "timeout")
    assert out.details == "too little time left for the entailment check"
    assert calls == [1]


def test_counter_model_does_not_use_the_shared_printer(monkeypatch: pytest.MonkeyPatch) -> None:
    def printer(*_: object) -> None:
        raise AssertionError("z3printer used")

    monkeypatch.setattr(z3.z3printer.PP, "__call__", printer)
    decls = "(declare-const p Bool)\n(declare-const q Bool)"
    out = Z3Solver()._check_here([SmtFormula(decls, "p")], SmtFormula(decls, "q"))
    assert out.verdict == "INVALID"
    assert "define-fun q" in out.counter_model


async def test_run_time_excludes_waiting_for_a_worker(
    make_executor: ExecutorFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = SimpleNamespace(now=0.0)
    fake_time = SimpleNamespace(perf_counter=lambda: clock.now, monotonic=lambda: clock.now)
    monkeypatch.setattr(solver_mod, "time", fake_time)
    executor = make_executor(1)
    started, gate = threading.Event(), threading.Event()

    def held() -> bool:
        started.set()
        return gate.wait(_WAIT_S)

    first = asyncio.create_task(executor.run(held))
    queued = asyncio.create_task(executor.run(lambda: True))
    assert await asyncio.to_thread(started.wait, _WAIT_S)
    clock.now += 1000.0  # the queued call waits this long for the only worker
    gate.set()
    (ran, held_ms), (_, queued_ms) = await asyncio.gather(first, queued)
    assert ran
    assert held_ms == 1000.0 * 1000
    assert queued_ms == 0.0


async def test_callers_beyond_the_worker_count_wait_outside_the_pool(
    make_executor: ExecutorFactory,
) -> None:
    executor = make_executor(2)
    submitted = 0
    real_submit = executor._pool.submit

    def submit(fn: Callable[..., Any], /, *args: Any) -> Future[Any]:
        nonlocal submitted
        submitted += 1
        return real_submit(fn, *args)

    setattr(executor._pool, "submit", submit)  # noqa: B010 - instance-level spy
    gate = threading.Event()
    tasks = [asyncio.create_task(executor.run(partial(gate.wait, _WAIT_S))) for _ in range(6)]
    await asyncio.sleep(0)  # every task runs up to its first suspension
    assert submitted == 2
    gate.set()
    assert [ok for ok, _ in await asyncio.gather(*tasks)] == [True] * 6
    assert submitted == 6


async def test_verifier_runs_every_check_on_the_injected_executor() -> None:
    shutdown_default_executor()
    executor = CountingExecutor(1)
    try:
        trace, fake = build(chain(4, cite=True))
        result = await Verifier(fake, solver_executor=executor).verify(trace)
    finally:
        executor.shutdown()
    assert result.consistency_score == 1.0
    assert executor.checks == len(trace.steps)
    assert solver_mod._default_executor is None


async def test_judge_runs_while_the_next_step_is_formalized() -> None:
    trace, fake = build(chain(4, cite=True))
    next_formalized = asyncio.Event()
    formalized = 0
    judged = 0

    class Overlapping(LatencyLLM):
        async def call_structured(
            self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
        ) -> SchemaT:
            nonlocal formalized, judged
            if purpose == "formalize":
                formalized += 1
                if formalized == 2:
                    next_formalized.set()
            elif judged == 0:
                judged = 1
                # A judge awaited before the next step is formalized never returns.
                await asyncio.wait_for(next_formalized.wait(), _WAIT_S)
            return await super().call_structured(purpose, prompt, schema)

    result = await Verifier(Overlapping(fake, 0.0), check_faithfulness=True).verify(trace)
    assert result.consistency_score == 1.0
