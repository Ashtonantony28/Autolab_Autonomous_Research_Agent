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
    built = [pathological() for _ in range(4)]
    started = time.perf_counter()
    results = await asyncio.gather(
        *(
            Verifier(f, solver_timeout_seconds=_SLOW_S, solver_executor=executor).verify(t)
            for t, f in built
        )
    )
    wall_ms = (time.perf_counter() - started) * 1000
    slow = [r.step_verdicts[-1].solver_ms for r in results]
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


class _Z3Spy:
    """Signals when an entailment check is running and when a context is interrupted."""

    def __init__(self) -> None:
        self.checks = 0
        self.entailing = threading.Event()
        self.interrupted = threading.Event()


@pytest.fixture
def z3_spy(monkeypatch: pytest.MonkeyPatch) -> _Z3Spy:
    spy = _Z3Spy()
    lock = threading.Lock()
    real_check, real_interrupt = z3.Solver.check, z3.Context.interrupt

    def check(self: z3.Solver, *args: object) -> object:
        with lock:
            spy.checks += 1
            if spy.checks == 2:  # premise consistency first, then the entailment check
                spy.entailing.set()
        return real_check(self, *args)

    def interrupt(self: z3.Context) -> None:
        spy.interrupted.set()
        real_interrupt(self)

    monkeypatch.setattr(z3.Solver, "check", check)
    monkeypatch.setattr(z3.Context, "interrupt", interrupt)
    return spy


def _undecidable_check() -> solver_mod.SolverOutcome:
    solver = Z3Solver(timeout_seconds=_HANG_S)
    return solver.check_entailment([SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES))


async def test_cancelling_a_caller_interrupts_its_z3_context(
    make_executor: ExecutorFactory, z3_spy: _Z3Spy
) -> None:
    executor = make_executor(1)
    task = asyncio.create_task(executor.run(_undecidable_check))
    assert await asyncio.to_thread(z3_spy.entailing.wait, _WAIT_S)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(z3_spy.interrupted.wait, _WAIT_S)
    assert (await executor.run(lambda: "free"))[0] == "free"


async def test_shutdown_interrupts_the_running_z3_context(
    make_executor: ExecutorFactory, z3_spy: _Z3Spy
) -> None:
    executor = make_executor(1)
    task = asyncio.create_task(executor.run(_undecidable_check))
    assert await asyncio.to_thread(z3_spy.entailing.wait, _WAIT_S)
    await asyncio.to_thread(executor.shutdown)
    assert z3_spy.interrupted.is_set()
    outcome, _ = await task
    assert (outcome.verdict, outcome.reason) == ("UNGROUNDED", "timeout")


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
