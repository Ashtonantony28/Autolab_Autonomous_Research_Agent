"""AUDIT-34 load measurements, round 9 (stubs and Z3 only; no network, no LLM).

Each section prints the numbers quoted in `AUDIT-34.md`. The repro tests in
`tests/audit/test_audit_34.py` reuse the helpers defined here. Every helper uses only
the public `SolverExecutor` / `Z3Solver` API, so the same file runs against an older
tree (`PYTHONPATH=<tree>/src:<tree>`) for version comparisons.

    uv run python -m scripts.audit_load_9                     # every section
    uv run python -m scripts.audit_load_9 requeue waste
    uv run python -m scripts.audit_load_9 --burn 4 requeue    # with 4 CPU burners

Sections: requeue, waste, stream, decided, fork.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from functools import partial
from typing import Any, Final

from autolab.verification import solver_workers
from autolab.verification.solver import SmtFormula, SolverExecutor, SolverOutcome, Z3Solver
from scripts.audit_load import NO_CUBES, POSITIVE, XYZ
from scripts.audit_load_2 import slow_check
from scripts.audit_load_7 import EASY, burners, hard_check_s, run_child

UNDECIDABLE: Final = ([SmtFormula(XYZ, POSITIVE)], SmtFormula(XYZ, NO_CUBES))
"""A check that runs until its timeout."""


def php(holes: int) -> tuple[list[SmtFormula], SmtFormula]:
    """Pigeonhole: `holes + 1` pigeons in `holes` holes. Decidable (premises inconsistent);
    8 holes take about 0.3 s alone on the audit machine, a little over `_HARD_SLICES`."""
    pigeons = holes + 1
    names = [[f"p{i}_{j}" for j in range(holes)] for i in range(pigeons)]
    decl = "\n".join(f"(declare-const {n} Bool)" for row in names for n in row)
    each = " ".join("(or " + " ".join(row) + ")" for row in names)
    excl = " ".join(
        f"(not (and {names[i][j]} {names[k][j]}))"
        for j in range(holes)
        for i in range(pigeons)
        for k in range(i + 1, pigeons)
    )
    return [SmtFormula(decl, f"(and {each} {excl})")], SmtFormula(decl, "false")


async def _stop(tasks: Sequence[asyncio.Task[Any]], ex: SolverExecutor) -> None:
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    ex.shutdown()


async def _in_new_task(ex: SolverExecutor, check: Callable[[], SolverOutcome]) -> float:
    """Wall s of one check made in a task that has never checked."""

    async def newcomer() -> float:
        started = time.perf_counter()
        await ex.run(check)
        return time.perf_counter() - started

    return await asyncio.create_task(newcomer())


# --- #1: a newcomer stopped for a short caller goes to the back of the hard queue ----------


async def stopped_newcomer_wait_s(
    hogs: int,
    shorts: int,
    timeout_s: float,
    probe_s: float,
    probes: int = 3,
    calls: list[int] | None = None,
) -> tuple[list[float], float]:
    """(wall s of each probe, one warm hard check's s); `calls` gets how many times each
    probe's check was started (more than 1: it was stopped and re-run).

    2 slots + 2 slow places. `hogs` hard runs make back-to-back runs-to-timeout checks
    in long-lived tasks (known hard, so they queue at the hard rank), and `shorts` easy
    runs make back-to-back easy checks (short callers, which keep the lent places busy
    and are often waiting). Each probe is a new task making one check that runs
    `probe_s` (a runs-to-timeout check with that timeout): longer than one slice,
    shorter than `_HARD_SLICES`, so it is not hard by the executor's own measure.
    """
    ex = SolverExecutor(2, 2)
    stop = [False]
    tasks: list[asyncio.Task[Any]] = []
    easy = partial(Z3Solver().check_entailment, *EASY)

    async def hog() -> None:
        while not stop[0]:
            await slow_check(ex, timeout_s)

    async def short() -> None:
        while not stop[0]:
            await ex.run(easy)

    started = [0]
    inner = partial(Z3Solver(probe_s).check_entailment, *UNDECIDABLE)

    def probe() -> SolverOutcome:
        started[0] += 1
        return inner()

    try:
        unit = min([await asyncio.create_task(hard_check_s(ex, timeout_s)) for _ in range(2)])
        tasks = [asyncio.create_task(hog()) for _ in range(hogs)]
        tasks += [asyncio.create_task(short()) for _ in range(shorts)]
        await asyncio.sleep(3 * timeout_s)  # past the start-up: every hog is known hard
        waits = []
        for _ in range(probes):
            started[0] = 0
            waits.append(await _in_new_task(ex, probe))
            if calls is not None:
                calls.append(started[0])
            await asyncio.sleep(timeout_s / 3)
    finally:
        stop[0] = True
        await _stop(tasks, ex)
    return waits, unit


# --- #2: stopped-and-rerun checks under a LangGraph-like load ------------------------------


class Meter:
    """Counts Z3 calls in worker processes, their concurrency, and worker restarts."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.area = 0.0  # integral of `running` over time
        self.last = time.perf_counter()
        self.restarts = 0

    def _tick(self, delta: int) -> None:
        now = time.perf_counter()
        self.area += self.running * (now - self.last)
        self.last = now
        self.running += delta
        self.peak = max(self.peak, self.running)

    def reset(self) -> float:
        with self.lock:
            self._tick(0)
            self.area, self.peak = 0.0, self.running
            return self.last

    def mean(self, since: float) -> float:
        with self.lock:
            self._tick(0)
            return self.area / max(1e-9, self.last - since)


@contextmanager
def metered() -> Iterator[Meter]:
    """Patch `Worker.call` and `Workers.discard` to feed a `Meter` for the duration."""
    meter = Meter()
    call, discard = solver_workers.Worker.call, solver_workers.Workers.discard

    def counted_call(self: Any, args: Any, budget_s: float) -> Any:
        with meter.lock:
            meter._tick(1)
        try:
            return call(self, args, budget_s)
        finally:
            with meter.lock:
                meter._tick(-1)

    def counted_discard(self: Any, worker: Any) -> None:
        with meter.lock:
            meter.restarts += 1
        discard(self, worker)

    solver_workers.Worker.call = counted_call  # type: ignore[method-assign]
    solver_workers.Workers.discard = counted_discard  # type: ignore[method-assign]
    try:
        yield meter
    finally:
        solver_workers.Worker.call = call  # type: ignore[method-assign]
        solver_workers.Workers.discard = discard  # type: ignore[method-assign]


class FreshTaskLoad(dict[str, float]):
    """What `fresh_task_load` measured."""


async def fresh_task_load(
    runs: int,
    timeout_s: float,
    per_task: int,
    duration_s: float,
    check: Callable[[], SolverOutcome] | None = None,
) -> FreshTaskLoad:
    """`runs` runs, each making `per_task` checks per fresh task, as LangGraph runs each
    node and retry in a task of its own. 2 slots + 2 slow places.

    Every call of the check function is timed; a run whose function was called more than
    once was stopped and re-run, and its earlier calls' time is wasted.
    """
    ex = SolverExecutor(2, 2)
    stop = [False]
    done = [0]
    calls = [0]
    wasted = [0.0]
    decided = [0]
    latencies: list[float] = []
    lock = threading.Lock()
    inner = check or partial(Z3Solver(timeout_s).check_entailment, *UNDECIDABLE)

    async def one_check() -> None:
        spent: list[float] = []

        def timed() -> SolverOutcome:
            started = time.perf_counter()
            try:
                return inner()
            finally:
                spent.append(time.perf_counter() - started)

        began = time.perf_counter()
        outcome, _ = await ex.run(timed)
        with lock:
            latencies.append(time.perf_counter() - began)
            decided[0] += outcome.verdict != "UNGROUNDED"
            done[0] += 1
            calls[0] += len(spent)
            wasted[0] += sum(spent[:-1])

    async def one_node() -> None:
        for _ in range(per_task):
            await one_check()

    async def run() -> None:
        while not stop[0]:
            await asyncio.create_task(one_node())

    tasks: list[asyncio.Task[None]] = []
    with metered() as meter:
        try:
            tasks = [asyncio.create_task(run()) for _ in range(runs)]
            await asyncio.sleep(2 * timeout_s)
            with lock:
                done[0] = calls[0] = decided[0] = 0
                wasted[0] = 0.0
                latencies.clear()
            restarts = meter.restarts
            since = meter.reset()
            await asyncio.sleep(duration_s)
            with lock:
                out = FreshTaskLoad(
                    done=done[0],
                    reruns=calls[0] - done[0],
                    wasted_s=wasted[0],
                    restarts=meter.restarts - restarts,
                    peak=meter.peak,
                    mean=meter.mean(since),
                    decided=decided[0],
                    p50_s=statistics.median(latencies) if latencies else 0.0,
                    max_s=max(latencies, default=0.0),
                )
        finally:
            stop[0] = True
            await _stop(tasks, ex)
    return out


# --- #3: newcomers in a stream of newcomers --------------------------------------------------


async def newcomer_in_stream_s(
    hogs: int, rate_hz: float, timeout_s: float, duration_s: float
) -> tuple[list[float], float, int]:
    """(every easy newcomer's wall s, warm hard check s, easy checks stopped and re-run).

    `hogs` known-hard runs fill every place, and new tasks arrive at `rate_hz`,
    alternating one runs-to-timeout check and one easy check (a LangGraph node's first
    check, hard or easy)."""
    ex = SolverExecutor(2, 2)
    stop = [False]
    tasks: list[asyncio.Task[Any]] = []
    waits: list[float] = []
    reruns = [0]
    easy = partial(Z3Solver().check_entailment, *EASY)

    async def hog() -> None:
        while not stop[0]:
            await slow_check(ex, timeout_s)

    async def easy_newcomer() -> None:
        started = time.perf_counter()
        calls = [0]

        def counted() -> SolverOutcome:
            calls[0] += 1
            return easy()

        await ex.run(counted)
        waits.append(time.perf_counter() - started)
        reruns[0] += calls[0] - 1

    async def stream() -> None:
        i = 0
        while not stop[0]:
            coro = easy_newcomer() if i % 2 else slow_check(ex, timeout_s)
            tasks.append(asyncio.create_task(coro))
            i += 1
            await asyncio.sleep(1 / rate_hz)

    try:
        unit = min([await asyncio.create_task(hard_check_s(ex, timeout_s)) for _ in range(2)])
        tasks = [asyncio.create_task(hog()) for _ in range(hogs)]
        await asyncio.sleep(3 * timeout_s)
        tasks.append(asyncio.create_task(stream()))
        await asyncio.sleep(duration_s)
        got = list(waits)
    finally:
        stop[0] = True
        await _stop(tasks, ex)
    return got, unit, reruns[0]


# --- #4: decidable checks that need a little over `_HARD_SLICES` -----------------------------


async def decidable_under_load(
    runs: int, timeout_s: float, duration_s: float, holes: int = 8
) -> tuple[float, FreshTaskLoad]:
    """(the check's solo wall s, fresh-task load of `runs` runs each deciding that check
    with `timeout_s`): a pigeonhole check that decides in a little over `_HARD_SLICES`."""
    check = partial(Z3Solver(timeout_s).check_entailment, *php(holes))
    ex = SolverExecutor(2, 2)
    try:
        await ex.run(check)
        solo = min([await _in_new_task(ex, check) for _ in range(3)])
    finally:
        ex.shutdown()
    out = await fresh_task_load(runs, timeout_s, 1, duration_s, check)
    return solo, out


# --- #5: fork adoption ----------------------------------------------------------------------

FORK_CHILD: Final = """
import json, os, time
from autolab.verification import solver as solver_mod
from autolab.verification.llm import FakeLLMClient
from autolab.verification.solver import SmtFormula, Z3Solver
from autolab.verification.vericot import Verifier

X = "(declare-const x Int)"
EASY = ([SmtFormula(X, "(> x 0)")], SmtFormula(X, "(> x -1)"))

def fds():
    return len(os.listdir("/proc/self/fd"))

def pss_kb(pid):
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except OSError:
        return 0
    return 0

def descendants(pid):
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        try:
            with open(f"/proc/{p}/task/{p}/children") as f:
                kids = [int(k) for k in f.read().split()]
        except OSError:
            kids = []
        out += kids
        todo += kids
    return out

def state(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return "gone"

if __name__ == "__main__":
    Verifier(FakeLLMClient())  # build_graph() at import, before a prefork server forks
    deadline = time.monotonic() + 10
    while solver_mod._workers().live() < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    parent_procs = descendants(os.getpid())
    report = {"parent_procs": len(parent_procs), "parent_fds_at_fork": fds(),
              "parent_pss_kb": sum(pss_kb(p) for p in parent_procs)}
    children = []
    for n in range(FORKS):
        read, write = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read)
            before = fds()
            time.sleep(IDLE)  # a prefork worker idles until its first request
            walls = []
            for _ in range(CHECKS):
                t = time.perf_counter()
                out = Z3Solver(1.0).check_entailment(*EASY)
                walls.append(round((time.perf_counter() - t) * 1e3, 1))
            time.sleep(0.3)
            mine = descendants(os.getpid())
            msg = {"verdict": out.verdict, "walls_ms": walls, "fds_at_fork": before,
                   "fds_after": fds(), "own_procs": mine,
                   "own_pss_kb": sum(pss_kb(p) for p in mine)}
            os.write(write, json.dumps(msg).encode())
            os.close(write)
            if EXIT == "_exit":
                os._exit(0)
            raise SystemExit(0)
        os.close(write)
        with os.fdopen(read) as f:
            children.append((pid, json.loads(f.read())))
    for pid, _ in children:
        os.waitpid(pid, 0)
    own = [p for _, c in children for p in c["own_procs"]]
    time.sleep(1.0)
    left_1s = sorted(state(p) for p in own if state(p) != "gone")
    time.sleep(4.0)
    left_5s = sorted(state(p) for p in own if state(p) != "gone")
    report.update({
        "children": [c for _, c in children],
        "own_procs_total": len(own),
        "left_1s": left_1s,
        "left_5s": left_5s,
        "parent_fds": len(os.listdir("/proc/self/fd")),
    })
    print("REPORT " + json.dumps(report))
"""


def fork_report(
    forks: int = 4, checks: int = 3, exit_how: str = "_exit", idle_s: float = 0.0
) -> dict[str, Any]:
    """Fork `forks` children after a `Verifier` is built; each idles `idle_s`, then makes
    `checks` easy checks."""
    head = f"FORKS = {forks}\nCHECKS = {checks}\nEXIT = {exit_how!r}\nIDLE = {idle_s!r}\n"
    out = run_child(head + FORK_CHILD)
    line = next(ln for ln in out.splitlines() if ln.startswith("REPORT "))
    parsed: dict[str, Any] = json.loads(line[len("REPORT ") :])
    return parsed


# --- sections -----------------------------------------------------------------------------


async def section_requeue() -> None:
    for hogs, shorts, probe_s in ((12, 3, 0.15), (16, 3, 0.15), (12, 0, 0.15), (12, 3, 0.01)):
        calls: list[int] = []
        waits, unit = await stopped_newcomer_wait_s(hogs, shorts, 0.4, probe_s, 5, calls)
        units = " ".join(f"{w / unit:.1f}" for w in waits)
        print(
            f"requeue: {hogs} hard runs, {shorts} short runs, {probe_s * 1e3:.0f} ms newcomer:"
            f" waited {units} hard checks (unit {unit:.2f} s), started {calls}"
        )
    calls = []
    waits, unit = await stopped_newcomer_wait_s(16, 4, 5.0, 0.15, 4, calls)
    print(
        f"requeue at 5 s: 16 hard, 4 short, 150 ms newcomer: "
        f"{' '.join(f'{w:.1f}' for w in waits)} s, started {calls}"
    )


async def section_waste() -> None:
    for runs, per_task, timeout_s in ((8, 1, 0.4), (12, 1, 0.4), (12, 3, 0.4), (12, 1, 1.0)):
        r = await fresh_task_load(runs, timeout_s, per_task, 8.0)
        print(
            f"waste: {runs} runs x {per_task}/task at {timeout_s} s, 8 s: {r['done']:.0f} done,"
            f" {r['reruns']:.0f} re-run, {r['wasted_s']:.1f} s wasted, {r['restarts']:.0f}"
            f" restarts, Z3 calls at once peak {r['peak']:.0f} mean {r['mean']:.2f},"
            f" latency p50 {r['p50_s']:.2f} max {r['max_s']:.2f} s"
        )


async def section_stream() -> None:
    for rate in (5.0, 10.0, 20.0):
        waits, unit, reruns = await newcomer_in_stream_s(8, rate, 0.4, 8.0)
        waits.sort()
        p50, p95 = statistics.median(waits), waits[int(0.95 * (len(waits) - 1))]
        print(
            f"stream: 8 hard + {rate:.0f}/s newcomers: {len(waits)} easy newcomers, "
            f"p50 {p50 / unit:.2f} p95 {p95 / unit:.2f} max {waits[-1] / unit:.2f} hard checks,"
            f" {reruns} re-run"
        )


async def section_decided() -> None:
    for runs, timeout_s in ((6, 0.45), (10, 0.45), (6, 1.0)):
        solo, r = await decidable_under_load(runs, timeout_s, 8.0)
        print(
            f"decided: php(8) solo {solo:.2f} s; {runs} runs at {timeout_s} s, 8 s: "
            f"{r['done']:.0f} done, {r['reruns']:.0f} re-run, {r['wasted_s']:.1f} s wasted, "
            f"{r['restarts']:.0f} restarts, peak {r['peak']:.0f} mean {r['mean']:.2f}, "
            f"{r['decided']:.0f} decided, latency p50 {r['p50_s']:.2f} max {r['max_s']:.2f} s"
        )


async def section_fork() -> None:
    for exit_how in ("_exit", "exit"):
        r = fork_report(4, 3, exit_how, 1.0 if exit_how == "exit" else 0.0)
        kids = r.pop("children")
        print(f"fork ({exit_how}): {json.dumps(r)}")
        for c in kids:
            own = c.pop("own_procs")
            print(f"  child: {len(own)} own processes, {json.dumps(c)}")


SECTIONS: Final[dict[str, Callable[[], Awaitable[None]]]] = {
    "requeue": section_requeue,
    "waste": section_waste,
    "stream": section_stream,
    "decided": section_decided,
    "fork": section_fork,
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
