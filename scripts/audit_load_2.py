"""AUDIT-6 load measurements, round 2 (FakeLLMClient / stubs only; no network).

Each section prints the numbers quoted in `AUDIT-6.md`. The repro tests in
`tests/audit/test_audit_6.py` reuse the helpers defined here. Round-1
measurements are re-run with `python -m scripts.audit_load`.

    uv run python -m scripts.audit_load_2            # every section
    uv run python -m scripts.audit_load_2 loops judges

Sections: segv, loops, shutdown, races, threads, judges, payload, store, retry,
memory, budget.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import math
import os
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import anthropic
import httpx

from autolab.orchestrator.recovery import RetryingLLMClient
from autolab.state.models import ReasoningTrace, VerificationContext, VerificationResult
from autolab.verification import solver as solver_mod
from autolab.verification.llm import FakeLLMClient, LLMPurpose, SchemaT
from autolab.verification.solver import SmtFormula, SolverExecutor, Z3Solver
from autolab.verification.vericot import Verifier
from scripts.audit_load import NO_CUBES, POSITIVE, XYZ, _rss_mb, chain, pathological
from tests.fixtures.golden_traces import GoldenStep, GoldenTrace, build

P95: Final = 0.95
STEP_BUDGET_MS: Final = 50.0
HUNG: Final = 1000
"""`stress_returncode` result for an interpreter that had to be killed."""


# --- helpers -----------------------------------------------------------------


def pct(xs: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (0.0 if empty)."""
    if not xs:
        return 0.0
    ordered = sorted(xs)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def payload_chain(n: int, *, cite: bool, circular: bool = False) -> tuple[GoldenTrace, str]:
    """An `n`-step VALID chain whose last step is a `conclusion` (for `payload_claim`).

    a0; a0 => a1; a1; a1 => a2; ... ; conclusion a_k. With `circular`, one
    extra early premise states a_k outright, so SG7 marks it circular and
    re-checks the whole trace without it.
    """
    steps = [GoldenStep("a0 holds", "premise", "(declare-const a0 Bool)", "a0")]
    i = 0
    if circular:
        steps.append(GoldenStep("goal holds", "premise", "(declare-const goal Bool)", "goal"))
    while len(steps) < n - 2:
        i += 1
        steps.append(
            GoldenStep(
                f"a{i - 1} implies a{i}",
                "premise",
                f"(declare-const a{i} Bool)",
                f"(=> a{i - 1} a{i})",
            )
        )
        k = len(steps)
        refs: tuple[int, ...] = (k - 2, k - 1) if cite else ()
        steps.append(GoldenStep(f"a{i} holds", "inference", "", f"a{i}", refs))
    last = len(steps)
    if circular:
        # The goal is a{i} and the circular premise `goal` is defined to equal it.
        steps.append(GoldenStep("goal iff top", "premise", "", f"(= goal a{i})"))
        claim, assertion = "goal holds", "goal"
        refs = (1,) if cite else ()
    else:
        steps.append(GoldenStep(f"a{i} still holds", "premise", "", "true"))
        claim, assertion = f"a{i} holds finally", f"a{i}"
        refs = (last - 1,) if cite else ()
    steps.append(GoldenStep(claim, "conclusion", "", assertion, refs))
    return GoldenTrace(f"payload-{n}", "payload chain", "VALID", (), tuple(steps)), claim


class CountingExecutor(SolverExecutor):
    """A `SolverExecutor` that counts the checks it runs."""

    def __init__(self, workers: int = solver_mod.SOLVER_WORKERS) -> None:
        super().__init__(workers)
        self.checks = 0

    async def run(self, fn: Callable[[], solver_mod.T]) -> tuple[solver_mod.T, float]:
        self.checks += 1
        return await super().run(fn)


async def timed_verify(
    trace: ReasoningTrace,
    fake: FakeLLMClient,
    *,
    context: VerificationContext | None = None,
    executor: SolverExecutor | None = None,
) -> tuple[VerificationResult, float]:
    """Verify and return the result and wall time in ms."""
    started = time.perf_counter()
    result = await Verifier(fake, solver_executor=executor).verify(trace, context=context)
    return result, (time.perf_counter() - started) * 1000


class JudgeLLM(FakeLLMClient):
    """Delays formalize/judge calls, tracks judge concurrency, optionally fails a judge."""

    def __init__(
        self,
        inner: FakeLLMClient,
        *,
        formalize_s: float,
        judge_s: float,
        fail_judge_on: str | None = None,
    ) -> None:
        super().__init__()
        self._inner = inner
        self._formalize_s = formalize_s
        self._judge_s = judge_s
        self._fail_on = fail_judge_on
        self.in_flight = 0
        self.peak = 0
        self.formalize_calls = 0
        self.formalize_after_failure = 0
        self.failed_at: float | None = None

    async def call_structured(
        self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        if purpose == "faithfulness":
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            try:
                await asyncio.sleep(self._judge_s)
                if self._fail_on is not None and f"claim: {self._fail_on}\n" in prompt:
                    self.failed_at = time.perf_counter()
                    raise ConnectionError("judge connection reset (retries exhausted)")
                return schema.model_validate({"back_translation": "same", "faithful": True})
            finally:
                self.in_flight -= 1
        self.formalize_calls += 1
        if self.failed_at is not None:
            self.formalize_after_failure += 1
        await asyncio.sleep(self._formalize_s)
        return await self._inner.call_structured(purpose, prompt, schema)


def rate_limit_error(retry_after: str) -> anthropic.RateLimitError:
    """A 429 `RateLimitError` carrying `retry-after: <retry_after>`."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, headers={"retry-after": retry_after}, request=request)
    return anthropic.RateLimitError("rate limited", response=response, body=None)


class RateLimited(FakeLLMClient):
    """Always raises a 429 with `retry-after`, logging each attempt's time."""

    def __init__(self, starts: list[float], retry_after: str) -> None:
        super().__init__()
        self._starts = starts
        self._retry_after = retry_after

    async def call(self, purpose: LLMPurpose, prompt: str) -> str:
        self._starts.append(time.perf_counter())
        raise rate_limit_error(self._retry_after)


async def retry_attempt_spreads(
    clients: int, retry_after: str, backoff_s: float
) -> list[tuple[float, float]]:
    """(earliest start s, spread ms) of attempts 2 and 3 across `clients` rate-limited clients."""
    starts: list[list[float]] = [[] for _ in range(clients)]
    t0 = time.perf_counter()
    await asyncio.gather(
        *(
            RetryingLLMClient(
                RateLimited(starts[i], retry_after), initial_backoff_seconds=backoff_s
            ).call("p", "q")
            for i in range(clients)
        ),
        return_exceptions=True,
    )
    out = []
    for k in (1, 2):
        ts = [s[k] - t0 for s in starts]
        out.append((min(ts), (max(ts) - min(ts)) * 1000))
    return out


async def slow_check(executor: SolverExecutor, timeout_s: float) -> str:
    """One undecidable (runs-to-timeout) Z3 check on `executor`; returns its reason."""
    check = partial(
        Z3Solver(timeout_s).check_entailment, [SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES)
    )
    outcome, _ = await executor.run(check)
    return outcome.reason


def run_in_loops(n_loops: int, per_loop: int, executor: SolverExecutor, timeout_s: float) -> float:
    """`n_loops` threads, each running `per_loop` slow checks on its own loop; peak pool queue."""
    peak = [0]
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            peak[0] = max(peak[0], executor._pool._work_queue.qsize())
            time.sleep(0.005)

    def one_loop() -> None:
        async def main() -> None:
            await asyncio.gather(*(slow_check(executor, timeout_s) for _ in range(per_loop)))

        asyncio.run(main())

    sampler = threading.Thread(target=sample)
    sampler.start()
    threads = [threading.Thread(target=one_loop) for _ in range(n_loops)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    sampler.join()
    return float(peak[0])


def verify_in_threads(n_threads: int, per_thread: int, steps: int = 20) -> None:
    """Verify `per_thread` traces on each of `n_threads` threads, each with its own event loop.

    Fixtures are built first, on the calling thread. Run it in a subprocess
    (`stress_returncode`): a crash takes the whole interpreter down.
    """
    built = [[build(chain(steps, cite=True)) for _ in range(per_thread)] for _ in range(n_threads)]

    def worker(jobs: list[tuple[ReasoningTrace, FakeLLMClient]]) -> None:
        async def main() -> None:
            for trace, fake in jobs:
                await Verifier(fake).verify(trace)

        asyncio.run(main())

    threads = [threading.Thread(target=worker, args=(jobs,)) for jobs in built]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def stress_returncode(n_threads: int, per_thread: int, *, timeout_s: float = 30.0) -> int:
    """Exit code of a fresh interpreter running `verify_in_threads`.

    -11 is SIGSEGV, -6 SIGABRT; `HUNG` means it was killed after `timeout_s`.
    """
    code = (
        "from scripts.audit_load_2 import verify_in_threads; "
        f"verify_in_threads({n_threads}, {per_thread})"
    )
    root = Path(__file__).resolve().parent.parent
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=root,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return HUNG
    return proc.returncode


def z3_threads() -> int:
    """Live threads whose name starts with `z3`."""
    return sum(t.name.startswith("z3") for t in threading.enumerate())


# --- sections ----------------------------------------------------------------


async def section_loops() -> None:
    """Per-loop semaphores: total admitted checks scale with the number of event loops."""
    for n_loops in (1, 2, 4):
        ex = SolverExecutor(8)
        try:
            started = time.perf_counter()
            peak = await asyncio.to_thread(run_in_loops, n_loops, 16, ex, 0.3)
            wall = time.perf_counter() - started
        finally:
            ex.shutdown()
        print(
            f"loops {n_loops} loop(s) x 16 slow checks on 8 workers: peak pool queue "
            f"(admitted past the semaphore, waiting for a thread)={peak:.0f}, wall={wall:.2f} s"
        )
    ex = SolverExecutor(2)
    try:
        for _ in range(200):
            await asyncio.to_thread(asyncio.run, _one_fast_verify(ex))
        gc.collect()
        print(f"loops semaphore entries after 200 asyncio.run loops: {len(ex._slots)}")
    finally:
        ex.shutdown()


async def _one_fast_verify(ex: SolverExecutor) -> None:
    trace, fake = build(chain(4, cite=True))
    await Verifier(fake, solver_executor=ex).verify(trace)


async def section_segv(attempts: int = 10) -> None:
    """Verifies on several threads (one event loop each) crash the interpreter."""
    for n_threads, per in ((1, 40), (2, 20), (4, 10)):
        codes = [
            await asyncio.to_thread(stress_returncode, n_threads, per) for _ in range(attempts)
        ]
        crashed = [c for c in codes if c != 0]
        print(
            f"segv {n_threads} thread(s) x {per} verifies of a 20-step trace: "
            f"{len(crashed)}/{attempts} interpreters crashed, exit codes {sorted(set(codes))}"
        )


async def section_shutdown() -> None:
    """What in-flight and queued verifies see when the executor shuts down."""
    ex = SolverExecutor(4)
    built = [pathological() for _ in range(12)]
    tasks = [
        asyncio.create_task(Verifier(f, solver_timeout_seconds=3.0, solver_executor=ex).verify(t))
        for t, f in built
    ]
    await asyncio.sleep(0.3)
    started = time.perf_counter()
    ex.shutdown()
    took = time.perf_counter() - started
    outs = await asyncio.gather(*tasks, return_exceptions=True)
    kinds: dict[str, int] = {}
    for o in outs:
        if isinstance(o, BaseException):
            key = f"raised {type(o).__name__}"
        else:
            v = o.step_verdicts[-1]
            key = f"returned {v.verdict}/{v.reason} ({v.details})"
        kinds[key] = kinds.get(key, 0) + 1
    print(f"shutdown with 4 running + 8 waiting slow verifies: shutdown() took {took:.2f} s")
    for k, c in sorted(kinds.items()):
        print(f"shutdown   {c:>2} x {k}")

    # Two loops share a 2-worker executor: 2 checks run, 2 sit in the pool queue.
    ex2 = SolverExecutor(2)
    results: list[str] = []

    def loop_thread() -> None:
        async def main() -> None:
            outs = await asyncio.gather(
                *(slow_check(ex2, 3.0) for _ in range(2)), return_exceptions=True
            )
            results.extend(type(o).__name__ if isinstance(o, BaseException) else o for o in outs)

        asyncio.run(main())

    threads = [threading.Thread(target=loop_thread) for _ in range(2)]
    for t in threads:
        t.start()
    await asyncio.sleep(0.3)
    ex2.shutdown()
    for t in threads:
        t.join()
    print(f"shutdown two loops, 2 running + 2 queued in the pool: outcomes={sorted(results)}")


async def section_races(iterations: int = 2000) -> None:
    """Cancel verifies at random points; check the executor's bookkeeping and later results."""
    ex = SolverExecutor(4)
    rng = random.Random(6)
    golden = chain(6, cite=True)
    cancelled = errors = 0
    try:
        for _ in range(iterations):
            trace, fake = build(golden)
            task = asyncio.create_task(Verifier(fake, solver_executor=ex).verify(trace))
            await asyncio.sleep(rng.uniform(0, 0.04))
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                cancelled += 1
            except Exception:  # counted: any error escaping a cancelled verify is a finding
                errors += 1
        trace, fake = build(golden)
        after = await Verifier(fake, solver_executor=ex).verify(trace)
        await asyncio.sleep(0.05)
        print(
            f"races {iterations} randomly cancelled verifies: cancelled={cancelled} "
            f"errors={errors} running handles left={len(ex._running)} "
            f"next verify all VALID={all(v.verdict == 'VALID' for v in after.step_verdicts)}"
        )
    finally:
        ex.shutdown()


async def section_threads() -> None:
    """Thread counts across many Verifier instances and executors."""
    base = threading.active_count()
    for _ in range(1000):
        trace, fake = build(chain(4, cite=True))
        await Verifier(fake).verify(trace)
    print(
        f"threads 1000 Verifier() on the default executor: z3 threads={z3_threads()}, "
        f"process threads {base} -> {threading.active_count()}"
    )
    made = []
    for _ in range(50):
        ex = SolverExecutor(4)
        trace, fake = build(chain(8, cite=False))
        await Verifier(fake, solver_executor=ex).verify(trace)
        made.append(ex)
    del made, ex
    gc.collect()
    await asyncio.sleep(0.5)  # idle workers exit once their pool is collected
    print(
        f"threads 50 Verifier(solver_executor=SolverExecutor(4)) never shut down: "
        f"z3 threads={z3_threads()}"
    )


async def section_judges() -> None:
    """Faithfulness judges as tasks: fan-out, failure latency and wasted calls."""
    for n, f_s, j_s in ((100, 0.01, 0.5), (100, 0.01, 2.0), (100, 0.05, 0.05)):
        trace, fake = build(chain(n, cite=True))
        llm = JudgeLLM(fake, formalize_s=f_s, judge_s=j_s)
        started = time.perf_counter()
        await Verifier(llm, check_faithfulness=True).verify(trace)
        print(
            f"judges n={n} formalize={f_s * 1000:.0f} ms judge={j_s * 1000:.0f} ms: "
            f"peak concurrent judge calls={llm.peak}, wall={time.perf_counter() - started:.2f} s"
        )
    # 20 concurrent verifies of 100 steps, judge 0.5 s.
    llms = []
    for _ in range(20):
        trace, fake = build(chain(100, cite=True))
        llms.append((trace, JudgeLLM(fake, formalize_s=0.01, judge_s=0.5)))
    shared = {"cur": 0, "peak": 0}

    async def watch() -> None:
        while True:
            cur = sum(llm.in_flight for _, llm in llms)
            shared["peak"] = max(shared["peak"], cur)
            await asyncio.sleep(0.002)

    w = asyncio.create_task(watch())
    await asyncio.gather(*(Verifier(llm, check_faithfulness=True).verify(t) for t, llm in llms))
    w.cancel()
    await asyncio.gather(w, return_exceptions=True)
    print(f"judges 20 concurrent 100-step verifies: peak in-flight judge calls={shared['peak']}")

    # Cancel a verify while 40+ judges are in flight.
    trace, fake = build(chain(100, cite=True))
    llm = JudgeLLM(fake, formalize_s=0.01, judge_s=2.0)
    before = len(asyncio.all_tasks())
    task = asyncio.create_task(Verifier(llm, check_faithfulness=True).verify(trace))
    await asyncio.sleep(0.5)
    in_flight = llm.in_flight
    started = time.perf_counter()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    print(
        f"judges cancel a 100-step verify with {in_flight} judges in flight: settled in "
        f"{(time.perf_counter() - started) * 1000:.1f} ms, judges still in flight="
        f"{llm.in_flight}, leftover tasks={len(asyncio.all_tasks()) - before}"
    )
    for n in (20, 100):
        trace, fake = build(chain(n, cite=True))
        llm = JudgeLLM(fake, formalize_s=0.05, judge_s=0.05, fail_judge_on="a0 holds")
        started = time.perf_counter()
        try:
            await Verifier(llm, check_faithfulness=True).verify(trace)
            outcome = "no error"
        except ConnectionError as exc:
            outcome = f"raised {type(exc).__name__}"
        ended = time.perf_counter()
        failed = (llm.failed_at or ended) - started
        print(
            f"judges n={n}, step-0 judge fails at {failed:.2f} s: verify {outcome} at "
            f"{ended - started:.2f} s; formalize calls made after the failure="
            f"{llm.formalize_after_failure} of {llm.formalize_calls}"
        )


async def section_payload() -> None:
    """SG7's extra checks on bound 50/100-step traces: check count, wall, per-step cost."""
    for n in (50, 100):
        for cite in (False, True):
            for circular in (False, True):
                golden, claim = payload_chain(n, cite=cite, circular=circular)
                trace, fake = build(golden)
                ex = CountingExecutor()
                ctx = VerificationContext(payload_claim=claim)
                await timed_verify(trace, fake, context=ctx, executor=ex)  # warm-up
                ex.checks = 0
                trace, fake = build(golden)
                result, wall = await timed_verify(trace, fake, context=ctx, executor=ex)
                ex.shutdown()
                bound_checks = ex.checks
                all_ms = [v.solver_ms for v in result.step_verdicts]
                inf_ms = [
                    v.solver_ms
                    for v, s in zip(result.step_verdicts, trace.steps, strict=True)
                    if s.claim_type != "premise"
                ]
                ex2 = CountingExecutor()
                trace, fake = build(golden)
                _, unbound_wall = await timed_verify(trace, fake, executor=ex2)
                ex2.shutdown()
                last = result.step_verdicts[-1]
                print(
                    f"payload n={len(trace.steps)} cite={cite!s:<5} circular={circular!s:<5}: "
                    f"checks={bound_checks} (unbound {ex2.checks}), wall={wall:.0f} ms "
                    f"(unbound {unbound_wall:.0f}), wall/step={wall / len(trace.steps):.1f} ms, "
                    f"solver_ms p95 all={pct(all_ms, P95):.1f} inferences only="
                    f"{pct(inf_ms, P95):.1f} max={max(all_ms):.1f}; last={last.verdict}"
                )
    # Premise checks on cited traces pool every prior step: compare premise vs inference ms.
    for n in (100, 400):
        trace, fake = build(chain(n, cite=True))
        result = await Verifier(fake).verify(trace)
        prem = [
            v.solver_ms
            for v, s in zip(result.step_verdicts, trace.steps, strict=True)
            if s.claim_type == "premise"
        ]
        inf = [
            v.solver_ms
            for v, s in zip(result.step_verdicts, trace.steps, strict=True)
            if s.claim_type != "premise"
        ]
        print(
            f"payload cited chain n={n}: premise check p50={statistics.median(prem):.1f} "
            f"p95={pct(prem, P95):.1f} max={max(prem):.1f} ms vs inference p50="
            f"{statistics.median(inf):.1f} p95={pct(inf, P95):.1f} ms; "
            f"premise checks are {sum(prem) / (sum(prem) + sum(inf)):.0%} of solver time"
        )


async def section_store() -> None:
    """SQLite contention: many runs + checkpoints, several stores on one file."""
    from autolab.monitoring.tracer import RunTracer
    from autolab.orchestrator.graph import build_graph
    from autolab.state.checkpointer import sqlite_saver
    from autolab.state.schema import empty_state
    from autolab.state.store import StateStore
    from tests.integration.orchestrator.test_graph import (
        HYPOTHESIS,
        QUESTION,
        RESULT,
        FixedResultSimulator,
        _seed_full_run,
    )

    async def one(ckpt: Path, store: StateStore) -> Any:
        fake = FakeLLMClient()
        _seed_full_run(fake)
        async with sqlite_saver(ckpt) as saver:
            graph = build_graph(
                llm=fake,
                simulator=FixedResultSimulator(RESULT),
                checkpointer=saver,
                retry_backoff_seconds=0,
                tracer=RunTracer(store),
            )
            config = {"configurable": {"thread_id": str(uuid4())}}
            return await graph.ainvoke(empty_state(QUESTION), config=config)

    with tempfile.TemporaryDirectory() as d:
        cases = ((1, 100, True), (4, 25, True), (8, 25, True), (4, 25, False), (8, 25, False))
        for n_stores, per, shared_ckpt in cases:
            tag = f"{n_stores}-{per}-{shared_ckpt}"
            db = f"sqlite+aiosqlite:///{Path(d) / f'store{tag}.db'}"
            stores = [await StateStore.create(db) for _ in range(n_stores)]
            try:
                started = time.perf_counter()
                outs = await asyncio.gather(
                    *(
                        one(
                            Path(d) / (f"ckpt{tag}.db" if shared_ckpt else f"ckpt{tag}-{i}.db"),
                            stores[i % n_stores],
                        )
                        for i in range(n_stores * per)
                    ),
                    return_exceptions=True,
                )
                wall = time.perf_counter() - started
            finally:
                for s in stores:
                    await s.dispose()
            errs = [o for o in outs if isinstance(o, BaseException)]
            from_store = sum(type(e).__module__.startswith("sqlalchemy") for e in errs)
            kinds = sorted({f"{type(e).__name__}: {str(e).splitlines()[0][-30:]}" for e in errs})
            print(
                f"store {n_stores} store engine(s) on one file, {n_stores * per} concurrent "
                f"traced runs, {'one shared' if shared_ckpt else 'a separate'} checkpoint file: "
                f"wall={wall:.2f} s, runs failed={len(errs)} (store {from_store}, "
                f"checkpointer {len(errs) - from_store}) {kinds}"
            )
        # Raw contention: 200 concurrent upserts of one key from 4 engines.
        db = f"sqlite+aiosqlite:///{Path(d) / 'contend.db'}"
        stores = [await StateStore.create(db) for _ in range(4)]
        try:
            run = await stores[0].create_run("q")
            started = time.perf_counter()
            outs = await asyncio.gather(
                *(stores[i % 4].append(run, HYPOTHESIS) for i in range(200)),
                return_exceptions=True,
            )
            wall = time.perf_counter() - started
            errs = [o for o in outs if isinstance(o, BaseException)]
            rows = await stores[0].load_hypotheses(run)
        finally:
            for s in stores:
                await s.dispose()
        print(
            f"store 200 concurrent upserts of one key from 4 engines: wall={wall:.2f} s "
            f"errors={len(errs)} {sorted({type(e).__name__ for e in errs})} rows={len(rows)}"
        )


async def section_retry() -> None:
    """Jitter and retry-after under 50 concurrent rate-limited clients."""
    for label, ra, backoff in (
        ("no retry-after (header 'x')", "x", 0.5),
        ("retry-after: 1", "1", 0.5),
        ("retry-after: 1, backoff 2 s", "1", 2.0),
    ):
        (s2, sp2), (s3, sp3) = await retry_attempt_spreads(50, ra, backoff)
        print(
            f"retry 50 clients, {label}: attempt 2 from {s2:.3f} s spread {sp2:.1f} ms; "
            f"attempt 3 from {s3:.3f} s spread {sp3:.1f} ms"
        )


async def section_memory(runs: int = 200) -> None:
    """Memory over sequential graph runs: one compiled graph vs a fresh graph per run."""
    from autolab.monitoring.tracer import RunTracer
    from autolab.orchestrator.graph import build_graph
    from autolab.state.schema import empty_state
    from autolab.state.store import StateStore
    from tests.integration.orchestrator.test_graph import (
        QUESTION,
        RESULT,
        FixedResultSimulator,
        _seed_full_run,
    )

    fake = FakeLLMClient()
    _seed_full_run(fake)

    def make(tracer: RunTracer | None = None) -> Any:
        return build_graph(
            llm=fake,
            simulator=FixedResultSimulator(RESULT),
            retry_backoff_seconds=0,
            tracer=tracer,
        )

    for mode in ("one graph (default MemorySaver)", "fresh graph per run"):
        gc.collect()
        tracemalloc.start()
        base = tracemalloc.get_traced_memory()[0]
        rss0 = _rss_mb()
        graph = make()
        marks = []
        for i in range(runs):
            g = graph if mode.startswith("one") else make()
            await g.ainvoke(
                empty_state(QUESTION), config={"configurable": {"thread_id": str(uuid4())}}
            )
            if (i + 1) % 50 == 0:
                gc.collect()
                marks.append((tracemalloc.get_traced_memory()[0] - base) / 2**20)
        tracemalloc.stop()
        per = (marks[-1] - marks[0]) / (runs - 50) * 1024
        print(
            f"memory {runs} sequential runs, {mode}: traced heap growth at 50/100/150/200 = "
            f"{', '.join(f'{m:.1f}' for m in marks)} MB ({per:.0f} KB/run after 50); "
            f"rss {rss0:.0f} -> {_rss_mb():.0f} MB"
        )
        del graph
    with tempfile.TemporaryDirectory() as d:
        store = await StateStore.create(f"sqlite+aiosqlite:///{Path(d) / 's.db'}")
        tracer = RunTracer(store)
        try:
            graph = make(tracer)
            for _ in range(runs):
                await graph.ainvoke(
                    empty_state(QUESTION), config={"configurable": {"thread_id": str(uuid4())}}
                )
        finally:
            await store.dispose()
        print(
            f"memory {runs} traced runs on one RunTracer: seen-set entries={len(tracer._seen)}, "
            f"run ids kept={len(tracer._runs)}"
        )


async def section_budget() -> None:
    """CLAUDE.md: a 10-step trace verifies in < 30 s. Wall time vs per-call LLM latency."""
    for delay in (0.1, 0.5, 1.0, 2.0):
        trace, fake = build(chain(10, cite=True))
        llm = JudgeLLM(fake, formalize_s=delay, judge_s=delay)
        started = time.perf_counter()
        await Verifier(llm, check_faithfulness=True).verify(trace)
        wall = time.perf_counter() - started
        print(
            f"budget 10-step, {delay * 1000:.0f} ms per LLM call (faithfulness on): "
            f"wall={wall:.2f} s = {wall / delay:.1f} call latencies; "
            f"break-even for 30 s ~ {30 * delay / wall:.2f} s per call"
        )
    # Zero latency, bound payload trace.
    golden, claim = payload_chain(10, cite=True)
    trace, fake = build(golden)
    _, wall = await timed_verify(trace, fake, context=VerificationContext(payload_claim=claim))
    print(f"budget 10-step bound payload trace, zero LLM latency: {wall:.0f} ms")
    # One rate-limited formalize call (retry-after 1 s, twice) on a 10-step trace.
    trace, fake = build(chain(10, cite=True))

    class OneRateLimit(FakeLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.failures = 0

        async def call_structured(
            self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
        ) -> SchemaT:
            if purpose == "formalize" and self.failures < 2:
                self.failures += 1
                raise rate_limit_error("1")
            if purpose == "faithfulness":
                return schema.model_validate({"back_translation": "same", "faithful": True})
            return await fake.call_structured(purpose, prompt, schema)

    started = time.perf_counter()
    await Verifier(RetryingLLMClient(OneRateLimit()), check_faithfulness=True).verify(trace)
    print(
        f"budget 10-step with one formalize call rate-limited twice (retry-after 1 s): "
        f"{time.perf_counter() - started:.2f} s"
    )


SECTIONS: Final[dict[str, Callable[[], Awaitable[None]]]] = {
    "segv": section_segv,
    "loops": section_loops,
    "shutdown": section_shutdown,
    "races": section_races,
    "threads": section_threads,
    "judges": section_judges,
    "payload": section_payload,
    "store": section_store,
    "retry": section_retry,
    "memory": section_memory,
    "budget": section_budget,
}


async def amain(argv: Sequence[str] | None = None) -> int:
    """Run the selected sections (all by default)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("sections", nargs="*", choices=[*SECTIONS, []], default=[])
    args = parser.parse_args(argv)
    print(f"machine: {os.cpu_count()} CPUs, default solver workers={solver_mod.SOLVER_WORKERS}")
    for name in args.sections or list(SECTIONS):
        await SECTIONS[name]()
    return 0


def main() -> None:
    """CLI entry point."""
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
