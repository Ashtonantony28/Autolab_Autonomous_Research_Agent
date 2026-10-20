"""AUDIT-10 load measurements, round 3 (FakeLLMClient / stubs only; no network).

Each section prints the numbers quoted in `AUDIT-10.md`. The repro tests in
`tests/audit/test_audit_10.py` reuse the helpers defined here. Earlier rounds
are re-run with `python -m scripts.audit_load --default-timeout` and
`python -m scripts.audit_load_2`.

    uv run python -m scripts.audit_load_3            # every section
    uv run python -m scripts.audit_load_3 threads long

Sections: pool, scaling, contexts, threads, churn, long, design, hitl, printer, symbols,
pytest_timeout, bench, concurrent.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import z3

from autolab.agents.common import design_claim, design_facts
from autolab.config import SOLVER_TIMEOUT_S
from autolab.orchestrator.checkpoints import contradiction_trace, contradiction_trigger
from autolab.state.models import (
    ExperimentDesign,
    ExperimentParameter,
    Hypothesis,
    ReasoningTrace,
    VerificationContext,
)
from autolab.state.schema import empty_state
from autolab.verification import solver as solver_mod
from autolab.verification.formalizer import Formalizer, _parse_context
from autolab.verification.llm import FakeLLMClient
from autolab.verification.solver import SmtFormula, SolverExecutor, Z3Solver
from autolab.verification.vericot import Verifier
from scripts.audit_load import LatencyLLM, _rss_mb, chain, max_loop_stall_ms
from scripts.audit_load_2 import (
    P95,
    STEP_BUDGET_MS,
    CountingExecutor,
    payload_chain,
    pct,
    stress_returncode,
)
from tests.fixtures.agent_helpers import register_trace
from tests.fixtures.golden_traces import ALL, GoldenStep, build

ROOT: Final = Path(__file__).resolve().parent.parent


# --- helpers -----------------------------------------------------------------


def thread_churn(rounds: int, width: int, steps: int = 20) -> dict[str, float]:
    """`rounds` x `width` short-lived threads, each verifying one trace on its own loop.

    Returns RSS, live thread count and live `z3.Context` objects after the last
    round. Run it in a child interpreter (`churn_report`): a crash kills it.
    """

    def job() -> None:
        trace, fake = build(chain(steps, cite=True))

        async def main() -> None:
            result = await Verifier(fake).verify(trace)
            if any(v.verdict != "VALID" for v in result.step_verdicts):
                raise AssertionError(result.summary)

        asyncio.run(main())

    rss: list[float] = []
    for _ in range(rounds):
        threads = [threading.Thread(target=job) for _ in range(width)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        gc.collect()
        rss.append(_rss_mb())
    return {
        "rss_first_mb": rss[0],
        "rss_mid_mb": rss[len(rss) // 2],
        "rss_last_mb": rss[-1],
        "threads": float(threading.active_count()),
        "contexts": float(sum(isinstance(o, z3.Context) for o in gc.get_objects())),
    }


def churn_report(rounds: int, width: int, *, timeout_s: float = 120.0) -> dict[str, float]:
    """`thread_churn` in a fresh interpreter; `{"returncode": ...}` on a crash or hang."""
    code = (
        "import json; from scripts.audit_load_3 import thread_churn; "
        f"print(json.dumps(thread_churn({rounds}, {width})))"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"returncode": 1000.0}
    if proc.returncode != 0:
        return {"returncode": float(proc.returncode)}
    report: dict[str, float] = json.loads(proc.stdout.strip().splitlines()[-1])
    report["returncode"] = 0.0
    return report


def design_with(params: int) -> ExperimentDesign:
    """A design with `params` numeric parameters (one `param_p<i>` fact each)."""
    return ExperimentDesign(
        hypothesis_id=uuid4(),
        protocol="sweep",
        parameters=tuple(
            ExperimentParameter(name=f"p{i}", value=float(i) + 0.5, unit="mM")
            for i in range(params)
        ),
        estimated_cost_usd=5.0,
        estimated_duration_minutes=30.0,
    )


def design_trace(
    params: int, steps: int
) -> tuple[ReasoningTrace, FakeLLMClient, VerificationContext]:
    """A VALID `steps`-step chain bound to `design_claim` of a `params`-parameter design."""
    design = design_with(params)
    facts = design_facts(design)
    golden, _ = payload_chain(steps, cite=True)
    *body, last = golden.steps
    conclusion = GoldenStep(
        design_claim(design), "conclusion", "", last.assertion, last.referenced_step_indices
    )
    fake = FakeLLMClient()
    trace = register_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt="unused",
        steps=(*body, conclusion),
        facts=facts,
    )
    return trace, fake, VerificationContext(facts=facts, payload_claim=design_claim(design))


def contradiction_fixture(k: int) -> tuple[list[Hypothesis], Hypothesis, FakeLLMClient]:
    """`k` prior hypotheses plus a new one, with every formalization the check will request."""
    prior = [Hypothesis(question="q", statement=f"h{i} holds", rationale="r") for i in range(k)]
    new = Hypothesis(question="q", statement=f"h{k} holds", rationale="r")
    trace = contradiction_trace(prior, new)
    steps = [
        GoldenStep(s.claim, s.claim_type, f"(declare-const h{s.index} Bool)", f"h{s.index}")
        for s in trace.steps[:-1]
    ]
    last = trace.steps[-1]
    steps.append(GoldenStep(last.claim, last.claim_type, "", f"h{k}", last.referenced_step_indices))
    fake = FakeLLMClient()
    register_trace(fake, agent="x", trace_purpose="x", trace_prompt="x", steps=steps)
    return prior, new, fake


async def contradiction_cost(k: int, latency_s: float) -> tuple[float, int, int, str | None]:
    """(wall s, LLM calls, solver checks, reason) of the research policy with `k` priors."""
    prior, new, fake = contradiction_fixture(k)
    llm = LatencyLLM(fake, latency_s)
    ex = CountingExecutor()
    trigger = contradiction_trigger(
        Verifier(llm, solver_executor=ex, check_faithfulness=latency_s > 0)
    )
    state = empty_state("q")
    state["hypotheses"] = prior
    started = time.perf_counter()
    reason = await trigger(new, state)
    wall = time.perf_counter() - started
    ex.shutdown()
    return wall, llm.calls, ex.checks, reason


def invalid_checks(n: int, symbols: int = 12) -> list[tuple[list[SmtFormula], SmtFormula]]:
    """`n` distinct INVALID (sat counter-model) entailment problems."""
    out = []
    for i in range(n):
        names = [f"x{i}_{k}" for k in range(symbols)]
        decls = "\n".join(f"(declare-const {v} Int)" for v in names)
        premise = "(and " + " ".join(f"(= {v} {k + i})" for k, v in enumerate(names)) + ")"
        out.append(([SmtFormula("", premise)], SmtFormula(decls, f"(< {names[0]} 0)")))
    return out


async def counter_models(
    problems: list[tuple[list[SmtFormula], SmtFormula]], executor: SolverExecutor
) -> list[str]:
    """Each problem's `counter_model`, checked concurrently on `executor`."""
    solver = Z3Solver(5.0)

    async def one(premises: list[SmtFormula], claim: SmtFormula) -> str:
        outcome, _ = await executor.run(partial(solver.check_entailment, premises, claim))
        return outcome.counter_model

    return list(await asyncio.gather(*(one(p, c) for p, c in problems)))


def parse_novel_symbols(parses: int, per_parse: int = 20, *, novel: bool = True) -> float:
    """RSS growth (MB) after `parses` formalizer-style parses on this thread's context."""
    ctx = _parse_context()
    gc.collect()
    before = _rss_mb()
    for i in range(parses):
        tag = i if novel else 0
        decls = "\n".join(f"(declare-const s{tag}_{k} Real)" for k in range(per_parse))
        z3.parse_smt2_string(f"{decls}\n(assert (> s{tag}_0 1.5))", ctx=ctx)
    gc.collect()
    return _rss_mb() - before


HANG_TEST: Final = """
from autolab.verification.solver import SmtFormula, Z3Solver
from scripts.audit_load import NO_CUBES, POSITIVE, XYZ


def test_z3_runs_past_the_limit() -> None:
    Z3Solver(timeout_seconds={check_s}).check_entailment(
        [SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES)
    )
"""


def pytest_timeout_probe(limit_s: float, check_s: float) -> tuple[float, str]:
    """Wall time of a child pytest whose one test runs a `check_s` Z3 check on the main thread.

    The child uses the repository's pytest config with `--timeout=<limit_s>`.
    Returns (wall s, summary line).
    """
    with tempfile.TemporaryDirectory() as d:
        test = Path(d) / "test_hang_probe.py"
        test.write_text(HANG_TEST.format(check_s=check_s))
        started = time.perf_counter()
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-c",
                str(ROOT / "pyproject.toml"),
                "--rootdir",
                str(ROOT),
                "-p",
                "no:cacheprovider",
                "-q",
                f"--timeout={limit_s}",
                str(test),
            ],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True,
            text=True,
            timeout=check_s + 60,
            check=False,
        )
        wall = time.perf_counter() - started
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return wall, lines[-1] if lines else proc.stderr[-200:]


def held_contexts_rss(threads: int) -> tuple[float, float]:
    """RSS growth (MB) while `threads` idle threads are alive: (control, after one formalize).

    Control threads only wait; the others each formalize one step on their own
    event loop first (SG11 keeps a `z3.Context` per thread), then wait.
    Run it in a child interpreter (`held_contexts_report`).
    """
    trace, fake = build(chain(2, cite=False))

    def measure(formalize: bool) -> float:
        ready = threading.Barrier(threads + 1)
        release = threading.Event()

        def body() -> None:
            if formalize:
                asyncio.run(Formalizer(fake).formalize(trace.steps[0]))
            ready.wait()
            release.wait()

        gc.collect()
        before = _rss_mb()
        workers = [threading.Thread(target=body) for _ in range(threads)]
        for t in workers:
            t.start()
        ready.wait()
        grown = _rss_mb() - before
        release.set()
        for t in workers:
            t.join()
        return grown

    return measure(False), measure(True)


def held_contexts_report(threads: int, *, timeout_s: float = 60.0) -> tuple[float, float]:
    """`held_contexts_rss(threads)` in a fresh interpreter."""
    code = (
        f"from scripts.audit_load_3 import held_contexts_rss; print(*held_contexts_rss({threads}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=True,
    )
    control, formalized = proc.stdout.split()
    return float(control), float(formalized)


CHAIN_DECLS: Final = "\n".join(f"(declare-const a{i} Bool)" for i in range(50))
CHAIN_PREMISES: Final = (
    SmtFormula("", "a0"),
    *(SmtFormula("", f"(=> a{i} a{i + 1})") for i in range(10)),
)
CHAIN_CLAIM: Final = SmtFormula(CHAIN_DECLS, "a10")
"""A trivial VALID check shaped like a cited chain step: 50 symbols, 11 premises."""


def context_overhead_ratio(rounds: int = 200) -> float:
    """Median in-process `Z3Solver` check time over the same work on one reused context.

    The check runs in this process as its worker runs it, so IPC is left out.

    Both run interleaved on this thread, so machine load affects them alike. The
    baseline parses the same SMT document and runs the same two checks; it only
    skips creating and destroying a `z3.Context` per call.
    """
    solver = Z3Solver(SOLVER_TIMEOUT_S)
    premises = list(CHAIN_PREMISES)
    smt = solver_mod._build_smt2(premises, CHAIN_CLAIM)
    ctx = z3.Context()

    def reused() -> None:
        assertions = z3.parse_smt2_string(smt, ctx=ctx)
        s = z3.Solver(ctx=ctx)
        s.set("timeout", int(SOLVER_TIMEOUT_S * 1000))
        for i in range(len(assertions) - 1):
            s.add(assertions[i])
        if s.check() != z3.sat:
            raise AssertionError("premises should be consistent")
        s.add(assertions[len(assertions) - 1])
        s.add(z3.Bool(solver_mod._NEGATED_CLAIM, ctx))  # the negated claim is named (SG19)
        if s.check() != z3.unsat:
            raise AssertionError("claim should be entailed")

    fresh_ms: list[float] = []
    reused_ms: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()
        if solver._check_here(premises, CHAIN_CLAIM).verdict != "VALID":
            raise AssertionError("claim should be entailed")
        fresh_ms.append(time.perf_counter() - started)
        started = time.perf_counter()
        reused()
        reused_ms.append(time.perf_counter() - started)
    return statistics.median(fresh_ms) / statistics.median(reused_ms)


def contexts_created(fn: Callable[[], object]) -> int:
    """How many `z3.Context` objects `fn()` constructs (on any thread)."""
    real = z3.Context
    made = [0]
    lock = threading.Lock()

    class Counting(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            with lock:
                made[0] += 1
            super().__init__(*args, **kwargs)

    z3.Context = Counting
    try:
        fn()
    finally:
        z3.Context = real
    return made[0]


async def printer_peak_concurrency(checks: int, workers: int, hold_s: float = 0.02) -> int:
    """Peak number of threads inside z3's shared pretty-printer during concurrent INVALID checks.

    Z3's printer keeps its state in module globals (`z3printer._PP`), so more
    than one thread inside it at once can interleave their output.
    """
    pp_class = z3.z3printer.PP
    real = pp_class.__call__
    inside = [0, 0]
    lock = threading.Lock()

    def tracked(self: Any, out: Any, f: Any) -> Any:
        with lock:
            inside[0] += 1
            inside[1] = max(inside[1], inside[0])
        try:
            time.sleep(hold_s)
            return real(self, out, f)
        finally:
            with lock:
                inside[0] -= 1

    pp_class.__call__ = tracked
    try:
        ex = SolverExecutor(workers)
        await counter_models(invalid_checks(checks), ex)
        ex.shutdown()
    finally:
        pp_class.__call__ = real
    return inside[1]


S08_PLUGIN: Final = """
from autolab.verification import solver as solver_mod

solver_mod._remaining_ms = lambda deadline: 0
"""
"""Mutant S08 (AUDIT-8 H1): every solver check gets Z3's "no limit" timeout."""

S08_TEST: Final = (
    "tests/unit/verification/test_latency.py::test_hard_premise_consistency_check_times_out"
)
"""A sync test whose Z3 check runs on the main thread and never ends under S08."""


def s08_suite_probe(limit_s: float, kill_after_s: float) -> tuple[float, bool, str]:
    """Run `S08_TEST` under mutant S08 with `--timeout=<limit_s>` in a child pytest.

    Returns (wall s, whether the child had to be killed after `kill_after_s`, its output).
    With working per-test limits the child fails the test and exits after about `limit_s`.
    """
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "audit10_s08.py").write_text(S08_PLUGIN)
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-p",
                    "audit10_s08",
                    "-p",
                    "no:cacheprovider",
                    "-q",
                    f"--timeout={limit_s}",
                    S08_TEST,
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), d])},
                capture_output=True,
                text=True,
                timeout=kill_after_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            return time.perf_counter() - started, True, out
        return time.perf_counter() - started, False, proc.stdout + proc.stderr


def verdict_ms(result: Any, trace: ReasoningTrace, kind: str) -> list[float]:
    """`solver_ms` of the steps whose claim type is (`premise`) or is not (`other`) a premise."""
    return [
        v.solver_ms
        for v, s in zip(result.step_verdicts, trace.steps, strict=True)
        if (s.claim_type == "premise") == (kind == "premise")
    ]


# --- sections ----------------------------------------------------------------


async def section_threads() -> None:
    """SG11: verifies on several threads, each with its own loop, in child interpreters."""
    for n, per, runs in ((2, 20, 5), (4, 10, 5), (8, 10, 5), (16, 5, 5)):
        codes = Counter(stress_returncode(n, per, timeout_s=120.0) for _ in range(runs))
        print(
            f"threads {n} x {per} verifies (20 steps), {runs} interpreters: "
            f"exit codes {dict(codes)}"
        )


async def section_churn() -> None:
    """SG11: short-lived threads each verify once; RSS, threads and live contexts after."""
    for rounds, width in ((25, 4), (100, 4), (25, 16)):
        r = churn_report(rounds, width, timeout_s=600.0)
        if r["returncode"] != 0:
            print(f"churn {rounds} x {width}: child exited {r['returncode']:.0f}")
            continue
        print(
            f"churn {rounds * width} thread-verifies ({width} at a time): rss first/mid/last "
            f"{r['rss_first_mb']:.0f}/{r['rss_mid_mb']:.0f}/{r['rss_last_mb']:.0f} MB, "
            f"threads alive {r['threads']:.0f}, live z3.Context {r['contexts']:.0f}"
        )


async def verify_long(
    n: int, *, cite: bool, bound: bool, circular: bool = False
) -> tuple[ReasoningTrace, Any, float, float, int]:
    """Verify one `n`-step chain; (trace, result, wall s, max loop stall ms, checks)."""
    if bound:
        golden, claim = payload_chain(n, cite=cite, circular=circular)
        context: VerificationContext | None = VerificationContext(payload_claim=claim)
    else:
        golden, context = chain(n, cite=cite), None
    trace, fake = build(golden)
    ex = CountingExecutor()
    started = time.perf_counter()
    result, stall = await max_loop_stall_ms(
        Verifier(fake, solver_executor=ex).verify(trace, context=context)
    )
    wall = time.perf_counter() - started
    ex.shutdown()
    return trace, result, wall, stall, ex.checks


async def section_long() -> None:
    """50..800-step traces, plain and payload-bound: wall, checks, all-check p95, stall, memory."""
    for n in (50, 100, 200, 400, 800):
        for label, cite, bound, circular in (
            ("plain uncited", False, False, False),
            ("plain cited", True, False, False),
            ("bound cited", True, True, False),
            ("bound circular", True, True, True),
        ):
            trace, result, wall, stall, checks = await verify_long(
                n, cite=cite, bound=bound, circular=circular
            )
            all_ms = [v.solver_ms for v in result.step_verdicts]
            prem = verdict_ms(result, trace, "premise")
            other = verdict_ms(result, trace, "other")
            over = sum(ms > STEP_BUDGET_MS for ms in all_ms)
            print(
                f"long n={len(trace.steps):<4} {label:<15} wall={wall:6.2f} s checks={checks:<5} "
                f"step p95={pct(all_ms, P95):5.1f} max={max(all_ms):5.1f} ms (premise p95="
                f"{pct(prem, P95):5.1f}, other p95={pct(other, P95):5.1f}) over 50 ms={over:<3} "
                f"stall={stall:5.1f} ms last={result.step_verdicts[-1].verdict}"
            )
    for n in (100, 800):
        gc.collect()
        tracemalloc.start()
        await verify_long(n, cite=False, bound=False)
        peak = tracemalloc.get_traced_memory()[1] / 2**20
        tracemalloc.stop()
        print(f"long n={n} plain uncited: traced heap peak during verify {peak:.1f} MB")


async def section_design() -> None:
    """SG12: cost of the design facts by parameter count (10-step bound trace)."""
    for params in (0, 10, 100, 500, 1000):
        trace, fake, context = design_trace(params, 10)
        ex = CountingExecutor()
        started = time.perf_counter()
        result, stall = await max_loop_stall_ms(
            Verifier(fake, solver_executor=ex).verify(trace, context=context)
        )
        wall = time.perf_counter() - started
        ex.shutdown()
        all_ms = [v.solver_ms for v in result.step_verdicts]
        print(
            f"design params={params:<5} facts={len(context.facts):<5} wall={wall * 1000:7.0f} ms "
            f"checks={ex.checks} step p95={pct(all_ms, P95):6.1f} max={max(all_ms):6.1f} ms "
            f"stall={stall:5.1f} ms score={result.consistency_score:.2f}"
        )


async def section_hitl() -> None:
    """SG12: cost of the research policy (contradiction check) that approve now re-runs."""
    for k in (0, 1, 10, 50, 100):
        for latency in (0.0, 0.2):
            if k > 10 and latency:
                continue
            wall, calls, checks, reason = await contradiction_cost(k, latency)
            print(
                f"hitl research re-check, {k:<3} prior hypotheses, "
                f"{latency * 1000:3.0f} ms/LLM call: wall={wall:6.2f} s "
                f"LLM calls={calls:<4} solver checks={checks:<4} "
                f"fires={reason is not None}"
            )


async def section_printer() -> None:
    """`counter_model` strings built concurrently on the solver pool vs sequentially."""
    problems = invalid_checks(64)
    expected = await counter_models(problems, SolverExecutor(1))
    for interval in (0.005, 1e-5):
        old = sys.getswitchinterval()
        sys.setswitchinterval(interval)
        try:
            wrong = 0
            for _ in range(5):
                got = await counter_models(problems, SolverExecutor(8))
                wrong += sum(g != e for g, e in zip(got, expected, strict=True))
        finally:
            sys.setswitchinterval(old)
        print(
            f"printer 5 x 64 INVALID checks on 8 workers, GIL switch interval {interval}: "
            f"{wrong}/320 counter_model strings differ from the sequential ones"
        )
    peak = await printer_peak_concurrency(16, 8)
    print(f"printer 16 INVALID checks on 8 workers: peak threads inside z3's printer={peak}")
    ex = SolverExecutor(1)
    solver = Z3Solver(5.0)
    for symbols in (12, 200):
        (prem, claim) = invalid_checks(1, symbols)[0]
        await ex.run(partial(solver.check_entailment, prem, claim))  # warm-up
        started = time.perf_counter()
        n = 50
        for _ in range(n):
            await ex.run(partial(solver.check_entailment, prem, claim))
        per_ms = (time.perf_counter() - started) / n * 1000
        print(f"printer INVALID check with {symbols} symbols: {per_ms:.1f} ms")


async def section_symbols() -> None:
    """Z3 interns every symbol name process-wide: RSS after parses of novel vs reused names."""
    for novel in (False, True):
        grown = parse_novel_symbols(20_000, novel=novel)
        print(
            f"symbols 20000 parses x 20 {'novel' if novel else 'reused'} names on one thread: "
            f"rss +{grown:.1f} MB"
        )


async def section_pytest_timeout() -> None:
    """pytest-timeout (signal method) vs a Z3 call on the main thread."""
    for limit, check in ((1.0, 6.0), (2.0, 12.0)):
        wall, summary = pytest_timeout_probe(limit, check)
        print(
            f"pytest_timeout --timeout={limit:.0f} on a {check:.0f} s main-thread Z3 check: "
            f"child pytest took {wall:.1f} s ({summary})"
        )
    for limit, kill in ((1.0, 30.0), (10.0, 120.0)):
        wall, killed, out = s08_suite_probe(limit, kill)
        named = "Timeout" in out and "test_hard_premise_consistency_check_times_out" in out
        print(
            f"pytest_timeout mutant S08, --timeout={limit:.0f}: child "
            f"{'killed' if killed else 'exited'} after {wall:.1f} s; test named as timed out: "
            f"{named}"
        )


async def section_bench() -> None:
    """Golden-corpus step budget with and without the premise (`assumed`) checks; 10-step budget."""
    every: list[float] = []
    reported: list[float] = []
    for _ in range(5):
        for golden in ALL:
            trace, fake = build(golden, agent="bench")
            result = await Verifier(fake).verify(trace)
            for v in result.step_verdicts:
                if v.reason != "formalization_failed":
                    every.append(v.solver_ms)
                if v.reason not in {"assumed", "formalization_failed"}:
                    reported.append(v.solver_ms)
    print(
        f"bench 5 runs: bench-reported n={len(reported)} p95={pct(reported, P95):.2f} ms; "
        f"all checks n={len(every)} p95={pct(every, P95):.2f} max={max(every):.2f} ms"
    )
    for delay in (0.0, 1.0, 2.0):
        trace, fake = build(chain(10, cite=True))
        llm = LatencyLLM(fake, delay)
        started = time.perf_counter()
        await Verifier(llm, check_faithfulness=True).verify(trace)
        print(f"bench 10-step at {delay:.1f} s per LLM call: {time.perf_counter() - started:.2f} s")


async def section_concurrent() -> None:
    """Concurrent verifies of 100-step traces on one loop: wall and per-check solver_ms."""
    for n in (1, 10, 50):
        jobs = [build(chain(100, cite=True)) for _ in range(n)]
        started = time.perf_counter()
        results, stall = await max_loop_stall_ms(
            asyncio.gather(*(Verifier(f).verify(t) for t, f in jobs))
        )
        wall = time.perf_counter() - started
        ms = [v.solver_ms for r in results for v in r.step_verdicts]
        bad = sum(v.verdict != "VALID" for r in results for v in r.step_verdicts)
        print(
            f"concurrent {n:<3} x 100-step verifies: wall={wall:6.2f} s "
            f"step p95={pct(ms, P95):5.1f} ms stall={stall:5.1f} ms "
            f"non-VALID steps={bad} rss={_rss_mb():.0f} MB"
        )


async def concurrent_solver_ms(
    executor: SolverExecutor, verifies: int, steps: int
) -> tuple[list[float], float, float]:
    """`verifies` concurrent `steps`-step chains on `executor`: (solver_ms, wall s, stall ms)."""
    jobs = [build(chain(steps, cite=True)) for _ in range(verifies)]
    started = time.perf_counter()
    results, stall = await max_loop_stall_ms(
        asyncio.gather(*(Verifier(f, solver_executor=executor).verify(t) for t, f in jobs))
    )
    wall = time.perf_counter() - started
    for r in results:
        if any(v.verdict != "VALID" for v in r.step_verdicts):
            raise AssertionError(r.summary)
    return [v.solver_ms for r in results for v in r.step_verdicts], wall, stall


async def pool_inflation(verifies: int = 12, steps: int = 30) -> tuple[float, float]:
    """(p95 solver_ms on the default pool, p95 on a 1-worker pool) for the same concurrent load."""
    serial = SolverExecutor(1)
    try:
        base, _, _ = await concurrent_solver_ms(serial, verifies, steps)
    finally:
        serial.shutdown()
    pooled, _, _ = await concurrent_solver_ms(solver_mod.default_executor(), verifies, steps)
    return pct(pooled, P95), pct(base, P95)


async def section_pool() -> None:
    """Solver pool size vs throughput and solver_ms under concurrent verifies (GIL-bound)."""
    for verifies, steps in ((10, 40), (30, 40)):
        for workers in (1, 2, 4, solver_mod.SOLVER_WORKERS):
            ex = SolverExecutor(workers)
            c0 = time.process_time()
            ms, wall, stall = await concurrent_solver_ms(ex, verifies, steps)
            cpu = time.process_time() - c0
            ex.shutdown()
            print(
                f"pool {verifies} x {steps}-step verifies on {workers} workers: wall={wall:5.2f} s "
                f"cpu/wall={cpu / wall:4.2f} solver_ms p50={pct(ms, 0.5):5.1f} "
                f"p95={pct(ms, P95):5.1f} max={max(ms):6.1f} ms stall={stall:6.1f} ms"
            )


async def section_contexts() -> None:
    """Cost of a `z3.Context`: RSS per live context, per thread (SG11), per check."""
    base = _rss_mb()
    live = [z3.Context() for _ in range(10)]
    print(f"contexts 10 live z3.Context(): rss +{_rss_mb() - base:.1f} MB")
    del live
    for threads in (4, 16, 64):
        control, formalized = held_contexts_report(threads)
        print(
            f"contexts {threads} idle threads: rss +{control:.1f} MB; after one formalize each "
            f"+{formalized:.1f} MB"
        )
    for _ in range(3):
        print(
            f"contexts check_entailment / reused-context baseline: {context_overhead_ratio():.2f}"
        )
    solver = Z3Solver(SOLVER_TIMEOUT_S)
    made = contexts_created(
        lambda: [solver.check_entailment(list(CHAIN_PREMISES), CHAIN_CLAIM) for _ in range(50)]
    )
    print(f"contexts created by 50 checks on one thread: {made}")


_SCALING_S: Final = 3.0
_reused = threading.local()


def checks_per_second(threads: int, *, reuse_context: bool = False) -> float:
    """Trivial chain checks per second on `threads` threads (fresh or per-thread contexts)."""
    real = z3.Context

    def per_thread(*args: Any, **kwargs: Any) -> z3.Context:
        ctx: z3.Context | None = getattr(_reused, "ctx", None)
        if ctx is None:
            ctx = _reused.ctx = real()
        return ctx

    counts: list[int] = []

    def loop() -> None:
        solver = Z3Solver(SOLVER_TIMEOUT_S)
        n = 0
        end = time.perf_counter() + _SCALING_S
        while time.perf_counter() < end:
            solver.check_entailment(list(CHAIN_PREMISES), CHAIN_CLAIM)
            n += 1
        counts.append(n)

    if reuse_context:
        z3.Context = per_thread
    try:
        workers = [threading.Thread(target=loop) for _ in range(threads)]
        for t in workers:
            t.start()
        for t in workers:
            t.join()
    finally:
        z3.Context = real
    return sum(counts) / _SCALING_S


def processes_checks_per_second(processes: int) -> float:
    """Trivial chain checks per second over `processes` child interpreters (one thread each)."""
    code = "from scripts.audit_load_3 import checks_per_second; print(checks_per_second(1))"
    procs = [
        subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, stdout=subprocess.PIPE, text=True)
        for _ in range(processes)
    ]
    return sum(float(p.communicate()[0].strip()) for p in procs)


async def section_scaling() -> None:
    """Why the pool does not scale: threads vs processes, fresh vs reused contexts."""
    for reuse in (False, True):
        label = "one context per thread" if reuse else "fresh context per check"
        rates = [f"{n}: {checks_per_second(n, reuse_context=reuse):.0f}" for n in (1, 2, 4, 8)]
        print(f"scaling threads, {label}: checks/s {', '.join(rates)}")
    rates = [f"{n}: {processes_checks_per_second(n):.0f}" for n in (1, 4)]
    print(f"scaling processes, fresh context per check: checks/s {', '.join(rates)}")
    real = z3.Context
    spent: list[float] = []
    lock = threading.Lock()

    class Timed(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            started = time.perf_counter()
            super().__init__(*args, **kwargs)
            if threading.current_thread().name.startswith("z3"):  # solver checks only
                with lock:
                    spent.append((time.perf_counter() - started) * 1000)

    z3.Context = Timed
    try:
        for verifies in (1, 4, 10, 30):
            spent.clear()
            ms, _, _ = await concurrent_solver_ms(solver_mod.default_executor(), verifies, 40)
            print(
                f"scaling {verifies:<2} x 40-step verifies: z3.Context() p50={pct(spent, 0.5):.2f} "
                f"p95={pct(spent, P95):.2f} ms, {sum(spent) / sum(ms):.0%} of solver_ms"
            )
    finally:
        z3.Context = real


SECTIONS: Final[dict[str, Callable[[], Awaitable[None]]]] = {
    "pool": section_pool,
    "scaling": section_scaling,
    "contexts": section_contexts,
    "threads": section_threads,
    "churn": section_churn,
    "long": section_long,
    "design": section_design,
    "hitl": section_hitl,
    "printer": section_printer,
    "symbols": section_symbols,
    "pytest_timeout": section_pytest_timeout,
    "bench": section_bench,
    "concurrent": section_concurrent,
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


__all__ = [
    "churn_report",
    "context_overhead_ratio",
    "contexts_created",
    "contradiction_cost",
    "counter_models",
    "design_trace",
    "held_contexts_report",
    "invalid_checks",
    "parse_novel_symbols",
    "pool_inflation",
    "printer_peak_concurrency",
    "pytest_timeout_probe",
    "s08_suite_probe",
    "thread_churn",
]
