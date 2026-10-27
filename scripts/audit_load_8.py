"""AUDIT-30 load measurements, round 8 (FakeLLMClient / stubs only; no network).

Each section prints the numbers quoted in `AUDIT-30.md`. The repro tests in
`tests/audit/test_audit_30.py` reuse the helpers defined here.

    uv run python -m scripts.audit_load_8                   # every section
    uv run python -m scripts.audit_load_8 together fresh
    uv run python -m scripts.audit_load_8 --burn 4 vocab    # with 4 CPU burners

Sections: together, fresh, graph, sg32, starve, probe, vocab, cold, fork, ipc.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from typing import Any, Final, TypedDict

import z3

from autolab.verification import formalizer as formalizer_mod
from autolab.verification.formalizer import FormalizationError, Formalizer, Vocabulary
from autolab.verification.solver import SolverExecutor, Z3Solver
from autolab.verification.vericot import Verifier
from scripts.audit_load import chain
from scripts.audit_load_2 import slow_check
from scripts.audit_load_4 import slow_verify
from scripts.audit_load_7 import EASY, burners, hard_check_s, newcomer_first_check_s, run_child
from tests.fixtures.golden_traces import build


async def _easy_newcomer_s(ex: SolverExecutor) -> float:
    """Wall s of one easy check made in a task that has never checked."""
    check = partial(Z3Solver().check_entailment, *EASY)

    async def newcomer() -> float:
        started = time.perf_counter()
        await ex.run(check)
        return time.perf_counter() - started

    return await asyncio.create_task(newcomer())


async def _stop(tasks: list[asyncio.Task[None]], ex: SolverExecutor) -> None:
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    ex.shutdown()


# --- #1: runs that start together, and runs that check in fresh tasks ---------------------


async def newcomer_behind_together_s(hogs: int, timeout_s: float) -> tuple[float, float]:
    """(s a new task's easy check waits, one warm hard check's s) when it arrives
    `timeout_s / 4` after `hogs` hard runs started together.

    This is the AUDIT-26 #1 load as round 7 wrote it, before SG32 changed the
    harness to wait until every hard run had completed a check.
    """
    ex = SolverExecutor(2, 2)
    tasks: list[asyncio.Task[None]] = []
    try:
        unit = min([await asyncio.create_task(hard_check_s(ex, timeout_s)) for _ in range(2)])
        tasks = [asyncio.create_task(slow_verify(ex, timeout_s, 1000)) for _ in range(hogs)]
        await asyncio.sleep(timeout_s / 4)
        waited = await _easy_newcomer_s(ex)
    finally:
        await _stop(tasks, ex)
    return waited, unit


async def newcomer_beside_fresh_task_runs_s(
    hogs: int, timeout_s: float, per_task: int = 1, probes: int = 3
) -> tuple[list[float], float]:
    """(s each of `probes` new tasks' easy checks waits, one warm hard check's s) in the
    steady state of `hogs` hard runs that make `per_task` checks per fresh task.

    A LangGraph node runs in a task of its own, so each agent's verify is a fresh
    task: its first check is a newcomer however hard its run has been.
    """
    ex = SolverExecutor(2, 2)
    stop = [False]
    tasks: list[asyncio.Task[None]] = []

    async def one_verify() -> None:
        for _ in range(per_task):
            await slow_check(ex, timeout_s)

    async def hog() -> None:
        while not stop[0]:
            await asyncio.create_task(one_verify())

    try:
        unit = min([await asyncio.create_task(hard_check_s(ex, timeout_s)) for _ in range(2)])
        tasks = [asyncio.create_task(hog()) for _ in range(hogs)]
        await asyncio.sleep(max(3, per_task * hogs / 4) * timeout_s)  # past the start-up
        waits = []
        for _ in range(probes):
            waits.append(await _easy_newcomer_s(ex))
            await asyncio.sleep(timeout_s / 3)
    finally:
        stop[0] = True
        await _stop(tasks, ex)
    return waits, unit


async def easy_verify_beside_together_s(hogs: int, timeout_s: float) -> tuple[float, float]:
    """(10-step easy verify alone, in a new task 50 ms after `hogs` hard runs started)."""
    ex = SolverExecutor(2, 2)
    tasks: list[asyncio.Task[None]] = []

    async def verify() -> float:
        trace, fake = build(chain(10, cite=True))
        started = time.perf_counter()
        await Verifier(fake, solver_executor=ex).verify(trace)
        return time.perf_counter() - started

    try:
        await asyncio.create_task(verify())
        alone = await asyncio.create_task(verify())
        tasks = [asyncio.create_task(slow_verify(ex, timeout_s, 1000)) for _ in range(hogs)]
        await asyncio.sleep(0.05)
        loaded = await asyncio.create_task(verify())
    finally:
        await _stop(tasks, ex)
    return alone, loaded


class _Count(TypedDict):
    n: int


async def graph_node_tasks(nodes: int = 6) -> tuple[int, int]:
    """(distinct asyncio tasks, node runs + 1) over one LangGraph run: each node's own task?"""
    from langgraph.graph import END, START, StateGraph

    seen: list[asyncio.Task[Any] | None] = [asyncio.current_task()]

    async def node(s: _Count) -> dict[str, int]:
        seen.append(asyncio.current_task())
        return {"n": s["n"] + 1}

    g: Any = StateGraph(_Count)
    g.add_node("node", node)
    g.add_edge(START, "node")
    g.add_conditional_edges("node", lambda s: "node" if s["n"] < nodes else END)
    await g.compile().ainvoke({"n": 0})
    return len({id(t) for t in seen}), len(seen)


# --- starvation of known-hard runs by a stream of new tasks ------------------------------


async def hard_runs_under_stream(
    hard_runs: int, rate_hz: float, kind: str, timeout_s: float, duration_s: float
) -> tuple[int, float, float]:
    """(hard checks completed in `duration_s`, warm hard check s, longest hard wait s)
    while new tasks arrive at `rate_hz`, each making one `kind` ("easy"/"hard") check."""
    ex = SolverExecutor(2, 2)
    done = [0]
    waits: list[float] = []
    stop = [False]
    tasks: list[asyncio.Task[None]] = []
    easy = partial(Z3Solver().check_entailment, *EASY)

    async def hog() -> None:
        await slow_check(ex, timeout_s)  # this task is now hard
        while not stop[0]:
            started = time.perf_counter()
            await slow_check(ex, timeout_s)
            waits.append(time.perf_counter() - started - timeout_s)
            done[0] += 1

    async def newcomer() -> None:
        if kind == "easy":
            await ex.run(easy)
        else:
            await slow_check(ex, timeout_s)

    async def stream() -> None:
        while not stop[0]:
            tasks.append(asyncio.create_task(newcomer()))
            await asyncio.sleep(1 / rate_hz)

    try:
        unit = min([await asyncio.create_task(hard_check_s(ex, timeout_s)) for _ in range(2)])
        tasks.extend(asyncio.create_task(hog()) for _ in range(hard_runs))
        await asyncio.sleep(2 * timeout_s)
        base, waits[:] = done[0], []
        if rate_hz:
            tasks.append(asyncio.create_task(stream()))
        await asyncio.sleep(duration_s)
        got = done[0] - base
        longest = max(waits, default=0.0)
    finally:
        stop[0] = True
        await _stop(tasks, ex)
    return got, unit, longest


# --- SG33: the worst admitted reply ------------------------------------------------------

REPLY_SHAPES: Final[dict[str, Callable[[int], str]]] = {
    "constants": lambda n: "\n".join(f"(declare-const c{i} Int)" for i in range(n)),
    "1-ary Int functions": lambda n: "\n".join(f"(declare-fun f{i} (Int) Bool)" for i in range(n)),
    "4-ary Int functions": lambda n: "\n".join(
        f"(declare-fun f{i} (Int Int Int Int) Bool)" for i in range(n)
    ),
    "8-ary Int functions": lambda n: "\n".join(
        f"(declare-fun f{i} ({' '.join(['Int'] * 8)}) Bool)" for i in range(n)
    ),
    "functions over a sort": lambda n: (
        "(declare-sort S 0)\n" + "\n".join(f"(declare-fun g{i} (S S) S)" for i in range(n))
    ),
    "1-ary sorts": lambda n: "\n".join(f"(declare-sort S{i} 1)" for i in range(n)),
}


def _clear_probe_caches() -> None:
    formalizer_mod._applied_builtin.cache_clear()
    formalizer_mod._is_z3_builtin.cache_clear()


def largest_admitted(make: Callable[[int], str], hi: int = 5000) -> int:
    """The most declarations of shape `make` that one reply may carry (SG33's budget)."""
    lo, best = 1, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        _clear_probe_caches()
        try:
            Vocabulary().merged(make(mid))
        except FormalizationError as exc:
            if "builtin probes" not in str(exc):
                raise
            hi = mid - 1
        else:
            best, lo = mid, mid + 1
    return best


def merged_s(vocab: Vocabulary, decls: str) -> float:
    """Wall s of `vocab.merged(decls)` with the probe caches cleared (on the event loop)."""
    _clear_probe_caches()
    started = time.perf_counter()
    vocab.merged(decls)
    return time.perf_counter() - started


# --- #3: loop work that grows with the vocabulary -------------------------------------------


def constants(n: int, prefix: str) -> str:
    """`n` Int constant declarations, one per line."""
    return "\n".join(f"(declare-const {prefix}{i} Int)" for i in range(n))


def merge_into_vocab_s(vocab_size: int, reply_size: int = 100) -> float:
    """Best of 3 walls of merging a `reply_size`-constant reply into a vocabulary of
    `vocab_size` symbols: the reply is the same, only what it is merged into grows."""
    vocab = Vocabulary({f"v{i}": f"(declare-const v{i} Int)" for i in range(vocab_size)})
    reply = constants(reply_size, "r")
    return min(merged_s(vocab, reply) for _ in range(3))


def facts_merge_s(n: int) -> float:
    """Wall s of merging `n` one-symbol facts the way `Verifier.verify` does, one at a time."""
    _clear_probe_caches()
    formalizer = Formalizer(llm=_Scripted([]))  # no LLM call is made
    started = time.perf_counter()
    for i in range(n):
        formalizer.vocabulary.merge(f"(declare-const m{i} Real)")
    return time.perf_counter() - started


class _Scripted:
    """An LLM that returns the given formalizations in order."""

    def __init__(self, replies: list[Any]) -> None:
        self._replies = replies

    async def call(self, purpose: str, prompt: str) -> str:
        raise NotImplementedError

    async def call_structured(self, purpose: str, prompt: str, schema: type[Any]) -> Any:
        return self._replies.pop(0)


async def trace_growth_ms(steps: int, per_step: int) -> list[float]:
    """Per-step ms of `Formalizer.formalize` (on the loop) when every step's reply
    declares `per_step` new constants, all admitted by SG33's budget."""
    from autolab.state.models import FormalizedStep, ReasoningStep

    replies = [
        FormalizedStep(declarations=constants(per_step, f"c{i}_"), assertion=f"(> c{i}_0 0)")
        for i in range(steps)
    ]
    formalizer = Formalizer(_Scripted(replies))
    out = []
    for i in range(steps):
        step = ReasoningStep(index=i, claim=f"claim {i}", claim_type="premise")
        started = time.perf_counter()
        await formalizer.formalize(step)
        out.append((time.perf_counter() - started) * 1e3)
    return out


# --- #4: the first check's start-up wait --------------------------------------------------

COLD_CHILD: Final = """
import asyncio, json, time
from autolab.verification import solver as solver_mod
from autolab.verification.vericot import Verifier
from scripts.audit_load import LatencyLLM, chain
from tests.fixtures.golden_traces import build

walls = []
_run = solver_mod.SolverExecutor.run
async def run(self, fn):
    started = time.perf_counter()
    out = await _run(self, fn)
    walls.append(time.perf_counter() - started)
    return out
solver_mod.SolverExecutor.run = run

async def main():
    trace, fake = build(chain(STEPS, cite=True))
    llm = LatencyLLM(fake, DELAY) if DELAY else fake
    result = await Verifier(llm).verify(trace)
    first = result.step_verdicts[0].model_dump()
    recorded_ms = sum(v for k, v in first.items() if k.endswith("_ms") and isinstance(v, float))
    print(json.dumps({"first_check_wall_ms": walls[0] * 1e3, "solver_ms": first["solver_ms"],
                      "recorded_ms": recorded_ms, "fields": sorted(first)}))

if __name__ == "__main__":
    asyncio.run(main())
"""


def cold_first_check(delay_s: float = 0.0, steps: int = 2) -> dict[str, Any]:
    """A fresh process builds a `Verifier` and verifies at once: the first check's wall
    against what its `StepVerdict` records (`solver_ms` and any other `*_ms` field)."""
    out = run_child(f"DELAY = {delay_s!r}\nSTEPS = {steps}\n" + COLD_CHILD)
    parsed: dict[str, Any] = json.loads(out.strip().splitlines()[-1])
    return parsed


# --- #2: a fork after a Verifier is built ---------------------------------------------------

FORK_CHILD: Final = """
import os, time
from autolab.verification import solver as solver_mod
from autolab.verification.llm import FakeLLMClient
from autolab.verification.solver import SmtFormula, Z3Solver
from autolab.verification.vericot import Verifier

if __name__ == "__main__":
    Verifier(FakeLLMClient())  # e.g. build_graph() at import, before a prefork server forks
    deadline = time.monotonic() + 10
    while solver_mod._workers().live() < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read)
        lines = []
        for _ in range(CHECKS):
            try:
                out = Z3Solver(timeout_seconds=1.0).check_entailment(
                    [SmtFormula("(declare-const x Int)", "(> x 0)")],
                    SmtFormula("(declare-const x Int)", "(> x -1)"),
                )
                lines.append(f"{out.verdict} {out.reason}")
            except Exception as exc:
                lines.append(f"raised {type(exc).__name__}: {exc}")
        os.write(write, "\\n".join(lines).encode())
        os._exit(0)
    os.close(write)
    os.waitpid(pid, 0)
    with os.fdopen(read) as f:
        print("CHILD " + f.read().replace("\\n", "|"))
"""


def checks_in_forked_child(checks: int = 6) -> list[str]:
    """Outcomes of `checks` easy checks in a child forked after a `Verifier` was built."""
    out = run_child(f"CHECKS = {checks}\n" + FORK_CHILD)
    line = next(ln for ln in out.splitlines() if ln.startswith("CHILD "))
    return line[len("CHILD ") :].split("|")


# --- SG31: the cost of a tagged reply ------------------------------------------------------


def ipc_overhead_ms(runs: int = 300) -> tuple[float, float]:
    """(median ms of an easy check in a worker, the same check in-process)."""
    z = Z3Solver()
    z.check_entailment(*EASY)
    worker, here = [], []
    for _ in range(runs):
        started = time.perf_counter()
        z.check_entailment(*EASY)
        worker.append(time.perf_counter() - started)
        started = time.perf_counter()
        z._check_here(*EASY)
        here.append(time.perf_counter() - started)
    return statistics.median(worker) * 1e3, statistics.median(here) * 1e3


# --- sections -----------------------------------------------------------------------------


async def section_together() -> None:
    for hogs in (8, 12, 16):
        waited, unit = await newcomer_behind_together_s(hogs, 0.4)
        print(f"together: {hogs} hard runs, newcomer waited {waited:.2f} s = {waited / unit:.1f}")
    alone, loaded = await easy_verify_beside_together_s(16, 5.0)
    print(
        f"together: 10-step easy verify, 16 hard runs at 5 s: {alone:.2f} s alone, {loaded:.2f} s"
    )


async def section_fresh() -> None:
    for hogs in (8, 12, 16):
        waits, unit = await newcomer_beside_fresh_task_runs_s(hogs, 0.4)
        print(f"fresh: {hogs} runs, 1 check/task: {' '.join(f'{w / unit:.1f}' for w in waits)}")
    for per_task in (3, 10):
        waits, unit = await newcomer_beside_fresh_task_runs_s(12, 0.4, per_task)
        units = " ".join(f"{w / unit:.1f}" for w in waits)
        print(f"fresh: 12 runs, {per_task} checks/task: {units} hard checks")


async def section_graph() -> None:
    distinct, runs = await graph_node_tasks()
    print(f"graph: {distinct} distinct tasks over {runs} (main + node runs)")


async def section_sg32() -> None:
    for hogs in (8, 12, 16):
        waited, unit = await newcomer_first_check_s(hogs, 0.4)
        print(f"sg32 harness: {hogs} hard runs, newcomer {waited / unit:.1f} hard checks")


async def section_starve() -> None:
    for hard_runs in (4, 8):
        for rate, kind in ((0.0, "easy"), (20.0, "easy"), (100.0, "easy"), (5.0, "hard")):
            got, unit, longest = await hard_runs_under_stream(hard_runs, rate, kind, 0.4, 8.0)
            print(
                f"starve: {hard_runs} hard runs, {rate:.0f}/s {kind} newcomers: {got} hard "
                f"checks in 8 s, longest wait {longest:.2f} s = {longest / unit:.1f}"
            )


async def section_probe() -> None:
    for name, make in REPLY_SHAPES.items():
        n = largest_admitted(make)
        decls = make(n)
        took = min(merged_s(Vocabulary(), decls) for _ in range(3))
        print(f"probe: {name}: {n} admitted, {len(decls) / 1024:.1f} KB, {took * 1e3:.0f} ms")
    for depth in (1000, 4000):
        nested = "(not " * depth + "a" + ")" * depth
        started = time.perf_counter()
        z3.parse_smt2_string(f"(declare-const a Bool)(assert {nested})", ctx=z3.Context())
        took = time.perf_counter() - started
        print(f"probe: assertion of {depth} nested nots: {took * 1e3:.0f} ms")


async def section_vocab() -> None:
    for size in (0, 5000, 10000, 20000):
        took = merge_into_vocab_s(size)
        print(f"vocab: 100-constant reply into {size} symbols: {took * 1e3:.0f} ms")
    for n in (750, 1500, 3000, 6000):
        print(f"vocab: {n} facts merged one at a time: {facts_merge_s(n) * 1e3:.0f} ms")
    for steps, per in ((50, 110), (100, 110), (400, 10)):
        ms = await trace_growth_ms(steps, per)
        print(
            f"vocab: {steps} steps x {per} constants: step 1 {ms[0]:.0f} ms, last {ms[-1]:.0f} ms,"
            f" max {max(ms):.0f} ms, total {sum(ms) / 1e3:.2f} s"
        )


async def section_cold() -> None:
    for delay in (0.0, 0.0, 0.0, 0.1):
        r = cold_first_check(delay)
        print(
            f"cold (LLM {delay * 1e3:.0f} ms): first check {r['first_check_wall_ms']:.0f} ms, "
            f"recorded {r['recorded_ms']:.0f} ms ({', '.join(r['fields'])})"
        )


async def section_fork() -> None:
    print("fork: " + " | ".join(checks_in_forked_child()))


async def section_ipc() -> None:
    worker, here = ipc_overhead_ms()
    print(f"ipc: easy check {worker:.2f} ms in a worker, {here:.2f} ms in-process")


SECTIONS: Final[dict[str, Callable[[], Awaitable[None]]]] = {
    "together": section_together,
    "fresh": section_fresh,
    "graph": section_graph,
    "sg32": section_sg32,
    "starve": section_starve,
    "probe": section_probe,
    "vocab": section_vocab,
    "cold": section_cold,
    "fork": section_fork,
    "ipc": section_ipc,
}


async def amain(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sections", nargs="*", choices=[*SECTIONS, []])
    parser.add_argument("--burn", type=int, default=0, help="CPU burners to run meanwhile")
    args = parser.parse_args(argv)
    with burners(args.burn):
        for name in args.sections or SECTIONS:
            print(f"[{name}] load {os.getloadavg()[0]:.2f}", flush=True)
            await SECTIONS[name]()
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
