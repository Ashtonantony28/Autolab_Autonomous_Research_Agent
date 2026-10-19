"""AUDIT-2 load measurements (FakeLLMClient only; no real LLM, no network).

Each section prints the numbers quoted in `AUDIT-2.md`. The repro tests in
`tests/audit/test_audit_2.py` reuse the helpers defined here.

    uv run python -m scripts.audit_load            # every section
    uv run python -m scripts.audit_load pool long  # selected sections

Sections: long, concurrency, memory, pool, threads, cancel, latency, store,
graph, retry, lag. `pool --default-timeout` also runs 50 slow checks at the
5 s default solver timeout (about 35 s).
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final, TypeVar
from uuid import uuid4

import anthropic
import httpx

from autolab.config import SOLVER_TIMEOUT_S
from autolab.orchestrator.recovery import RetryingLLMClient
from autolab.state.models import ReasoningStep, ReasoningTrace
from autolab.verification import solver as solver_mod
from autolab.verification.formalizer import Vocabulary, prompt_for
from autolab.verification.llm import ClaudeLLMClient, FakeLLMClient, LLMPurpose, SchemaT
from autolab.verification.solver import SmtFormula, Z3Solver
from autolab.verification.vericot import Verifier
from tests.fixtures.golden_traces import ALL, GoldenStep, GoldenTrace, build

T = TypeVar("T")

XYZ: Final = "(declare-const x Int)\n(declare-const y Int)\n(declare-const z Int)"
POSITIVE: Final = "(and (> x 0) (> y 0) (> z 0))"
NO_CUBES: Final = "(not (= (+ (* x x x) (* y y y)) (* z z z)))"
"""Fermat n=3: true but beyond Z3's nonlinear tactics, so the check runs to its timeout."""


def default_executor_workers() -> int:
    """`max_workers` of asyncio's default `ThreadPoolExecutor` on this machine."""
    return min(32, (os.cpu_count() or 1) + 4)


def chain(n: int, *, cite: bool) -> GoldenTrace:
    """An `n`-step VALID chain: a0, a0 => a1, a1, a1 => a2, a2, ...

    With `cite=False` no step names its premises, so every prior VALID step
    joins the premise pool of every later step.
    """
    steps = [GoldenStep("a0 holds", "premise", "(declare-const a0 Bool)", "a0")]
    i = 0
    while len(steps) < n:
        i += 1
        steps.append(
            GoldenStep(
                f"a{i - 1} implies a{i}",
                "premise",
                f"(declare-const a{i} Bool)",
                f"(=> a{i - 1} a{i})",
            )
        )
        if len(steps) < n:
            k = len(steps)
            refs = (k - 2, k - 1) if cite else ()
            steps.append(GoldenStep(f"a{i} holds", "inference", "", f"a{i}", refs))
    return GoldenTrace(f"chain-{n}", "load chain", "VALID", (), tuple(steps))


def pathological() -> tuple[ReasoningTrace, FakeLLMClient]:
    """A 2-step trace whose conclusion Z3 cannot decide: it always runs to the timeout."""
    steps = (
        ReasoningStep(index=0, claim="x, y, z are positive", claim_type="premise"),
        ReasoningStep(index=1, claim="x^3 + y^3 != z^3", claim_type="conclusion"),
    )
    fake = FakeLLMClient()
    vocab = Vocabulary()
    reply0 = json.dumps({"declarations": XYZ, "assertion": POSITIVE})
    fake.register("formalize", prompt_for(steps[0], vocab), reply0)
    vocab.merge(XYZ)
    reply1 = json.dumps({"declarations": "", "assertion": NO_CUBES})
    fake.register("formalize", prompt_for(steps[1], vocab), reply1)
    return ReasoningTrace(agent="audit", steps=steps), fake


class LatencyLLM(FakeLLMClient):
    """Adds `delay_s` to every structured call and accepts every faithfulness check."""

    def __init__(self, inner: FakeLLMClient, delay_s: float) -> None:
        super().__init__()
        self._inner = inner
        self._delay_s = delay_s
        self.calls = 0

    async def call_structured(
        self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        self.calls += 1
        await asyncio.sleep(self._delay_s)
        if purpose == "faithfulness":
            return schema.model_validate({"back_translation": "same", "faithful": True})
        return await self._inner.call_structured(purpose, prompt, schema)


def rate_limited_transport(hits: list[float]) -> httpx.MockTransport:
    """An HTTP transport that answers every request 429, counting requests in `hits`."""

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(time.perf_counter())
        body = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
        return httpx.Response(429, headers={"retry-after-ms": "1"}, json=body, request=request)

    return httpx.MockTransport(handler)


async def max_loop_stall_ms(work: Awaitable[T]) -> tuple[T, float]:
    """Await `work` while a 1 ms ticker measures the longest event-loop stall."""
    gaps: list[float] = []
    done = False

    async def tick() -> None:
        last = time.perf_counter()
        while not done:
            await asyncio.sleep(0.001)
            now = time.perf_counter()
            gaps.append(now - last - 0.001)
            last = now

    ticker = asyncio.create_task(tick())
    result = await work
    done = True
    await ticker
    return result, max(gaps, default=0.0) * 1000


def _rss_mb() -> float:
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20


def _key(result: Any) -> tuple[tuple[str, str], ...]:
    return tuple((v.verdict, v.reason) for v in result.step_verdicts)


async def section_long() -> None:
    """Latency of 10..800-step traces, with and without citations."""
    scanned = [0]
    original = solver_mod._build_smt2

    def counting(premises: list[SmtFormula], claim: SmtFormula) -> str:
        scanned[0] += sum(len(p.declarations.splitlines()) for p in (*premises, claim))
        return original(premises, claim)

    solver_mod._build_smt2 = counting
    try:
        for cite in (False, True):
            for n in (10, 50, 100, 200, 400, 800):
                trace, fake = build(chain(n, cite=cite))
                scanned[0] = 0
                started = time.perf_counter()
                result = await Verifier(fake).verify(trace)
                total = (time.perf_counter() - started) * 1000
                timed = [v.solver_ms for v in result.step_verdicts if v.reason != "assumed"]
                prompt_chars = sum(len(k[1]) for k in fake._responses if k[0] == "formalize")
                print(
                    f"long cite={cite!s:<5} n={n:<4} total={total:8.0f} ms  "
                    f"step p50={sorted(timed)[len(timed) // 2]:6.1f} max={max(timed):6.1f} ms  "
                    f"decl lines scanned={scanned[0]:>9}  formalize prompt chars={prompt_chars}"
                )
    finally:
        solver_mod._build_smt2 = original


async def section_concurrency() -> None:
    """20/50 concurrent golden verifies: wall time and agreement with sequential verdicts."""
    expected = {}
    started = time.perf_counter()
    for golden in ALL:
        trace, fake = build(golden)
        expected[golden.id] = _key(await Verifier(fake).verify(trace))
    print(
        f"concurrency sequential {len(ALL)} traces: {(time.perf_counter() - started) * 1000:.0f} ms"
    )
    for n in (20, 50):
        jobs = [ALL[i % len(ALL)] for i in range(n)]
        built = [build(g) for g in jobs]
        started = time.perf_counter()
        results = await asyncio.gather(*(Verifier(f).verify(t) for t, f in built))
        wall = (time.perf_counter() - started) * 1000
        bad = sum(_key(r) != expected[g.id] for r, g in zip(results, jobs, strict=True))
        print(f"concurrency n={n}: wall={wall:.0f} ms, verdict mismatches vs sequential={bad}")


async def section_memory(batches: int = 100) -> None:
    """RSS across `batches` x 50 concurrent verifies."""
    gc.collect()
    print(f"memory start rss={_rss_mb():.0f} MB")
    for i in range(batches):
        built = [build(ALL[j % len(ALL)]) for j in range(50)]
        await asyncio.gather(*(Verifier(f).verify(t) for t, f in built))
        if (i + 1) % 20 == 0:
            gc.collect()
            print(f"memory after {(i + 1) * 50} verifies rss={_rss_mb():.0f} MB")


async def section_pool(default_timeout: bool) -> None:
    """Slow checks saturate the default executor; other to_thread users wait."""
    print(f"pool default executor workers={default_executor_workers()}")
    timeouts = [1.0, SOLVER_TIMEOUT_S] if default_timeout else [1.0]
    for timeout_s in timeouts:
        for n in (8, 20, 50):
            if timeout_s > 1.0 and n != 50:
                continue
            built = [pathological() for _ in range(n)]
            started = time.perf_counter()
            results = await asyncio.gather(
                *(Verifier(f, solver_timeout_seconds=timeout_s).verify(t) for t, f in built)
            )
            wall = time.perf_counter() - started
            ms = [r.step_verdicts[-1].solver_ms for r in results]
            print(
                f"pool timeout={timeout_s:.0f}s n={n}: wall={wall:.2f} s, "
                f"solver_ms min={min(ms):.0f} max={max(ms):.0f}"
            )
    loop = asyncio.get_running_loop()
    built = [pathological() for _ in range(50)]
    load = asyncio.gather(*(Verifier(f, solver_timeout_seconds=1.0).verify(t) for t, f in built))
    await asyncio.sleep(0.05)
    started = time.perf_counter()
    await loop.getaddrinfo("localhost", 443)
    print(f"pool getaddrinfo during 50 slow verifies: {time.perf_counter() - started:.2f} s")
    await load
    started = time.perf_counter()
    await loop.getaddrinfo("localhost", 443)
    print(f"pool getaddrinfo idle: {(time.perf_counter() - started) * 1000:.1f} ms")


async def section_threads() -> None:
    """Z3 honours its timeout with 50 checks on 50 dedicated threads (no queueing)."""
    solver = Z3Solver(timeout_seconds=1.0)
    premises, claim = [SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES)

    def one() -> tuple[str, float]:
        started = time.perf_counter()
        out = solver.check_entailment(premises, claim)
        return out.reason, time.perf_counter() - started

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=50) as pool:
        started = time.perf_counter()
        outs = await asyncio.gather(*(loop.run_in_executor(pool, one) for _ in range(50)))
        wall = time.perf_counter() - started
    print(
        f"threads 50 checks on 50 threads, timeout 1 s: wall={wall:.2f} s, "
        f"per-check max={max(d for _, d in outs):.2f} s, reasons={sorted({r for r, _ in outs})}"
    )


async def section_cancel() -> None:
    """Cancelling verify() does not stop the Z3 check it started."""
    workers = default_executor_workers()
    tasks = []
    for _ in range(workers):
        trace, fake = pathological()
        tasks.append(asyncio.create_task(Verifier(fake, solver_timeout_seconds=3.0).verify(trace)))
    await asyncio.sleep(0.2)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    started = time.perf_counter()
    await asyncio.to_thread(lambda: None)
    print(
        f"cancel {workers} verifies (timeout 3 s) cancelled at 0.2 s: "
        f"next to_thread job waited {time.perf_counter() - started:.2f} s"
    )


async def section_latency() -> None:
    """10-step trace wall time vs simulated per-call LLM latency (faithfulness on)."""
    trace, fake = build(chain(10, cite=True))
    started = time.perf_counter()
    await Verifier(fake).verify(trace)
    print(f"latency 10-step, zero LLM latency: {(time.perf_counter() - started) * 1000:.0f} ms")
    for delay in (0.1, 0.2, 0.4):
        trace, fake = build(chain(10, cite=True))
        llm = LatencyLLM(fake, delay)
        started = time.perf_counter()
        await Verifier(llm, check_faithfulness=True).verify(trace)
        wall = time.perf_counter() - started
        print(
            f"latency 10-step, {delay * 1000:.0f} ms/call: wall={wall:.2f} s for {llm.calls} "
            f"calls ({wall / (llm.calls * delay):.2f}x their sum: fully sequential)"
        )


async def section_store() -> None:
    """Concurrent upserts of one id, concurrent ensure_run, append throughput."""
    from autolab.state.store import StateStore
    from tests.integration.orchestrator.test_graph import HYPOTHESIS

    with tempfile.TemporaryDirectory() as d:
        store = await StateStore.create(f"sqlite+aiosqlite:///{Path(d) / 's.db'}")
        try:
            run = await store.create_run("q")
            outs = await asyncio.gather(
                *(store.append(run, HYPOTHESIS) for _ in range(20)), return_exceptions=True
            )
            errors = [type(o).__name__ for o in outs if isinstance(o, BaseException)]
            print(
                f"store 20 concurrent append(same id): {len(errors)} errors {sorted(set(errors))}"
            )
            rid = uuid4()
            outs2 = await asyncio.gather(
                *(store.ensure_run(rid, "q") for _ in range(10)), return_exceptions=True
            )
            errors = [type(o).__name__ for o in outs2 if isinstance(o, BaseException)]
            print(
                f"store 10 concurrent ensure_run(same id): {len(errors)} errors "
                f"{sorted(set(errors))}"
            )
            many = [HYPOTHESIS.model_copy(update={"id": uuid4()}) for _ in range(200)]
            started = time.perf_counter()
            for h in many:
                await store.append(run, h)
            per = (time.perf_counter() - started) * 1000 / len(many)
            print(f"store sequential append: {per:.1f} ms each (one transaction per entity)")
        finally:
            await store.dispose()


async def section_graph() -> None:
    """Concurrent full runs on one SQLite checkpoint file, with and without a shared store."""
    from autolab.monitoring.tracer import RunTracer
    from autolab.orchestrator.graph import build_graph
    from autolab.state.checkpointer import sqlite_saver
    from autolab.state.schema import empty_state
    from autolab.state.store import StateStore
    from tests.integration.orchestrator.test_graph import (
        QUESTION,
        RESULT,
        FixedResultSimulator,
        _seed_full_run,
    )

    async def one(db: Path, store: StateStore | None) -> Any:
        fake = FakeLLMClient()
        _seed_full_run(fake)
        async with sqlite_saver(db) as saver:
            graph = build_graph(
                llm=fake,
                simulator=FixedResultSimulator(RESULT),
                checkpointer=saver,
                retry_backoff_seconds=0,
                tracer=RunTracer(store) if store else None,
            )
            config = {"configurable": {"thread_id": str(uuid4())}}
            return await graph.ainvoke(empty_state(QUESTION), config=config)

    with tempfile.TemporaryDirectory() as d:
        for n in (10, 30):
            db = Path(d) / f"ckpt{n}.db"
            started = time.perf_counter()
            outs = await asyncio.gather(*(one(db, None) for _ in range(n)), return_exceptions=True)
            errors = sum(isinstance(o, BaseException) for o in outs)
            wall = (time.perf_counter() - started) * 1000
            print(f"graph {n} concurrent runs, one checkpoint file: {wall:.0f} ms, errors={errors}")
            store = await StateStore.create(f"sqlite+aiosqlite:///{Path(d) / f'store{n}.db'}")
            try:
                outs = await asyncio.gather(
                    *(one(Path(d) / f"t{n}.db", store) for _ in range(n)), return_exceptions=True
                )
            finally:
                await store.dispose()
            errs = [o for o in outs if isinstance(o, BaseException)]
            kinds = sorted({type(e).__name__ for e in errs})
            print(f"graph {n} concurrent traced runs, shared store: errors={len(errs)} {kinds}")
        _, stall = await max_loop_stall_ms(one(Path(d) / "lag.db", None))
        print(f"graph single run max event-loop stall: {stall:.1f} ms")


async def section_retry() -> None:
    """Retry layering, jitter and retry-after handling."""
    hits: list[float] = []
    http = httpx.AsyncClient(transport=rate_limited_transport(hits))
    inner = ClaudeLLMClient(
        "sk-audit", client=anthropic.AsyncAnthropic(api_key="sk-audit", http_client=http)
    )
    client = RetryingLLMClient(inner, initial_backoff_seconds=0.01)
    try:
        await client.call("audit", "hi")
    except anthropic.RateLimitError:
        pass  # expected: every attempt is rate limited
    finally:
        await http.aclose()
    print(f"retry HTTP requests for one logical call: {len(hits)} (SDK max_retries=2 x 3 attempts)")

    starts: dict[int, list[float]] = {}

    class Flaky(FakeLLMClient):
        def __init__(self, i: int) -> None:
            super().__init__()
            self.i = i

        async def call(self, purpose: LLMPurpose, prompt: str) -> str:
            starts.setdefault(self.i, []).append(time.perf_counter())
            raise ConnectionError("reset")

    t0 = time.perf_counter()
    await asyncio.gather(
        *(
            RetryingLLMClient(Flaky(i), initial_backoff_seconds=0.2).call("p", "q")
            for i in range(20)
        ),
        return_exceptions=True,
    )
    for k in range(3):
        ts = [v[k] - t0 for v in starts.values()]
        print(
            f"retry attempt {k + 1} of 20 clients: at {min(ts):.3f} s, "
            f"spread {(max(ts) - min(ts)) * 1000:.2f} ms"
        )


async def section_lag() -> None:
    """Longest event-loop stall while verifying long traces."""
    for n in (10, 100, 400):
        trace, fake = build(chain(n, cite=False))
        _, stall = await max_loop_stall_ms(Verifier(fake).verify(trace))
        print(f"lag verify n={n}: max event-loop stall {stall:.1f} ms")


SECTIONS: Final[dict[str, Callable[[argparse.Namespace], Awaitable[None]]]] = {
    "long": lambda a: section_long(),
    "concurrency": lambda a: section_concurrency(),
    "memory": lambda a: section_memory(),
    "pool": lambda a: section_pool(a.default_timeout),
    "threads": lambda a: section_threads(),
    "cancel": lambda a: section_cancel(),
    "latency": lambda a: section_latency(),
    "store": lambda a: section_store(),
    "graph": lambda a: section_graph(),
    "retry": lambda a: section_retry(),
    "lag": lambda a: section_lag(),
}


async def amain(argv: Sequence[str] | None = None) -> int:
    """Run the selected sections (all by default)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("sections", nargs="*", choices=[*SECTIONS, []], default=[])
    parser.add_argument("--default-timeout", action="store_true")
    args = parser.parse_args(argv)
    for name in args.sections or list(SECTIONS):
        await SECTIONS[name](args)
    return 0


def main() -> None:
    """CLI entry point."""
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
