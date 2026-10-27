"""AUDIT-14 load measurements, round 4 (FakeLLMClient / stubs only; no network).

Each section prints the numbers quoted in `AUDIT-14.md`. The repro tests in
`tests/audit/test_audit_14.py` reuse the helpers defined here. Earlier rounds
are re-run with `scripts.audit_load --default-timeout`, `scripts.audit_load_2`
and `scripts.audit_load_3`.

    uv run python -m scripts.audit_load_4            # every section
    uv run python -m scripts.audit_load_4 assume starve

Sections: assume, overrun, starve, cpus, pool, interrupt, shutdown, reset, probe,
second, loop, memory, bench.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from functools import partial
from pathlib import Path
from typing import Final

import z3

from autolab.config import SOLVER_TIMEOUT_S
from autolab.verification import formalizer as formalizer_mod
from autolab.verification import solver as solver_mod
from autolab.verification.solver import SmtFormula, SolverExecutor, SolverOutcome, Z3Solver
from autolab.verification.vericot import Verifier
from scripts.audit_load import NO_CUBES, POSITIVE, XYZ, _rss_mb, chain, max_loop_stall_ms
from scripts.audit_load_2 import P95, STEP_BUDGET_MS, CountingExecutor, pct
from scripts.audit_load_3 import design_trace
from tests.fixtures.golden_traces import GoldenStep, GoldenTrace, build

ROOT: Final = Path(__file__).resolve().parent.parent


# --- problems ------------------------------------------------------------------


def lia_chain(n: int) -> tuple[list[SmtFormula], SmtFormula]:
    """`n - 1` linear integer premises b_i > b_{i+1} + 1 and the claim b0 > 0 (not entailed).

    The shape of an uncited premise pool in a trace of numeric steps: every
    prior VALID step is a premise.
    """
    decls = "\n".join(f"(declare-const b{i} Int)" for i in range(n))
    premises = [SmtFormula("", f"(> b{i} (+ b{i + 1} 1))") for i in range(n - 1)]
    return premises, SmtFormula(decls, "(> b0 0)")


def lia_trace(n: int, *, cite: bool = False) -> GoldenTrace:
    """`n` steps: numeric premises b_{i-1} > b_i + 1, then an inference b0 > b_{n-2}.

    Uncited by default, so every step's premise pool is every prior VALID step.
    """
    steps = [GoldenStep("b0 is an integer", "premise", "(declare-const b0 Int)", "(>= b0 b0)")]
    for i in range(1, n - 1):
        steps.append(
            GoldenStep(
                f"b{i - 1} exceeds b{i} by more than one",
                "premise",
                f"(declare-const b{i} Int)",
                f"(> b{i - 1} (+ b{i} 1))",
            )
        )
    refs = tuple(range(n - 1)) if cite else ()
    steps.append(GoldenStep("b0 exceeds the last b", "inference", "", f"(> b0 b{n - 2})", refs))
    return GoldenTrace(f"lia-{n}", "numeric chain", "VALID", (), tuple(steps))


def _add_based_check(smt2: str, premises: int, true_claim: bool, timeout_s: float) -> SolverOutcome:
    """The pre-SG19 second check, for comparison: assert `claim!` instead of assuming it."""
    deadline = time.monotonic() + timeout_s
    ctx = z3.Context()
    solver = z3.Solver(ctx=ctx)
    try:
        solver.from_string(smt2)
    except z3.Z3Exception as exc:
        return SolverOutcome("UNGROUNDED", "formalization_failed", f"SMT parse error: {exc}")
    solver.set("timeout", max(1, int((deadline - time.monotonic()) * 1000)))
    consistency = solver.check()
    if consistency == z3.unsat:
        return SolverOutcome("INVALID", "premises_inconsistent", "")
    if consistency == z3.sat:
        solver.add(z3.Bool(solver_mod._NEGATED_CLAIM, ctx))
        solver.set("timeout", max(1, int((deadline - time.monotonic()) * 1000)))
        result = solver.check()
        if result == z3.unsat:
            return SolverOutcome("VALID", "entailed", "")
        if result == z3.sat:
            return SolverOutcome("INVALID", "not_entailed", "", solver.model().sexpr())
    return SolverOutcome("UNGROUNDED", "timeout", "")


@contextmanager
def add_based_checks() -> Iterator[None]:
    """Temporarily run `Z3Solver` checks the pre-SG19 way, in this process (comparison only)."""
    real_solve, real_check = solver_mod._solve, Z3Solver.check_entailment
    solver_mod._solve = _add_based_check
    Z3Solver.check_entailment = Z3Solver._check_here  # type: ignore[method-assign]
    try:
        yield
    finally:
        solver_mod._solve = real_solve
        Z3Solver.check_entailment = real_check  # type: ignore[method-assign]


def timed_check(
    premises: list[SmtFormula], claim: SmtFormula, timeout_s: float
) -> tuple[str, float]:
    """(reason, wall s) of one `check_entailment` from this thread (in a worker process)."""
    started = time.perf_counter()
    outcome = Z3Solver(timeout_s).check_entailment(premises, claim)
    return outcome.reason, time.perf_counter() - started


def core_check(
    premises: list[SmtFormula], claim: SmtFormula, timeout_s: float
) -> tuple[str, float]:
    """(reason, wall s) of the check a worker would run, run in this process (`_solve`)."""
    started = time.perf_counter()
    outcome = Z3Solver(timeout_s)._check_here(premises, claim)
    return outcome.reason, time.perf_counter() - started


def overrun_ratio(n: int = 1600, timeout_s: float = 0.5) -> float:
    """Wall time of the `lia_chain(n)` check over its timeout (1.0 means the bound held)."""
    premises, claim = lia_chain(n)
    _, wall = timed_check(premises, claim, timeout_s)
    return wall / timeout_s


def assume_vs_add(n: int, rounds: int = 3) -> tuple[float, float]:
    """Median wall ms of the `lia_chain(n)` check: (as built, pre-SG19 add-based)."""
    premises, claim = lia_chain(n)
    now, old = [], []
    for _ in range(rounds):
        now.append(core_check(premises, claim, 30.0)[1])
        with add_based_checks():
            old.append(core_check(premises, claim, 30.0)[1])
    return sorted(now)[rounds // 2] * 1000, sorted(old)[rounds // 2] * 1000


async def verify_ms(golden: GoldenTrace, executor: SolverExecutor | None = None) -> list[float]:
    """`solver_ms` of every step of one verify of `golden`."""
    trace, fake = build(golden)
    result = await Verifier(fake, solver_executor=executor).verify(trace)
    return [v.solver_ms for v in result.step_verdicts]


async def lia_step_p95(n: int) -> tuple[float, float, float, float]:
    """Step p95 and max ms of an `n`-step numeric uncited trace: as built, then add-based."""
    now = await verify_ms(lia_trace(n))
    with add_based_checks():
        old = await verify_ms(lia_trace(n))
    return pct(now, P95), max(now), pct(old, P95), max(old)


# --- cancellation and interrupts -----------------------------------------------


def interrupt_threads() -> int:
    """Live `z3-interrupt` re-send threads."""
    return sum(t.name == "z3-interrupt" for t in threading.enumerate())


@contextmanager
def counted_interrupts() -> Iterator[list[int]]:
    """Count `z3.Context.interrupt` calls (any thread)."""
    real = z3.Context.interrupt
    calls = [0]

    def counting(self: z3.Context) -> None:
        calls[0] += 1
        real(self)

    z3.Context.interrupt = counting
    try:
        yield calls
    finally:
        z3.Context.interrupt = real


async def cancel_release_s(
    premises: list[SmtFormula], claim: SmtFormula, *, after_s: float, timeout_s: float = 30.0
) -> tuple[float, int, float]:
    """Cancel one check after `after_s`; (s until its worker is free, interrupts sent, CPU s).

    CPU is process time from the cancel until the worker is free.
    """
    ex = SolverExecutor(1)
    solver = Z3Solver(timeout_s)
    task = asyncio.create_task(ex.run(partial(solver.check_entailment, premises, claim)))
    await asyncio.sleep(after_s)
    with counted_interrupts() as calls:
        started, cpu0 = time.perf_counter(), time.process_time()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await ex.run(lambda: None)
        freed, cpu = time.perf_counter() - started, time.process_time() - cpu0
    ex.shutdown()
    return freed, calls[0], cpu


async def many_cancels(n: int, workers: int = 2) -> tuple[int, int, float]:
    """Cancel `n` pathological checks mid-flight, `workers` at a time.

    Returns (peak live re-send threads, live re-send threads after, worst seconds
    from a cancel until its worker took the next job).
    """
    ex = SolverExecutor(workers)
    solver = Z3Solver(3.0)
    check = partial(solver.check_entailment, [SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES))
    peak, worst = 0, 0.0
    for i in range(0, n, workers):
        tasks = [asyncio.create_task(ex.run(check)) for _ in range(workers)]
        await asyncio.sleep(0.02 + (i % 7) * 0.003)
        started = time.perf_counter()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        peak = max(peak, interrupt_threads())
        await asyncio.gather(*(ex.run(lambda: None) for _ in range(workers)))
        worst = max(worst, time.perf_counter() - started)
    ex.shutdown()
    await asyncio.sleep(0.05)
    return peak, interrupt_threads(), worst


async def shutdown_under_load(verifies: int, steps: int) -> tuple[float, Counter[str]]:
    """Shut a 2-worker pool down while `verifies` verifies run; (shutdown s, how each ended)."""
    ex = SolverExecutor(2)
    jobs = [build(chain(steps, cite=True)) for _ in range(verifies)]
    tasks = [asyncio.create_task(Verifier(f, solver_executor=ex).verify(t)) for t, f in jobs]
    await asyncio.sleep(0.2)
    started = time.perf_counter()
    await asyncio.to_thread(ex.shutdown)
    took = time.perf_counter() - started
    done = await asyncio.gather(*tasks, return_exceptions=True)
    return took, Counter(type(d).__name__ for d in done)


# --- starvation ------------------------------------------------------------------


async def slow_verify(executor: SolverExecutor, timeout_s: float, checks: int) -> None:
    """`checks` back-to-back runs-to-timeout checks, as one run with a hard trace makes."""
    solver = Z3Solver(timeout_s)
    check = partial(solver.check_entailment, [SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES))
    for _ in range(checks):
        await executor.run(check)


async def fast_trace_under_slow_runs(
    workers: int, slow_runs: int, timeout_s: float, steps: int = 10
) -> tuple[float, float]:
    """Wall s of a `steps`-step easy verify alone, then while `slow_runs` hard runs verify.

    Each hard run makes back-to-back checks that each run to `timeout_s`, the
    way a run whose trace Z3 cannot decide does.
    """
    ex = SolverExecutor(workers)
    try:
        trace, fake = build(chain(steps, cite=True))
        started = time.perf_counter()
        await Verifier(fake, solver_executor=ex).verify(trace)
        alone = time.perf_counter() - started
        hogs = [asyncio.create_task(slow_verify(ex, timeout_s, 1000)) for _ in range(slow_runs)]
        await asyncio.sleep(0.05)
        trace, fake = build(chain(steps, cite=True))
        started = time.perf_counter()
        await Verifier(fake, solver_executor=ex).verify(trace)
        loaded = time.perf_counter() - started
        for h in hogs:
            h.cancel()
        await asyncio.gather(*hogs, return_exceptions=True)
    finally:
        ex.shutdown()
    return alone, loaded


# --- CPU count -------------------------------------------------------------------

CPUS_CHILD: Final = """
import asyncio, json, os, time
os.cpu_count = lambda: {cpus}
from autolab.verification import solver as solver_mod
from scripts.audit_load_3 import concurrent_solver_ms
from scripts.audit_load_2 import pct, P95

async def main():
    ms, wall, stall = await concurrent_solver_ms(solver_mod.default_executor(), {verifies}, {steps})
    print(json.dumps({{"workers": solver_mod.SOLVER_WORKERS, "wall": wall,
                       "p95": pct(ms, P95), "stall": stall}}))

asyncio.run(main())
"""


def cpus_report(cpus: int, verifies: int = 10, steps: int = 100) -> dict[str, float]:
    """Default pool size and a load's numbers, in a child whose `os.cpu_count()` is `cpus`."""
    code = CPUS_CHILD.format(cpus=cpus, verifies=verifies, steps=steps)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    report: dict[str, float] = json.loads(proc.stdout.strip().splitlines()[-1])
    return report


# --- reset_params ------------------------------------------------------------------

RESET_CHILD: Final = """
import asyncio, threading, z3
from autolab.verification.vericot import Verifier
from scripts.audit_load import chain
from tests.fixtures.golden_traces import build
bad = [0]

def worker():
    async def main():
        for _ in range({per}):
            t, f = build(chain(20, cite=True))
            r = await Verifier(f).verify(t)
            bad[0] += sum(v.verdict != "VALID" for v in r.step_verdicts)
    asyncio.run(main())

def setter():
    for i in range({per} * 50):
        z3.set_param("smt.random_seed", i % 7)

ts = [threading.Thread(target=worker) for _ in range({threads})]
ts += [threading.Thread(target=setter) for _ in range({setters})]
[t.start() for t in ts]
[t.join() for t in ts]
print("bad", bad[0])
"""


def reset_race_returncodes(threads: int, per: int, setters: int, runs: int) -> Counter[str]:
    """Exit codes (and non-VALID counts) of child interpreters verifying on `threads` threads.

    `setters` more threads set a global Z3 parameter while the solver workers
    call `z3.reset_params()` before every check.
    """
    code = RESET_CHILD.format(threads=threads, per=per, setters=setters)
    out: Counter[str] = Counter()
    for _ in range(runs):
        try:
            proc = subprocess.run(
                [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=180
            )
        except subprocess.TimeoutExpired:
            out["hung"] += 1
            continue
        tail = proc.stdout.strip().splitlines()[-1:] or [""]
        out[f"rc={proc.returncode} {tail[0]}"] += 1
    return out


def reset_params_us(rounds: int = 20000) -> float:
    """Mean microseconds per `z3.reset_params()`."""
    started = time.perf_counter()
    for _ in range(rounds):
        z3.reset_params()
    return (time.perf_counter() - started) / rounds * 1e6


def host_param_survives_a_check(param: str = "timeout", value: str = "60000") -> bool:
    """Whether a global parameter the host set is still set after one solver check."""
    z3.set_param(param, value)
    try:
        Z3Solver(1.0).check_entailment(list(lia_chain(3)[0]), lia_chain(3)[1])
        return bool(z3.get_param(param) == value)
    finally:
        z3.reset_params()


# --- SG15 probe, SG16 second pass --------------------------------------------------


def probe_cost(names: int) -> tuple[float, float]:
    """(µs per uncached builtin probe, RSS MB grown) over `names` novel names."""
    formalizer_mod._parse_context()
    gc.collect()
    before = _rss_mb()
    started = time.perf_counter()
    for i in range(names):
        formalizer_mod._is_z3_builtin(f"audit14_novel_{i}_{time.monotonic_ns()}")
    per_us = (time.perf_counter() - started) / names * 1e6
    gc.collect()
    return per_us, _rss_mb() - before


def premise_heavy_payload(premises: int, *, circular: bool) -> tuple[GoldenTrace, str]:
    """`premises` independent Bool premises, then a conclusion citing all of them.

    With `circular`, the last premise states the conclusion, so the second
    pass drops it and the trace is checked a third time.
    """
    steps = [
        GoldenStep(f"p{i} holds", "premise", f"(declare-const p{i} Bool)", f"p{i}")
        for i in range(premises)
    ]
    steps.append(
        GoldenStep("goal holds", "premise", "(declare-const goal Bool)", "(= goal (and p0 p1))")
    )
    if circular:
        steps.append(GoldenStep("goal really holds", "premise", "", "goal"))
    claim = "goal follows"
    refs = tuple(range(len(steps)))
    steps.append(GoldenStep(claim, "conclusion", "", "goal", refs))
    return GoldenTrace(f"premises-{premises}", "premise heavy", "VALID", (), tuple(steps)), claim


async def second_pass_cost(premises: int, *, circular: bool) -> tuple[int, float, str]:
    """(solver checks, wall s, last verdict) of a payload-bound premise-heavy verify."""
    from autolab.state.models import VerificationContext

    golden, claim = premise_heavy_payload(premises, circular=circular)
    trace, fake = build(golden)
    ex = CountingExecutor()
    started = time.perf_counter()
    result = await Verifier(fake, solver_executor=ex).verify(
        trace, context=VerificationContext(payload_claim=claim)
    )
    wall = time.perf_counter() - started
    ex.shutdown()
    return ex.checks, wall, result.step_verdicts[-1].verdict


# --- sections ----------------------------------------------------------------------


async def section_assume() -> None:
    """SG19's assumption-based entailment check vs the add-based one it replaced."""
    for n in (100, 200, 400, 800, 1200, 1600):
        now, old = assume_vs_add(n)
        print(
            f"assume lia_chain({n}) check: as built {now:7.1f} ms, "
            f"add-based {old:7.1f} ms ({now / old:4.1f}x)"
        )
    for n in (50, 100, 200, 400, 800):
        p95, mx, op95, omx = await lia_step_p95(n)
        print(
            f"assume {n}-step numeric uncited trace: step p95 {p95:6.1f} max {mx:6.1f} ms; "
            f"add-based p95 {op95:6.1f} max {omx:6.1f} ms"
        )
    for params in (0, 100, 500, 1000):
        trace, fake, context = design_trace(params, 10)
        res = await Verifier(fake).verify(trace, context=context)
        ms = [v.solver_ms for v in res.step_verdicts]
        with add_based_checks():
            res2 = await Verifier(fake).verify(trace, context=context)
        ms2 = [v.solver_ms for v in res2.step_verdicts]
        print(
            f"assume design params={params:<5}: step p95 {pct(ms, P95):6.1f} ms, "
            f"add-based {pct(ms2, P95):6.1f} ms"
        )


async def section_overrun() -> None:
    """Checks that outlive their timeout, and how long a cancel takes to free the worker."""
    for timeout in (0.5, 1.0, 5.0):
        for n in (800, 1000, 1200, 1400, 1600, 2000):
            premises, claim = lia_chain(n)
            reason, wall = timed_check(premises, claim, timeout)
            with add_based_checks():
                oreason, owall = timed_check(premises, claim, timeout)
            print(
                f"overrun lia_chain({n}) timeout {timeout:.1f} s: {reason} after {wall:5.2f} s "
                f"({wall / timeout:4.1f}x); add-based {oreason} after {owall:5.2f} s"
            )
    for n in (1600, 2000):
        premises, claim = lia_chain(n)
        freed, calls, cpu = await cancel_release_s(premises, claim, after_s=0.8)
        print(
            f"overrun cancel lia_chain({n}) at 0.8 s: worker free after {freed:5.2f} s, "
            f"{calls} interrupts sent, process CPU {cpu:5.2f} s"
        )


async def section_starve() -> None:
    """A 10-step easy verify while hard runs hold the 1- or 2-worker pool."""
    for workers, slow_runs in ((2, 0), (2, 1), (2, 2), (1, 1), (8, 2)):
        alone, loaded = await fast_trace_under_slow_runs(workers, slow_runs, 1.0)
        print(
            f"starve {workers} workers, {slow_runs} hard runs (1 s checks): 10-step verify "
            f"{alone:5.2f} s alone, {loaded:6.2f} s loaded"
        )
    alone, loaded = await fast_trace_under_slow_runs(2, 2, SOLVER_TIMEOUT_S)
    print(f"starve default timeout 5 s, 2 hard runs: 10-step verify {loaded:6.1f} s loaded")


async def section_cpus() -> None:
    """Default pool size and 10 x 100-step load with `os.cpu_count` patched."""
    for cpus in (1, 2, 4, 64):
        r = cpus_report(cpus)
        print(
            f"cpus os.cpu_count()={cpus:<3}: workers={r['workers']:.0f} wall={r['wall']:5.2f} s "
            f"step p95={r['p95']:5.1f} ms stall={r['stall']:5.1f} ms"
        )


async def section_pool() -> None:
    """Concurrent verifies of 50- to 800-step traces on the default pool."""
    for verifies, steps in ((10, 50), (10, 100), (30, 100), (10, 200), (4, 400), (2, 800)):
        jobs = [build(chain(steps, cite=True)) for _ in range(verifies)]
        started = time.perf_counter()
        results, stall = await max_loop_stall_ms(
            asyncio.gather(*(Verifier(f).verify(t) for t, f in jobs))
        )
        wall = time.perf_counter() - started
        ms = [v.solver_ms for r in results for v in r.step_verdicts]
        bad = sum(v.verdict != "VALID" for r in results for v in r.step_verdicts)
        over = sum(m > STEP_BUDGET_MS for m in ms)
        print(
            f"pool {verifies:<2} x {steps:<3}-step cited: wall={wall:6.2f} s step p95="
            f"{pct(ms, P95):5.1f} max={max(ms):6.1f} ms over 50 ms={over:<3} "
            f"stall={stall:5.1f} ms non-VALID={bad}"
        )


async def section_interrupt() -> None:
    """Re-send threads under many cancels; what a cancel costs."""
    for n in (100, 400):
        peak, after, worst = await many_cancels(n)
        print(
            f"interrupt {n} cancels of running checks (2 at a time): peak re-send threads {peak}, "
            f"left after {after}, worst worker release {worst * 1000:6.1f} ms"
        )
    freed, calls, _ = await cancel_release_s(
        [SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES), after_s=0.2
    )
    print(
        f"interrupt cancel of a pathological check: free after {freed * 1000:.1f} ms, {calls} sent"
    )


async def section_shutdown() -> None:
    """Shutting the pool down while verifies run."""
    for verifies, steps in ((10, 100), (50, 100)):
        took, ends = await shutdown_under_load(verifies, steps)
        print(
            f"shutdown during {verifies} x {steps}-step verifies: {took * 1000:.0f} ms; "
            f"ends {dict(ends)}"
        )


async def section_reset() -> None:
    """`z3.reset_params()` before every check: cost and thread safety."""
    print(f"reset z3.reset_params(): {reset_params_us():.2f} us per call")
    print(f"reset a host's global timeout survives one check: {host_param_survives_a_check()}")
    for threads, per, setters in ((2, 10, 0), (4, 10, 0), (8, 5, 0), (4, 10, 2)):
        codes = reset_race_returncodes(threads, per, setters, 5)
        print(f"reset {threads} threads x {per} verifies, {setters} param setters: {dict(codes)}")


async def section_probe() -> None:
    """SG15's cached builtin probe on many novel names."""
    for names in (10_000, 100_000):
        per_us, grown = probe_cost(names)
        info = formalizer_mod._is_z3_builtin.cache_info()
        print(
            f"probe {names} novel names: {per_us:.0f} us each, rss +{grown:.1f} MB, "
            f"cache size {info.currsize}/{info.maxsize}"
        )


async def section_second() -> None:
    """SG16's second payload pass on premise-heavy traces."""
    for premises in (10, 50, 100, 200, 400):
        for circular in (False, True):
            checks, wall, last = await second_pass_cost(premises, circular=circular)
            print(
                f"second {premises:<3} premises circular={circular!s:<5}: checks={checks:<4} "
                f"wall={wall:6.2f} s last={last}"
            )


async def section_loop() -> None:
    """Event-loop stalls while 10 / 50 verifies share the default pool."""
    for verifies in (1, 10, 50):
        jobs = [build(lia_trace(60)) for _ in range(verifies)]
        _, stall = await max_loop_stall_ms(
            asyncio.gather(*(Verifier(f).verify(t) for t, f in jobs))
        )
        print(f"loop {verifies:<2} x 60-step numeric verifies: max loop stall {stall:6.1f} ms")


async def section_memory() -> None:
    """RSS over many verifies on the default pool."""
    rss = [_rss_mb()]
    for _ in range(10):
        jobs = [build(chain(40, cite=True)) for _ in range(100)]
        await asyncio.gather(*(Verifier(f).verify(t) for t, f in jobs))
        gc.collect()
        rss.append(_rss_mb())
    print("memory rss every 100 verifies: " + " ".join(f"{r:.0f}" for r in rss) + " MB")


async def section_bench() -> None:
    """CLAUDE.md budgets: 10-step < 30 s, and bench_verifier's step p95."""
    from scripts.audit_load import LatencyLLM
    from scripts.bench_verifier import format_report, run

    print(format_report(await run(20)))
    for delay in (0.0, 1.0, 2.0):
        trace, fake = build(chain(10, cite=True))
        llm = LatencyLLM(fake, delay)
        started = time.perf_counter()
        await Verifier(llm, check_faithfulness=True).verify(trace)
        print(f"bench 10-step at {delay:.1f} s per LLM call: {time.perf_counter() - started:.2f} s")


SECTIONS: Final[dict[str, Callable[[], Awaitable[None]]]] = {
    "assume": section_assume,
    "overrun": section_overrun,
    "starve": section_starve,
    "cpus": section_cpus,
    "pool": section_pool,
    "interrupt": section_interrupt,
    "shutdown": section_shutdown,
    "reset": section_reset,
    "probe": section_probe,
    "second": section_second,
    "loop": section_loop,
    "memory": section_memory,
    "bench": section_bench,
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
    "add_based_checks",
    "cancel_release_s",
    "fast_trace_under_slow_runs",
    "lia_chain",
    "lia_trace",
    "overrun_ratio",
]
