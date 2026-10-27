"""AUDIT-18 load measurements, round 5 (FakeLLMClient / stubs only; no network).

Each section prints the numbers quoted in `AUDIT-18.md`. The repro tests in
`tests/audit/test_audit_18.py` reuse the helpers defined here. Earlier rounds
are re-run with `scripts.audit_load`, `scripts.audit_load_2`, `scripts.audit_load_3`
and `scripts.audit_load_4`.

    uv run python -m scripts.audit_load_5            # every section
    uv run python -m scripts.audit_load_5 handoff probe

Sections: handoff, cap, oversub, cancel, threads, phases, guard, real, probe, tokens,
figures, store, loop, memory.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Sequence
from concurrent.futures import Future
from contextlib import contextmanager, suppress
from functools import partial
from pathlib import Path
from typing import Any, Final

import z3

from autolab.agents.common import protocol_figures
from autolab.verification import formalizer as formalizer_mod
from autolab.verification import solver as solver_mod
from autolab.verification.formalizer import Formalizer, Vocabulary
from autolab.verification.solver import SmtFormula, SolverExecutor, Z3Solver
from autolab.verification.vericot import Verifier
from scripts.audit_load import NO_CUBES, POSITIVE, XYZ, _rss_mb, chain, max_loop_stall_ms
from scripts.audit_load_2 import P95, STEP_BUDGET_MS, pct
from scripts.audit_load_4 import cancel_release_s, lia_chain, lia_trace, slow_verify, timed_check
from tests.fixtures.golden_traces import GoldenStep, GoldenTrace, build

ROOT: Final = Path(__file__).resolve().parent.parent
EASY: Final = SmtFormula("(declare-const a Bool)", "a")
"""A check that decides in about a millisecond."""


# --- problems --------------------------------------------------------------------


def real_chain(n: int, claim: str = "(> r0 0.0)") -> tuple[list[SmtFormula], SmtFormula]:
    """`lia_chain` over Reals: `n - 1` premises r_i > r_{i+1} + 1.0, claim r0 > 0 by default.

    `claim="true"` gives the check a premise step makes (the pool must be consistent).
    """
    decls = "\n".join(f"(declare-const r{i} Real)" for i in range(n))
    premises = [SmtFormula("", f"(> r{i} (+ r{i + 1} 1.0))") for i in range(n - 1)]
    return premises, SmtFormula(decls, claim)


def bool_chain(n: int, claim: str | None = None) -> tuple[list[SmtFormula], SmtFormula]:
    """`a0` and `n - 1` implications a_i => a_{i+1}; the claim a_{n-1} is entailed."""
    decls = "\n".join(f"(declare-const a{i} Bool)" for i in range(n))
    premises = [SmtFormula("", "a0")]
    premises += [SmtFormula("", f"(=> a{i} a{i + 1})") for i in range(n - 1)]
    return premises, SmtFormula(decls, claim or f"a{n - 1}")


def _true(claim: SmtFormula) -> SmtFormula:
    """The claim a premise step checks: `true`, with `claim`'s declarations."""
    return SmtFormula(claim.declarations, "true")


def lra_trace(n: int) -> GoldenTrace:
    """`lia_trace` over Reals: numeric premises r_{i-1} > r_i + 1.0, then r0 > r_{n-2}."""
    steps = [GoldenStep("r0 is a real", "premise", "(declare-const r0 Real)", "(>= r0 r0)")]
    for i in range(1, n - 1):
        steps.append(
            GoldenStep(
                f"r{i - 1} exceeds r{i} by more than one",
                "premise",
                f"(declare-const r{i} Real)",
                f"(> r{i - 1} (+ r{i} 1.0))",
            )
        )
    steps.append(GoldenStep("r0 exceeds the last r", "inference", "", f"(> r0 r{n - 2})"))
    return GoldenTrace(f"lra-{n}", "real chain", "VALID", (), tuple(steps))


def check_phases(
    premises: list[SmtFormula], claim: SmtFormula, rounds: int = 2
) -> tuple[float, float, str]:
    """(s in the consistency check, s in the entailment check, reason), best of `rounds`.

    Measured by spying on `z3.Solver.check` inside `check_entailment` (30 s timeout).
    """
    real_check = z3.Solver.check
    stamps: list[float] = []

    def check(self: z3.Solver, *assumptions: z3.ExprRef) -> z3.CheckSatResult:
        stamps.append(time.perf_counter())
        try:
            return real_check(self, *assumptions)
        finally:
            stamps.append(time.perf_counter())

    best: tuple[float, float, str] | None = None
    z3.Solver.check = check
    try:
        for _ in range(rounds):
            stamps.clear()
            reason = Z3Solver(30.0)._check_here(premises, claim).reason
            entail = stamps[3] - stamps[2] if len(stamps) == 4 else 0.0
            got = (stamps[1] - stamps[0], entail, reason)
            best = got if best is None or got < best else best
    finally:
        z3.Solver.check = real_check
    assert best is not None
    return best


def real_int_ratio(n: int = 800) -> tuple[float, float]:
    """(ms, ms) of the premise check (claim `true`) over `n` Real and `n` Int chain premises.

    Best of two each, interleaved, so machine load scales both.
    """
    real: list[float] = []
    ints: list[float] = []
    for _ in range(2):
        for make, out in ((real_chain, real), (lia_chain, ints)):
            premises, claim = make(n)
            started = time.perf_counter()
            Z3Solver(60.0).check_entailment(premises, SmtFormula(claim.declarations, "true"))
            out.append((time.perf_counter() - started) * 1000)
    return min(real), min(ints)


# --- #1: the slot hand-off and the pool's idle permits ------------------------------


class _GatedIdle:
    """Stands in for the step a pool worker takes to mark itself idle after a job:
    `_Pool._next`, or SG22's `ThreadPoolExecutor._idle_semaphore.release`.

    Once armed, the worker's next "I am idle" step waits until the pool's next
    `submit` has returned. That is the interleaving the event loop produces on its
    own whenever it submits a caller's next check before the worker thread that
    finished the previous one has marked itself idle.
    """

    def __init__(
        self, idle: Callable[..., Any], semaphore: threading.Semaphore | None = None
    ) -> None:
        self._idle = idle
        self._semaphore = semaphore
        self.armed_at: int | None = None
        self.submitted = 0
        self._cond = threading.Condition()

    def __call__(self, *args: Any) -> Any:
        with self._cond:
            target, self.armed_at = self.armed_at, None
            if target is not None:
                self._cond.wait_for(lambda: self.submitted > target, timeout=10.0)
        return self._idle(*args)

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        assert self._semaphore is not None
        return self._semaphore.acquire(blocking, timeout)

    def release(self, n: int = 1) -> None:
        self(n)

    def note_submit(self) -> None:
        with self._cond:
            self.submitted += 1
            self._cond.notify_all()


@contextmanager
def gated_pool(ex: SolverExecutor) -> Iterator[_GatedIdle]:
    """Install `_GatedIdle` on `ex`'s pool and count its submits."""
    pool = ex._pool
    semaphore = getattr(pool, "_idle_semaphore", None)
    if semaphore is not None:  # SG22's ThreadPoolExecutor
        gate = _GatedIdle(semaphore.release, semaphore)
        setattr(pool, "_idle_semaphore", gate)  # noqa: B010 - not an attribute of `_Pool`
    else:
        gate = _GatedIdle(pool._next)
        pool._next = gate  # type: ignore[method-assign]
    real_submit = pool.submit

    def submit(fn: Callable[..., Any], /, *args: Any) -> Future[Any]:
        try:
            return real_submit(fn, *args)
        finally:
            gate.note_submit()

    pool.submit = submit  # type: ignore[method-assign]
    try:
        yield gate
    finally:
        pool.submit = real_submit  # type: ignore[method-assign]


async def easy_check_beside_a_slow_one(hard_timeout_s: float = 3.0) -> tuple[float, int]:
    """Seconds an easy check takes while one hard check runs, after one stale idle permit.

    1-worker pool. Two trivial jobs run back to back, and the second is submitted
    before the worker has marked itself idle after the first (forced with
    `_GatedIdle`; it happens on its own in about one hand-off in six). SG22's pool then
    held one more idle permit than idle threads. A hard check takes the slot, runs
    past `_SLICE_S` and gives the slot up, raising `_max_workers` to 2. The easy check
    that takes the slot is submitted next. Returns (easy check s, pool threads then).
    """
    ex = SolverExecutor(1, slow_workers=4)
    try:
        await ex.run(lambda: None)  # one thread, idle
        with gated_pool(ex) as gate:
            own = gate.submitted + 1  # `first`'s own submit

            def first() -> None:
                gate.armed_at = own  # the release after this job waits for the next submit

            await ex.run(first)
            await ex.run(lambda: None)
        hard = partial(
            Z3Solver(hard_timeout_s).check_entailment,
            [SmtFormula(XYZ, POSITIVE)],
            SmtFormula(XYZ, NO_CUBES),
        )
        hog = asyncio.create_task(ex.run(hard))
        while ex._slow == 0:  # until the hard check has given up its slot
            await asyncio.sleep(0.005)
        started = time.perf_counter()
        await ex.run(partial(Z3Solver(hard_timeout_s).check_entailment, [EASY], EASY))
        took = time.perf_counter() - started
        threads = len(ex._pool._threads)
        hog.cancel()
        with suppress(asyncio.CancelledError):
            await hog
    finally:
        ex.shutdown()
    return took, threads


async def easy_verify_beside_hard_runs(
    workers: int, hard: int, timeout_s: float, slow_workers: int | None = None
) -> tuple[float, int, int]:
    """A 10-step verify beside `hard` runs of runs-to-timeout checks.

    Returns (verify s, most pool work items queued at once, fewest pool threads seen
    while the verify ran). The pool is warmed by one verify first, as in a live process.
    """
    slow = solver_mod.SOLVER_SLOW_WORKERS if slow_workers is None else slow_workers
    ex = SolverExecutor(workers, slow)
    pool = ex._pool
    queued, threads = 0, 1 << 30
    try:
        trace, fake = build(chain(10, cite=True))
        await Verifier(fake, solver_executor=ex).verify(trace)
        hogs = [asyncio.create_task(slow_verify(ex, timeout_s, 1000)) for _ in range(hard)]
        await asyncio.sleep(0.05)

        async def watch() -> None:
            nonlocal queued, threads
            while True:
                queued = max(queued, pool._work_queue.qsize())
                threads = min(threads, len(pool._threads))
                await asyncio.sleep(0.01)

        watcher = asyncio.create_task(watch())
        trace, fake = build(chain(10, cite=True))
        started = time.perf_counter()
        await Verifier(fake, solver_executor=ex).verify(trace)
        took = time.perf_counter() - started
        watcher.cancel()
        for h in hogs:
            h.cancel()
        await asyncio.gather(watcher, *hogs, return_exceptions=True)
    finally:
        ex.shutdown()
    return took, queued, threads


# --- #4: CPU-bound checks beyond the CPUs ----------------------------------------------

OVERSUB_CHILD: Final = """
import asyncio, json, os, time
from functools import partial
os.sched_setaffinity(0, {{min(os.sched_getaffinity(0))}})
os.cpu_count = lambda: {cpus}
from autolab.verification import solver as solver_mod
from autolab.verification.solver import SolverExecutor, Z3Solver
from scripts.audit_load_4 import lia_chain

async def main():
    slow = solver_mod.SOLVER_SLOW_WORKERS if {slow} < 0 else {slow}
    ex = SolverExecutor(solver_mod.SOLVER_WORKERS, slow)
    premises, claim = lia_chain({n})
    check = partial(Z3Solver({timeout}).check_entailment, premises, claim)
    started = time.perf_counter()
    alone = (await ex.run(check))[0].reason
    alone_s = time.perf_counter() - started
    started = time.perf_counter()
    outs = await asyncio.gather(*(ex.run(check) for _ in range({k})))
    wall = time.perf_counter() - started
    ex.shutdown()
    print(json.dumps({{"alone": alone, "alone_s": alone_s, "wall": wall,
                      "workers": solver_mod.SOLVER_WORKERS, "slow": slow,
                      "reasons": [o[0].reason for o in outs]}}))

asyncio.run(main())
"""


def oversubscribed(
    k: int, *, cpus: int, n: int = 2000, timeout_s: float = 3.0, slow: int = -1
) -> dict[str, Any]:
    """`k` concurrent `lia_chain(n)` checks in a child pinned to one CPU.

    `cpus` is what `os.cpu_count()` reports there (the pool's default sizes follow
    it); `slow=-1` keeps the default `slow_workers`.
    """
    code = OVERSUB_CHILD.format(cpus=cpus, slow=slow, n=n, timeout=timeout_s, k=k)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
    )
    report: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
    return report


# --- #2: the applied-builtin probe ----------------------------------------------------


class _TooManyParses(Exception):
    pass


def parses_to_merge(decl: str, cap: int = 1000) -> int:
    """How many Z3 parses `Vocabulary.merged(decl)` makes, stopping after `cap` + 1."""
    real = z3.Z3_parse_smtlib2_string
    count = [0]

    def counting(*args: Any) -> Any:
        count[0] += 1
        if count[0] > cap:
            raise _TooManyParses
        return real(*args)

    formalizer_mod._applied_builtin.cache_clear()
    formalizer_mod._is_z3_builtin.cache_clear()
    z3.Z3_parse_smtlib2_string = counting
    try:
        with suppress(_TooManyParses):
            Vocabulary().merged(decl)
    finally:
        z3.Z3_parse_smtlib2_string = real
    return count[0]


def int_function(arity: int) -> str:
    """`(declare-fun f<arity> (Int ... Int) Bool)`."""
    return f"(declare-fun f{arity} ({' '.join(['Int'] * arity)}) Bool)"


# --- #8, #9: quadratic scans -------------------------------------------------------------


async def tokenized_chars(n: int) -> tuple[int, int]:
    """(characters `_tokens` scans while formalizing an `n`-step chain, size of the result).

    The result is the final vocabulary plus every assertion, which is all the
    earlier-use rule needs to have seen once.
    """
    trace, fake = build(chain(n, cite=False))
    real = formalizer_mod._tokens
    scanned = [0]

    def counting(text: str) -> set[str]:
        scanned[0] += len(text)
        return real(text)

    formalizer = Formalizer(fake)
    formalizer_mod._tokens = counting
    try:
        formulas = [await formalizer.formalize(s) for s in trace.steps]
    finally:
        formalizer_mod._tokens = real
    size = len(formalizer.vocabulary.smt()) + sum(len(f.assertion) for f in formulas)
    return scanned[0], size


def figures_ms(numbers: int, rounds: int = 3) -> float:
    """Best ms of `protocol_figures` over a protocol listing `numbers` well positions."""
    text = " ".join(f"well {i % 96 + 1}," for i in range(numbers))
    best = float("inf")
    for _ in range(rounds):
        started = time.perf_counter()
        protocol_figures(text)
        best = min(best, (time.perf_counter() - started) * 1000)
    return best


# --- #6: the LangGraph checkpointer under a held lock ------------------------------------


async def checkpoint_write_under_lock(path: Path, hold_cap_s: float = 8.0) -> tuple[str, float]:
    """Write one checkpoint while another connection holds the file's write lock.

    The lock is held until the write returns or `hold_cap_s` passes. Returns
    (the exception's type name or "ok", seconds the write took).
    """
    from langgraph.checkpoint.base import empty_checkpoint

    from autolab.state.checkpointer import sqlite_saver

    async with sqlite_saver(path) as saver:
        holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        try:
            holder.execute("BEGIN EXCLUSIVE")
            config: Any = {"configurable": {"thread_id": "t", "checkpoint_ns": ""}}
            started = time.perf_counter()
            write = asyncio.create_task(saver.aput(config, empty_checkpoint(), {}, {}))
            done, _ = await asyncio.wait((write,), timeout=hold_cap_s)
            if not done:
                holder.execute("ROLLBACK")
            outcome = "ok"
            try:
                await write
            except sqlite3.OperationalError as exc:
                outcome = f"OperationalError: {exc}"
            took = time.perf_counter() - started
        finally:
            with suppress(sqlite3.OperationalError):
                holder.execute("ROLLBACK")
            holder.close()
    return outcome, took


# --- sections -------------------------------------------------------------------------


async def section_handoff() -> None:
    """SG22's slot hand-off under hard runs, and the stale-permit interleaving."""
    took, threads = await easy_check_beside_a_slow_one(3.0)
    print(
        f"handoff forced stale permit: easy check {took:5.2f} s beside a 3 s check, {threads} th."
    )
    for workers, hard, timeout, trials in (
        (1, 1, 0.5, 8),
        (1, 2, 0.5, 8),
        (1, 3, 0.5, 8),
        (2, 2, 0.5, 10),
        (2, 4, 0.5, 8),
        (1, 3, 5.0, 2),
        (2, 4, 5.0, 3),
    ):
        runs = [await easy_verify_beside_hard_runs(workers, hard, timeout) for _ in range(trials)]
        times = " ".join(f"{t:.2f}" for t, _, _ in runs)
        stuck = sum(q > 0 for _, q, _ in runs)
        print(
            f"handoff {workers} worker(s), {hard} hard runs at {timeout:.1f} s: 10-step verify "
            f"[{times}] s; trials with a check queued in the pool: {stuck}/{trials}"
        )


async def section_cap() -> None:
    """More hard runs than `slow_workers`."""
    for workers, slow, hard, timeout in (
        (2, 8, 8, 5.0),
        (2, 8, 10, 5.0),
        (2, 8, 12, 5.0),
        (2, 8, 16, 5.0),
        (1, 5, 6, 5.0),
        (1, 5, 7, 1.0),
    ):
        took, queued, threads = await easy_verify_beside_hard_runs(workers, hard, timeout, slow)
        print(
            f"cap {workers} worker(s), slow_workers={slow}, {hard} hard runs at {timeout:.0f} s: "
            f"10-step verify {took:6.2f} s (pool queue max {queued}, threads min {threads})"
        )


async def section_oversub() -> None:
    """CPU-bound checks on one pinned CPU, by what `os.cpu_count()` reports."""
    for cpus, slow, k, timeout in (
        (1, -1, 4, 3.0),
        (1, 0, 4, 3.0),
        (4, -1, 10, 5.0),
        (4, 0, 10, 5.0),
    ):
        r = oversubscribed(k, cpus=cpus, timeout_s=timeout, slow=slow)
        reasons = Counter(r["reasons"])
        print(
            f"oversub 1 CPU, cpu_count={cpus} (workers {r['workers']}, slow {r['slow']}): "
            f"{k} x lia_chain(2000) at {timeout:.0f} s: alone {r['alone']} in {r['alone_s']:.2f} s;"
            " "
            f"together {dict(reasons)} in {r['wall']:.2f} s"
        )


async def section_threads() -> None:
    """Pool threads after bursts of slow checks; `_max_workers` afterwards."""
    for workers, slow, burst in ((2, 8, 12), (2, 32, 40)):
        ex = SolverExecutor(workers, slow)
        before = threading.active_count()
        solver = Z3Solver(0.5)
        check = partial(
            solver.check_entailment, [SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES)
        )
        await asyncio.gather(*(ex.run(check) for _ in range(burst)))
        peak = len(ex._pool._threads)
        await asyncio.gather(*(ex.run(lambda: None) for _ in range(50)))
        print(
            f"threads workers={workers} slow={slow}: {burst} slow checks -> pool threads {peak}, "
            f"process threads {before} -> {threading.active_count()}, _max_workers back to "
            f"{ex._pool._max_workers}, _slow {ex._slow}"
        )
        ex.shutdown()


async def section_phases() -> None:
    """Consistency and entailment phases by theory and pool size."""
    kinds: dict[str, Callable[[int], tuple[list[SmtFormula], SmtFormula]]] = {
        "bool": bool_chain,
        "int": lia_chain,
        "real": real_chain,
        "bool premise": partial(bool_chain, claim="true"),
        "int premise": lambda n: (lia_chain(n)[0], _true(lia_chain(n)[1])),
        "real premise": partial(real_chain, claim="true"),
    }
    for name, make in kinds.items():
        for n in (50, 200, 400, 800, 1600):
            premises, claim = make(n)
            cons, entail, reason = check_phases(premises, claim)
            print(
                f"phases {name:<12} n={n:<5}: consistency {cons * 1000:8.1f} ms, "
                f"entailment {entail * 1000:7.1f} ms ({reason})"
            )


async def section_cancel() -> None:
    """Timeouts and cancels on large Int, Real and Boolean pools."""
    for name, make in (("int", lia_chain), ("real", real_chain), ("bool", bool_chain)):
        premises, claim = make(1600)
        for timeout_s in (0.5, 1.0, 5.0):
            reason, wall = timed_check(premises, claim, timeout_s)
            print(
                f"cancel {name} 1600 premises, timeout {timeout_s:.1f} s: {reason} after "
                f"{wall:5.2f} s ({wall / timeout_s:4.2f}x)"
            )
        freed, calls, _ = await cancel_release_s(premises, claim, after_s=0.4)
        print(
            f"cancel {name} 1600 premises at 0.4 s: worker free after {freed:5.2f} s ({calls} sent)"
        )


async def section_guard() -> None:
    """`_SETUP_SHARE` at the default timeout: premise checks that need ms to finish."""
    for n in (1100, 1200, 1300):
        premises, claim = real_chain(n, claim="true")
        started = time.perf_counter()
        outcome = Z3Solver().check_entailment(premises, claim)
        print(
            f"guard real premise check n={n}: {outcome.verdict}/{outcome.reason} "
            f"({outcome.details}) after {time.perf_counter() - started:.2f} s; "
            f"with 30 s: {Z3Solver(30.0).check_entailment(premises, claim).verdict}"
        )


async def section_real() -> None:
    """Real-valued pools against the same pools over Int."""
    for n in (400, 800, 1000):
        real, ints = real_int_ratio(n)
        print(
            f"real premise check n={n}: Real {real:7.1f} ms, Int {ints:6.1f} ms "
            f"({real / ints:.1f}x)"
        )
    for n in (50, 100, 200, 400):
        trace, fake = build(lra_trace(n))
        ms = [v.solver_ms for v in (await Verifier(fake).verify(trace)).step_verdicts]
        trace, fake = build(lia_trace(n))
        ims = [v.solver_ms for v in (await Verifier(fake).verify(trace)).step_verdicts]
        print(
            f"real {n}-step trace: step p95 {pct(ms, P95):6.1f} ms max {max(ms):7.1f} ms; "
            f"Int p95 {pct(ims, P95):6.1f} ms; over {STEP_BUDGET_MS:.0f} ms: "
            f"{sum(m > STEP_BUDGET_MS for m in ms)}"
        )


async def section_probe() -> None:
    """SG20's applied-builtin probe by arity."""
    for arity in (2, 4, 8, 10, 12, 14):
        decl = int_function(arity)
        formalizer_mod._applied_builtin.cache_clear()
        started = time.perf_counter()
        Vocabulary().merged(decl)
        took = time.perf_counter() - started
        print(
            f"probe {arity:>2} Int arguments: {parses_to_merge(decl, 1 << 20)} parses, "
            f"merge {took * 1000:8.1f} ms on the calling thread"
        )

    async def stall(arity: int) -> float:
        formalizer_mod._applied_builtin.cache_clear()

        async def merge() -> None:
            await asyncio.sleep(0.01)  # let the ticker start
            Vocabulary().merged(int_function(arity))

        _, ms = await max_loop_stall_ms(merge())
        return ms

    print(f"probe 12-argument declaration: event-loop stall {await stall(12):.0f} ms")


async def section_tokens() -> None:
    """SG20's earlier-use rule: characters tokenized per formalized trace."""
    for n in (50, 200, 800):
        scanned, size = await tokenized_chars(n)
        print(
            f"tokens {n}-step chain: scanned {scanned} chars for {size} chars of SMT "
            f"({scanned / size:.0f}x)"
        )
    for n in (400, 1600):
        trace, fake = build(lia_trace(n))
        formalizer = Formalizer(fake)
        started = time.perf_counter()
        for s in trace.steps:
            await formalizer.formalize(s)
        print(f"tokens {n}-step numeric trace: formalize {time.perf_counter() - started:.2f} s")


async def section_figures() -> None:
    """SG21's figure scan on long protocols."""
    for numbers in (500, 1000, 2000, 4000, 8000):
        print(f"figures {numbers} numbers ({numbers * 9} chars): {figures_ms(numbers):8.1f} ms")


async def section_store() -> None:
    """The LangGraph checkpointer and the store under held locks."""
    with tempfile.TemporaryDirectory() as d:
        outcome, took = await checkpoint_write_under_lock(Path(d) / "ckpt.db")
        print(f"store checkpoint write under a held lock: {outcome} after {took:.2f} s")


async def section_loop() -> None:
    """Event-loop stalls from formalizing long traces."""
    for n in (400, 1600):
        trace, fake = build(lia_trace(n))

        async def formalize_all(fake: Any = fake, trace: Any = trace) -> None:
            f = Formalizer(fake)
            for s in trace.steps:
                await f.formalize(s)

        _, stall = await max_loop_stall_ms(formalize_all())
        print(f"loop formalize {n}-step numeric trace: max stall {stall:.1f} ms")


async def section_memory() -> None:
    """RSS over many verifies beside hard runs (slow lane in use)."""
    ex = SolverExecutor()
    hogs = [asyncio.create_task(slow_verify(ex, 0.3, 100000)) for _ in range(4)]
    rss = [_rss_mb()]
    for _ in range(6):
        jobs = [build(chain(40, cite=True)) for _ in range(50)]
        await asyncio.gather(*(Verifier(f, solver_executor=ex).verify(t) for t, f in jobs))
        gc.collect()
        rss.append(_rss_mb())
    for h in hogs:
        h.cancel()
    await asyncio.gather(*hogs, return_exceptions=True)
    ex.shutdown()
    print("memory rss every 50 verifies: " + " ".join(f"{r:.0f}" for r in rss) + " MB")


SECTIONS: Final[dict[str, Callable[[], Awaitable[None]]]] = {
    "handoff": section_handoff,
    "cap": section_cap,
    "oversub": section_oversub,
    "cancel": section_cancel,
    "threads": section_threads,
    "phases": section_phases,
    "guard": section_guard,
    "real": section_real,
    "probe": section_probe,
    "tokens": section_tokens,
    "figures": section_figures,
    "store": section_store,
    "loop": section_loop,
    "memory": section_memory,
}


async def amain(argv: Sequence[str] | None = None) -> int:
    """Run the selected sections (all by default)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("sections", nargs="*", choices=[*SECTIONS, []], default=[])
    args = parser.parse_args(argv)
    print(
        f"machine: {os.cpu_count()} CPUs, workers={solver_mod.SOLVER_WORKERS}, "
        f"slow_workers={solver_mod.SOLVER_SLOW_WORKERS}, load {os.getloadavg()[0]:.1f}"
    )
    for name in args.sections or list(SECTIONS):
        await SECTIONS[name]()
    return 0


def main() -> None:
    """CLI entry point."""
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()


__all__ = [
    "bool_chain",
    "check_phases",
    "checkpoint_write_under_lock",
    "easy_check_beside_a_slow_one",
    "easy_verify_beside_hard_runs",
    "figures_ms",
    "int_function",
    "lra_trace",
    "oversubscribed",
    "parses_to_merge",
    "real_chain",
    "real_int_ratio",
    "tokenized_chars",
]
