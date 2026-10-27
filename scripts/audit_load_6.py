"""AUDIT-22 load measurements, round 6 (FakeLLMClient / stubs only; no network).

Each section prints the numbers quoted in `AUDIT-22.md`. The repro tests in
`tests/audit/test_audit_22.py` reuse the helpers defined here. Earlier rounds
are re-run with `scripts.audit_load_{3,4,5}`.

    uv run python -m scripts.audit_load_6            # every section
    uv run python -m scripts.audit_load_6 probe gil

Sections: probe, gil, concurrent, unpolled, cancel, newcomer, cgroup, estimate.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from functools import partial
from typing import Any, Final

import z3

from autolab.agents.common import design_claim, design_facts
from autolab.agents.design import (
    DesignAgent,
    build_design_prompt,
    build_design_trace_prompt,
)
from autolab.orchestrator.checkpoints import cost_trigger
from autolab.state.models import ExperimentDesign, ExperimentParameter, Hypothesis, ReasoningStep
from autolab.state.schema import empty_state
from autolab.verification import formalizer as formalizer_mod
from autolab.verification import solver as solver_mod
from autolab.verification.formalizer import Formalizer, Vocabulary, prompt_for
from autolab.verification.llm import FakeLLMClient, LLMPurpose, SchemaT
from autolab.verification.solver import SmtFormula, SolverExecutor, Z3Solver
from autolab.verification.vericot import Verifier
from scripts.audit_load import chain, max_loop_stall_ms
from scripts.audit_load_2 import P95, pct
from scripts.audit_load_4 import cancel_release_s, lia_trace, timed_check
from scripts.audit_load_5 import EASY, ROOT, bool_chain, int_function, lra_trace, real_chain
from tests.fixtures.agent_helpers import register_estimate, register_trace
from tests.fixtures.golden_traces import GoldenStep, GoldenTrace, build

# --- #1: the applied-builtin probe on a wide declaration ------------------------------


def wide_reply(arity: int) -> tuple[ReasoningStep, FakeLLMClient]:
    """A one-step formalization declaring an `arity`-argument Int function and applying it."""
    step = ReasoningStep(index=0, claim=f"f holds of {arity} integers", claim_type="premise")
    zeros = " ".join(["0"] * arity)
    fake = FakeLLMClient()
    reply = {"declarations": int_function(arity), "assertion": f"(f{arity} {zeros})"}
    fake.register("formalize", prompt_for(step, Vocabulary()), json.dumps(reply))
    return step, fake


async def formalize_wide_s(arity: int) -> tuple[float, float]:
    """(s to formalize `wide_reply(arity)`, longest event-loop stall in ms meanwhile)."""
    formalizer_mod._applied_builtin.cache_clear()
    formalizer_mod._is_z3_builtin.cache_clear()
    step, fake = wide_reply(arity)

    async def formalize() -> None:
        await asyncio.sleep(0.01)  # the stall ticker is running by now
        await Formalizer(fake).formalize(step)

    started = time.perf_counter()
    _, stall = await max_loop_stall_ms(formalize())
    return time.perf_counter() - started - 0.01, stall


class _ParseBudget(Exception):
    pass


def chars_parsed_to_merge(decl: str, budget: int) -> int:
    """Characters `Vocabulary.merged(decl)` hands Z3's parser, stopping past `budget`."""
    real = z3.Z3_parse_smtlib2_string
    parsed = [0]

    def counting(*args: Any) -> Any:
        parsed[0] += len(args[1])
        if parsed[0] > budget:
            raise _ParseBudget
        return real(*args)

    formalizer_mod._applied_builtin.cache_clear()
    formalizer_mod._is_z3_builtin.cache_clear()
    z3.Z3_parse_smtlib2_string = counting
    try:
        with suppress(_ParseBudget):
            Vocabulary().merged(decl)
    finally:
        z3.Z3_parse_smtlib2_string = real
    return parsed[0]


def merge_s(decl: str) -> float:
    """Wall s of `Vocabulary.merged(decl)`, caches cleared."""
    formalizer_mod._applied_builtin.cache_clear()
    formalizer_mod._is_z3_builtin.cache_clear()
    started = time.perf_counter()
    Vocabulary().merged(decl)
    return time.perf_counter() - started


# --- #3: one GIL hand-off per premise ---------------------------------------------------


@contextmanager
def python_thread() -> Iterator[None]:
    """A thread running pure Python, as the event loop does while it formalizes or parses."""
    stop = threading.Event()

    def spin() -> None:
        while not stop.is_set():
            pass

    thread = threading.Thread(target=spin, name="python-work", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def premise_check(n: int, timeout_s: float = 30.0) -> tuple[str, float]:
    """(reason, wall s) of the check a premise step makes over `bool_chain(n)`."""
    premises, claim = bool_chain(n, claim="true")
    return timed_check(premises, claim, timeout_s)


def gil_overhead_s(n: int) -> tuple[float, float]:
    """(best quiet s, best s beside `python_thread`) of the `bool_chain(n)` premise check."""
    quiet = min(premise_check(n)[1] for _ in range(3))
    with python_thread():
        busy = min(premise_check(n)[1] for _ in range(2))
    return quiet, busy


def entailment_beside_python(n: int, share: float) -> tuple[str, str, float, float]:
    """`bool_chain(n)`'s entailment check, with a timeout of `share` x its quiet run time.

    Returns (quiet reason, reason beside `python_thread`, quiet s, timeout s).
    """
    premises, claim = bool_chain(n)
    quiet = [timed_check(premises, claim, 30.0) for _ in range(3)]
    alone_s = min(s for _, s in quiet)
    timeout_s = share * alone_s
    with python_thread():
        reason, _ = timed_check(premises, claim, timeout_s)
    return quiet[0][0], reason, alone_s, timeout_s


async def step_p95(make: Callable[[int], GoldenTrace], n: int, k: int) -> tuple[float, float, int]:
    """`k` concurrent verifies of `make(n)` on a fresh default-sized executor.

    Returns (wall s, step p95 solver_ms, non-VALID steps).
    """
    ex = SolverExecutor()
    try:
        jobs = [build(make(n)) for _ in range(k)]
        started = time.perf_counter()
        results = await asyncio.gather(
            *(Verifier(f, solver_executor=ex).verify(t) for t, f in jobs)
        )
        wall = time.perf_counter() - started
    finally:
        ex.shutdown()
    ms = [v.solver_ms for r in results for v in r.step_verdicts]
    bad = sum(v.verdict != "VALID" for r in results for v in r.step_verdicts)
    return wall, pct(ms, P95), bad


@contextmanager
def one_call_loading() -> Iterator[None]:
    """Load every pool in one call, as before SG27 (comparison only; since SG29 checks run
    in worker processes, so this reaches only `_check_here`)."""
    real = solver_mod._LOAD_ABOVE
    solver_mod._LOAD_ABOVE = 1 << 30  # type: ignore[misc]
    try:
        yield
    finally:
        solver_mod._LOAD_ABOVE = real  # type: ignore[misc]


# --- #4: one unpolled simplex in pools of few, long premises ----------------------------


def conjunction_pool(bounds: int, premises: int) -> tuple[list[SmtFormula], SmtFormula]:
    """`real_chain(bounds + 1)`'s bounds packed into `premises` conjunctions; claim `true`.

    The check a premise step makes after `premises` steps that each state many Real
    bounds (or a fact list in one formula). `premises` <= 100 takes SG27's one-call path.
    """
    chain_premises, claim = real_chain(bounds + 1)
    size = -(-len(chain_premises) // premises)
    pool = [
        SmtFormula("", "(and " + " ".join(p.assertion for p in chain_premises[i : i + size]) + ")")
        for i in range(0, len(chain_premises), size)
    ]
    return pool, SmtFormula(claim.declarations, "true")


def long_pool_with_one_wide_premise(
    bounds: int, premises: int
) -> tuple[list[SmtFormula], SmtFormula]:
    """`premises` - 1 Boolean premises plus one premise holding `bounds` Real bounds."""
    wide, claim = conjunction_pool(bounds, 1)
    bools, bool_claim = bool_chain(premises - 1, claim="true")
    decls = f"{bool_claim.declarations}\n{claim.declarations}"
    return [*bools, *wide], SmtFormula(decls, "true")


def untimed_s(premises: list[SmtFormula], claim: SmtFormula) -> float:
    """Wall s of one check with a timeout it never reaches."""
    return timed_check(premises, claim, 300.0)[1]


async def freed_after_cancel_s(
    premises: list[SmtFormula], claim: SmtFormula, after_s: float
) -> float:
    """Seconds from cancelling the check `after_s` in until its worker is free."""
    freed, _, _ = await cancel_release_s(premises, claim, after_s=after_s)
    return freed


# --- #5: a cancelled slow check still holds its thread -----------------------------------


async def easy_check_after_cancelling_slow_ones(
    premises: list[SmtFormula], claim: SmtFormula, *, after_s: float, workers: int = 1
) -> float:
    """Seconds an easy check takes right after `workers` slow checks were cancelled.

    Each cancelled check is one Z3 cannot interrupt once it is `after_s` in
    (`conjunction_pool`); each had gone slow, so its slot was free and the pool had
    room for a thread for the next caller.
    """
    ex = SolverExecutor(workers, slow_workers=workers)
    try:
        await ex.run(lambda: None)
        check = partial(Z3Solver(300.0).check_entailment, premises, claim)
        started = time.perf_counter()
        hogs = [asyncio.create_task(ex.run(check)) for _ in range(workers)]
        while ex._slow < workers:  # until every hog has given up its slot
            await asyncio.sleep(0.005)
        await asyncio.sleep(max(0.0, after_s - (time.perf_counter() - started)))
        for hog in hogs:
            hog.cancel()
        await asyncio.gather(*hogs, return_exceptions=True)
        started = time.perf_counter()
        await ex.run(partial(Z3Solver(300.0).check_entailment, [EASY], EASY))
        return time.perf_counter() - started
    finally:
        await asyncio.to_thread(ex.shutdown)


# --- #6: a new run's first check waits behind every short caller --------------------------


async def newcomer_wait_s(
    busy: int, check_s: float, duration_s: float, workers: int = 2
) -> tuple[float, float, int]:
    """A new run's first check while `busy` runs make back-to-back `check_s` checks.

    The busy runs keep going for `duration_s`. The newcomer is submitted once
    `workers + 1` busy runs have finished two checks each (so the slots are busy and
    a caller waits). Returns (newcomer's wait s, s the busy runs had left when it was
    submitted, busy runs that had not finished one check when the newcomer finished).
    The checks sleep, so they never go slow.
    """
    ex = SolverExecutor(workers, slow_workers=workers)
    done = [0] * busy
    warmed = asyncio.Event()
    stop = time.perf_counter() + duration_s

    async def run(i: int) -> None:
        while time.perf_counter() < stop:
            await ex.run(partial(time.sleep, check_s))
            done[i] += 1
            if sum(d >= 2 for d in done) > workers:
                warmed.set()

    try:
        tasks = [asyncio.create_task(run(i)) for i in range(busy)]
        await warmed.wait()
        left = stop - time.perf_counter()
        started = time.perf_counter()
        await asyncio.create_task(ex.run(lambda: None))  # a new task: a new run
        took = time.perf_counter() - started
        never = sum(d == 0 for d in done)
        await asyncio.gather(*tasks)
    finally:
        ex.shutdown()
    return took, left, never


# --- #7: CPU quota ---------------------------------------------------------------------------

CGROUP_CHILD: Final = """
import builtins, io, json, os, os.path, pathlib
QUOTA = {quota}
FILES = {{
    "/sys/fs/cgroup/cpu.max": f"{{QUOTA * 100000}} 100000\\n",
    "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": f"{{QUOTA * 100000}}\\n",
    "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000\\n",
    "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us": f"{{QUOTA * 100000}}\\n",
    "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us": "100000\\n",
    "/proc/self/cgroup": "0::/\\n",
}}
real_open, real_exists, real_isfile = builtins.open, os.path.exists, os.path.isfile
def fake_open(file, mode="r", *args, **kwargs):
    path = os.fspath(file) if isinstance(file, (str, bytes, os.PathLike)) else None
    if isinstance(path, str) and path in FILES:
        text = FILES[path]
        return io.BytesIO(text.encode()) if "b" in mode else io.StringIO(text)
    return real_open(file, mode, *args, **kwargs)
builtins.open = io.open = fake_open
os.path.exists = lambda p: os.fspath(p) in FILES or real_exists(p)
os.path.isfile = lambda p: os.fspath(p) in FILES or real_isfile(p)
real_pexists, real_pisfile = pathlib.Path.exists, pathlib.Path.is_file
pathlib.Path.exists = lambda self, *a, **k: str(self) in FILES or real_pexists(self, *a, **k)
pathlib.Path.is_file = lambda self, *a, **k: str(self) in FILES or real_pisfile(self, *a, **k)
from autolab.verification import solver as solver_mod
print(json.dumps({{"cpus": os.cpu_count(), "affinity": len(os.sched_getaffinity(0)),
                  "workers": solver_mod.SOLVER_WORKERS, "slow": solver_mod.SOLVER_SLOW_WORKERS}}))
"""


def sizes_under_quota(quota_cpus: int) -> dict[str, int]:
    """The solver's default sizes in a child whose cgroup files allow `quota_cpus` CPUs.

    Both cgroup v2 (`cpu.max`) and v1 (`cpu.cfs_quota_us`) files are served through
    `open`, `os.path` and `pathlib`; the host's CPUs and affinity are left as they are.
    """
    proc = subprocess.run(
        [sys.executable, "-c", CGROUP_CHILD.format(quota=quota_cpus)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    report: dict[str, int] = json.loads(proc.stdout.strip().splitlines()[-1])
    return report


# --- #8: the independent estimate after the whole verify ---------------------------------

HYPOTHESIS: Final = Hypothesis(question="Q?", statement="S", rationale="R")
DESIGN: Final = ExperimentDesign(
    hypothesis_id=HYPOTHESIS.id,
    protocol="Incubate the plate with the reagent kit and read absorbance",
    parameters=(ExperimentParameter(name="volume", value=50.0, unit="uL"),),
    estimated_cost_usd=5.0,
    estimated_duration_minutes=30.0,
    success_criteria=("absorbance rises",),
)


class SlowLLM(FakeLLMClient):
    """A `FakeLLMClient` that answers every call after `delay_s`, logging each purpose."""

    def __init__(self, delay_s: float) -> None:
        super().__init__()
        self.delay_s = delay_s
        self.log: list[LLMPurpose] = []
        self._inside = False

    async def _answer(self, purpose: LLMPurpose) -> bool:
        """Wait out one call's latency unless this is the fake's own inner call."""
        if self._inside:
            return False
        self.log.append(purpose)
        await asyncio.sleep(self.delay_s)
        self._inside = True
        return True

    async def call(self, purpose: LLMPurpose, prompt: str) -> str:
        outer = await self._answer(purpose)
        try:
            return await super().call(purpose, prompt)
        finally:
            self._inside = self._inside and not outer

    async def call_structured(
        self, purpose: LLMPurpose, prompt: str, schema: type[SchemaT]
    ) -> SchemaT:
        outer = await self._answer(purpose)
        try:
            return await super().call_structured(purpose, prompt, schema)
        finally:
            self._inside = self._inside and not outer


def design_llm(steps: int, delay_s: float, *, estimate_cost_usd: float) -> SlowLLM:
    """A Design agent's replies: a VALID `steps`-step trace and the given estimate."""
    fake = SlowLLM(delay_s)
    fake.register("design.experiment", build_design_prompt(HYPOTHESIS), DESIGN.model_dump_json())
    syms = [f"design_p{i}" for i in range(steps - 1)]
    decls = "\n".join(f"(declare-const {s} Bool)" for s in syms)
    trace_steps = [GoldenStep("the first premise holds", "premise", decls, syms[0])]
    trace_steps += [
        GoldenStep(f"premise {i} gives premise {i + 1}", "premise", "", f"(=> {a} {b})")
        for i, (a, b) in enumerate(itertools.pairwise(syms))
    ]
    trace_steps.append(
        GoldenStep(design_claim(DESIGN), "conclusion", "", syms[-1], tuple(range(steps - 1)))
    )
    register_trace(
        fake,
        agent="design",
        trace_purpose="design.trace",
        trace_prompt=build_design_trace_prompt(HYPOTHESIS, DESIGN),
        steps=trace_steps,
        facts=design_facts(DESIGN),
    )
    register_estimate(fake, DESIGN, cost_usd=estimate_cost_usd)
    return fake


async def design_attempt_s(
    steps: int, delay_s: float, *, estimate_cost_usd: float
) -> tuple[float, list[LLMPurpose], str]:
    """One Design attempt: (wall s, LLM purposes in call order, trigger of its request)."""
    fake = design_llm(steps, delay_s, estimate_cost_usd=estimate_cost_usd)
    agent = DesignAgent(verifier=Verifier(fake), llm=fake, hitl_trigger=cost_trigger(10.0, 60.0))
    started = time.perf_counter()
    update = await agent({**empty_state("Q?"), "hypotheses": [HYPOTHESIS]})
    took = time.perf_counter() - started
    pending = update.get("hitl_pending")
    return took, fake.log, "" if pending is None else str(pending.payload["trigger"])


# --- sections ------------------------------------------------------------------------------


async def section_probe() -> None:
    """#1: the bounded probe on wide declarations (parses are linear, their size is not)."""
    for arity in (16, 64, 256, 512, 1024):
        decl = int_function(arity)
        chars = chars_parsed_to_merge(decl, 1 << 62)
        print(
            f"probe arity {arity:5d}: decl {len(decl):6d} chars, parsed {chars:11d} chars "
            f"({chars / len(decl):7.0f}x), merge {merge_s(decl) * 1000:9.1f} ms"
        )
    for arity in (256, 1024):
        took, stall = await formalize_wide_s(arity)
        print(f"probe formalize arity {arity}: {took:6.2f} s, event-loop stall {stall:8.0f} ms")


async def section_gil() -> None:
    """#3: premise checks beside a Python thread, and an entailment that times out."""
    print(f"switch interval {sys.getswitchinterval() * 1000:.1f} ms")
    for n in (100, 101, 200, 400, 800):
        quiet, busy = gil_overhead_s(n)
        print(
            f"gil bool_chain({n}) premise check: quiet {quiet * 1000:7.1f} ms, beside Python "
            f"{busy * 1000:8.1f} ms ({busy / quiet:5.1f}x)"
        )
    for n in (400, 800):
        verdict, busy_reason, alone_s, timeout_s = entailment_beside_python(n, 20.0)
        print(
            f"gil bool_chain({n}) entailment: quiet {verdict} in {alone_s * 1000:.0f} ms; timeout "
            f"{timeout_s:.2f} s beside Python: {busy_reason}"
        )


async def section_concurrent() -> None:
    """#3: step p95 of concurrent long uncited traces, as built and with one-call loading."""
    makers: dict[str, Callable[[int], GoldenTrace]] = {
        "bool": lambda n: chain(n, cite=False),
        "lia": lia_trace,
        "lra": lra_trace,
    }
    for name, make in makers.items():
        for n, k in ((200, 1), (200, 4), (400, 1), (400, 4)):
            wall, p95, bad = await step_p95(make, n, k)
            with one_call_loading():
                old_wall, old_p95, old_bad = await step_p95(make, n, k)
            print(
                f"concurrent {name} {k} x {n}: p95 {p95:6.1f} ms (one call {old_p95:6.1f} ms), "
                f"wall {wall:6.2f} s ({old_wall:6.2f} s), non-VALID {bad}/{old_bad}"
            )


async def section_unpolled() -> None:
    """#4: timeouts and cancels on pools of few, long premises."""
    shapes = {
        "1 x 1000 bounds": conjunction_pool(1000, 1),
        "50 x 20 bounds": conjunction_pool(1000, 50),
        "100 x 16 bounds": conjunction_pool(1600, 100),
        "150 premises, one of 1000 bounds": long_pool_with_one_wide_premise(1000, 150),
    }
    for name, (premises, claim) in shapes.items():
        alone = untimed_s(premises, claim)
        for timeout_s in (0.5, alone / 5):
            reason, wall = timed_check(premises, claim, timeout_s)
            print(
                f"unpolled {name}: alone {alone:5.2f} s; timeout {timeout_s:4.2f} s -> {reason} "
                f"after {wall:5.2f} s ({wall / timeout_s:4.1f}x)"
            )
        freed = await freed_after_cancel_s(premises, claim, alone / 10)
        print(f"unpolled {name}: cancelled at {alone / 10:.2f} s, worker free after {freed:.2f} s")


async def section_cancel() -> None:
    """#5: easy checks right after cancelling slow checks Z3 cannot interrupt."""
    premises, claim = conjunction_pool(1000, 50)
    alone = untimed_s(premises, claim)
    for workers in (1, 2):
        took = await easy_check_after_cancelling_slow_ones(
            premises, claim, after_s=alone / 10, workers=workers
        )
        print(
            f"cancel {workers} worker(s): easy check {took:5.2f} s (hard check alone {alone:.2f} s)"
        )


async def section_newcomer() -> None:
    """#6: a new run's first check beside runs whose checks stay within their slice."""
    for busy, check_s in ((3, 0.02), (6, 0.02), (6, 0.04), (12, 0.01)):
        took, left, never = await newcomer_wait_s(busy, check_s, 3.0)
        print(
            f"newcomer beside {busy} runs of {check_s * 1000:.0f} ms checks on 2 slots: waited "
            f"{took:5.2f} s (busy runs had {left:4.2f} s left); {never} busy runs had no check yet"
        )


async def section_cgroup() -> None:
    """#7: default sizes under a CPU quota."""
    for quota in (1, 2):
        print(f"cgroup quota {quota} CPU(s): {sizes_under_quota(quota)}")


async def section_estimate() -> None:
    """#8: a Design attempt whose independent estimate disagrees, and one that agrees."""
    for steps, delay_s in ((10, 0.1), (20, 0.1)):
        for cost, label in ((500.0, "disagrees"), (5.0, "agrees")):
            took, log, trigger = await design_attempt_s(steps, delay_s, estimate_cost_usd=cost)
            print(
                f"estimate {label}: {steps} steps, {delay_s * 1000:.0f} ms per call: "
                f"{took:5.2f} s ({took / delay_s:4.1f} calls' time), {len(log)} calls, "
                f"estimate is call {log.index('design.estimate') + 1}, trigger {trigger!r}"
            )


SECTIONS: Final = {
    "probe": section_probe,
    "gil": section_gil,
    "concurrent": section_concurrent,
    "unpolled": section_unpolled,
    "cancel": section_cancel,
    "newcomer": section_newcomer,
    "cgroup": section_cgroup,
    "estimate": section_estimate,
}


async def amain(argv: Sequence[str] | None = None) -> int:
    """Run the selected sections (all by default)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("sections", nargs="*", choices=[*SECTIONS, []], default=[])
    args = parser.parse_args(argv)
    print(
        f"machine: {os.cpu_count()} CPUs, workers={solver_mod.SOLVER_WORKERS}, "
        f"slow_workers={solver_mod.SOLVER_SLOW_WORKERS}, load {os.getloadavg()[0]:.1f}",
        flush=True,
    )
    for name in args.sections or list(SECTIONS):
        await SECTIONS[name]()
        sys.stdout.flush()
    return 0


def main() -> None:
    """CLI entry point."""
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()


__all__ = [
    "chars_parsed_to_merge",
    "conjunction_pool",
    "design_attempt_s",
    "easy_check_after_cancelling_slow_ones",
    "entailment_beside_python",
    "formalize_wide_s",
    "freed_after_cancel_s",
    "gil_overhead_s",
    "long_pool_with_one_wide_premise",
    "newcomer_wait_s",
    "one_call_loading",
    "premise_check",
    "python_thread",
    "sizes_under_quota",
    "step_p95",
    "untimed_s",
]
