"""SG10: the bounded Z3 executor (AUDIT-2 #1, #5, #6)."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from typing import Final

import pytest

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
from tests.fixtures.golden_traces import build

_SLOW_S: Final = 0.2
_SLACK_S: Final = 0.5

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


async def test_default_pool_stays_responsive_under_fifty_slow_checks(
    make_executor: ExecutorFactory,
) -> None:
    executor = make_executor(8)
    built = [pathological() for _ in range(50)]
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
    assert waited < 0.25
    assert {r.step_verdicts[-1].reason for r in results} == {"timeout"}
    assert max(r.step_verdicts[-1].solver_ms for r in results) < (_SLOW_S + _SLACK_S) * 1000


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
    finished = threading.Event()
    outcome: list[str] = []

    def late_check() -> None:
        time.sleep(0.1)
        out = solver.check_entailment([SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES))
        outcome.append(out.details)
        finished.set()

    task = asyncio.create_task(executor.run(late_check))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(finished.wait, 1.0)
    assert outcome == ["interrupted"]


async def test_cancel_while_queued_submits_nothing(make_executor: ExecutorFactory) -> None:
    executor = make_executor(1)
    running = asyncio.create_task(executor.run(lambda: time.sleep(_SLOW_S)))
    calls: list[int] = []
    queued = asyncio.create_task(executor.run(lambda: calls.append(1)))
    await asyncio.sleep(0.05)
    queued.cancel()
    await running
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert calls == []


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
