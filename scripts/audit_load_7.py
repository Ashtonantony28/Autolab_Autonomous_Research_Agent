"""AUDIT-26 load measurements, round 7 (FakeLLMClient / stubs only; no network).

Each section prints the numbers quoted in `AUDIT-26.md`. The repro tests in
`tests/audit/test_audit_26.py` reuse the helpers defined here.

    uv run python -m scripts.audit_load_7                 # every section
    uv run python -m scripts.audit_load_7 newcomer probe
    uv run python -m scripts.audit_load_7 --burn 4 judges # with 4 CPU burners

Sections: judges, cold, boot, storm, newcomer, probe, unguarded, leaks, wake.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Final

import z3

from autolab.verification import formalizer as formalizer_mod
from autolab.verification import solver as solver_mod
from autolab.verification.formalizer import Vocabulary
from autolab.verification.solver import SmtFormula, SolverExecutor, Z3Solver
from autolab.verification.vericot import Verifier
from scripts.audit_load import LatencyLLM, chain
from scripts.audit_load_2 import slow_check
from scripts.audit_load_4 import ROOT, slow_verify
from scripts.audit_load_6 import conjunction_pool
from tests.fixtures.golden_traces import build

EASY: Final = (
    [SmtFormula("(declare-const a Bool)", "a")],
    SmtFormula("(declare-const a Bool)", "a"),
)
"""A check that decides in about a millisecond."""


@contextmanager
def burners(n: int) -> Iterator[None]:
    """`n` busy-looping processes for the duration; always killed afterwards."""
    procs = [subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(max(0, n))]
    try:
        if procs:
            time.sleep(0.5)
        yield
    finally:
        for p in procs:
            p.kill()
        for p in procs:
            p.wait()


def run_child(
    code: str, *, inline: bool = False, main_name: str = "main.py", timeout_s: float = 120.0
) -> str:
    """Run `code` as a path-run main script (or with `-c`, `inline`) in a fresh
    interpreter; return its stdout. A `-c` main has no path for a worker to re-run."""
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / main_name
        header = f"import sys\nsys.path.insert(0, {str(ROOT)!r})\nsys.path.insert(0, {tmp!r})\n"
        script.write_text(header + textwrap.dedent(code))
        env = {**os.environ, "AUDIT26_TMP": tmp}
        done = subprocess.run(
            [sys.executable, *(["-c", script.read_text()] if inline else [str(script)])],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=ROOT,
            env=env,
            check=False,
        )
    if done.returncode != 0:
        raise RuntimeError(f"child failed ({done.returncode}): {done.stderr[-2000:]}")
    return done.stdout


# --- #1: a new task's first check queues behind every hard caller -----------------------


async def hard_check_s(executor: SolverExecutor, timeout_s: float) -> float:
    """Wall s of one runs-to-timeout check alone: the unit the newcomer's wait is read in."""
    started = time.perf_counter()
    await slow_check(executor, timeout_s)
    return time.perf_counter() - started


async def newcomer_first_check_s(hogs: int, timeout_s: float) -> tuple[float, float]:
    """(s a new task's first easy check waits beside `hogs` hard runs, one hard check's s).

    2 slots and 2 slow places, whatever the machine's size; each hard run makes
    back-to-back checks that run to `timeout_s`. The newcomer arrives once every hard
    run has completed a check: before that the runs are newcomers too, and a fair
    queue serves them, having arrived first, before it (SG32).
    """
    ex = SolverExecutor(2, 2)
    tasks: list[asyncio.Task[None]] = []
    hard = [0]

    async def hog() -> None:
        await slow_check(ex, timeout_s)
        hard[0] += 1
        await slow_verify(ex, timeout_s, 1000)

    try:
        # The first check may pay worker start-up (finding 3); the unit is a warm one.
        unit = min([await asyncio.create_task(hard_check_s(ex, timeout_s)) for _ in range(2)])
        tasks = [asyncio.create_task(hog()) for _ in range(hogs)]
        while hard[0] < hogs:  # every run is hard, and in or queued for a check
            await asyncio.sleep(0.01)
        check = partial(Z3Solver().check_entailment, *EASY)

        async def newcomer() -> float:
            started = time.perf_counter()
            await ex.run(check)
            return time.perf_counter() - started

        waited = await asyncio.create_task(newcomer())
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        ex.shutdown()
    return waited, unit


async def newcomer_verify_s(hogs: int, timeout_s: float) -> tuple[float, float, float]:
    """(10-step easy verify alone, in a new task beside `hogs` hard runs, in the old task)."""
    ex = SolverExecutor(2, 2)
    tasks: list[asyncio.Task[None]] = []

    async def verify() -> float:
        trace, fake = build(chain(10, cite=True))
        started = time.perf_counter()
        await Verifier(fake, solver_executor=ex).verify(trace)
        return time.perf_counter() - started

    try:
        await verify()  # warms the workers; this task has now checked before ("short")
        alone = await verify()
        tasks = [asyncio.create_task(slow_verify(ex, timeout_s, 1000)) for _ in range(hogs)]
        await asyncio.sleep(0.05)
        new_task = await asyncio.create_task(verify())
        same_task = await verify()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        ex.shutdown()
    return alone, new_task, same_task


# --- #2: SG28 bounds a line, not a reply ------------------------------------------------


def wide_reply(declarations: int, arity: int = formalizer_mod.MAX_ARITY) -> str:
    """`declarations` lines, each a function of `arity` Int arguments: a legal reply."""
    args = " ".join(["Int"] * arity)
    return "\n".join(f"(declare-fun f{i} ({args}) Bool)" for i in range(declarations))


def deep_sort_decl(depth: int) -> str:
    """One declaration whose first argument is an Array sort nested `depth` deep."""
    sort = "Int"
    for _ in range(depth):
        sort = f"(Array Int {sort})"
    return f"(declare-fun g ({sort} Int) Bool)"


def merge_s(decls: str) -> float:
    """Wall s of `Vocabulary.merged(decls)`, caches cleared (runs on the event loop)."""
    formalizer_mod._applied_builtin.cache_clear()
    formalizer_mod._is_z3_builtin.cache_clear()
    started = time.perf_counter()
    Vocabulary().merged(decls)
    return time.perf_counter() - started


def parse_once_s(decls: str) -> float:
    """Wall s of one Z3 parse of `decls` in a fresh context: the machine's unit."""
    started = time.perf_counter()
    z3.parse_smt2_string(decls, ctx=z3.Context())
    return time.perf_counter() - started


def parses_to_merge(decls: str) -> int:
    """How many times `Vocabulary.merged(decls)` calls Z3's parser."""
    real = z3.Z3_parse_smtlib2_string
    calls = [0]

    def counting(*args: object) -> object:
        calls[0] += 1
        return real(*args)

    formalizer_mod._applied_builtin.cache_clear()
    formalizer_mod._is_z3_builtin.cache_clear()
    z3.Z3_parse_smtlib2_string = counting
    try:
        Vocabulary().merged(decls)
    finally:
        z3.Z3_parse_smtlib2_string = real
    return calls[0]


# --- #3: cold start on the first check ----------------------------------------------------

COLD_CHILD: Final = """
import asyncio, json, time
from autolab.verification import solver as solver_mod
from autolab.verification import solver_workers
from autolab.verification.vericot import Verifier
from scripts.audit_load import LatencyLLM, chain
from tests.fixtures.golden_traces import build

events = []
_start = solver_workers.Workers._start
def start(self):
    events.append(("start", time.perf_counter()))
    return _start(self)
solver_workers.Workers._start = start
_check = Verifier._check
async def check(self, *args):
    events.append(("check", time.perf_counter()))
    return await _check(self, *args)
Verifier._check = check

async def verify(delay):
    trace, fake = build(chain(6, cite=True))
    started = time.perf_counter()
    result = await Verifier(LatencyLLM(fake, delay), check_faithfulness=True).verify(trace)
    assert result.consistency_score == 1.0, result
    return time.perf_counter() - started

async def main():
    cold = await verify(DELAY)
    warm = [await verify(DELAY) for _ in range(3)]
    first = {k: min(t for e, t in events if e == k) for k in ("start", "check")}
    print(json.dumps({"cold": cold, "warm": min(warm),
                      "start_before_check": first["start"] < first["check"]}))

if __name__ == "__main__":
    asyncio.run(main())
"""


def cold_start(delay_s: float = 0.1, *, inline: bool = False) -> dict[str, float | bool]:
    """A fresh process's first 6-step verify against its warm ones; was a worker started
    before the first check was requested?"""
    import json

    out = run_child(f"DELAY = {delay_s!r}\n" + COLD_CHILD, inline=inline)
    parsed: dict[str, float | bool] = json.loads(out.strip().splitlines()[-1])
    return parsed


# --- #4: every worker boot re-runs a path-run main ----------------------------------------

BOOT_CHILD: Final = """
import json, os, threading, time
{imports}
from autolab.verification import solver as solver_mod

def boot(pool, out):
    t = time.perf_counter()
    w = pool._start()
    assert w.ready()
    out.append(time.perf_counter() - t)
    w.stop()

if __name__ == "__main__":
    pool = solver_mod._workers()
    first = []
    boot(pool, first)
    seq = []
    for _ in range({boots}):
        boot(pool, seq)
    par = []
    ths = [threading.Thread(target=boot, args=(pool, par)) for _ in range({parallel})]
    t = time.perf_counter()
    for th in ths: th.start()
    for th in ths: th.join()
    wall = time.perf_counter() - t
    print(json.dumps({{"first": first[0], "boots": sorted(seq), "parallel_wall": wall}}))
"""

MARKER: Final = """
import os
with open(os.path.join(os.environ["AUDIT26_TMP"], "imports.txt"), "a") as f:
    f.write(f"{os.getpid()}\\n")
"""


def boot_times(imports: str, boots: int = 6, parallel: int = 4) -> dict[str, object]:
    """Worker boot times when the main script's top level runs `imports`."""
    import json

    code = BOOT_CHILD.format(imports=imports, boots=boots, parallel=parallel)
    parsed: dict[str, object] = json.loads(run_child(code).strip().splitlines()[-1])
    return parsed


def main_imports_per_boot(boots: int = 3) -> int:
    """Processes that ran a path-run main's top-level imports while `boots` workers started.

    Counted by a marker module the main imports. Preloaded once in the forkserver,
    as ADR 0009 says, the answer is 2 (the parent and the forkserver).
    """
    code = f"""
import os
from pathlib import Path
Path(os.environ["AUDIT26_TMP"], "audit26_marker.py").write_text({MARKER!r})
import audit26_marker  # noqa: F401
from autolab.verification import solver as solver_mod

if __name__ == "__main__":
    pool = solver_mod._workers()
    workers = [pool._start() for _ in range({boots})]
    assert all(w.ready() for w in workers)
    for w in workers:
        w.stop()
    print(len(set(Path(os.environ["AUDIT26_TMP"], "imports.txt").read_text().split())))
"""
    return int(run_child(code).strip().splitlines()[-1])


# --- #5: an unguarded main ----------------------------------------------------------------

UNGUARDED_CHILD: Final = """
import time
from autolab.verification.solver import SmtFormula, Z3Solver
t = time.perf_counter()
out = Z3Solver(timeout_seconds=1.0).check_entailment(
    [SmtFormula("(declare-const x Int)", "(> x 0)")],
    SmtFormula("(declare-const x Int)", "(> x -1)"),
)
print(f"RESULT {out.verdict}|{out.reason}|{out.details}|{time.perf_counter() - t:.3f}")
"""


def unguarded_outcome() -> tuple[str, str, str, float]:
    """(verdict, reason, details, s) of an easy check made by a main with no `__main__` guard."""
    lines = [ln for ln in run_child(UNGUARDED_CHILD).splitlines() if ln.startswith("RESULT ")]
    verdict, reason, details, took = lines[-1][len("RESULT ") :].split("|")
    return verdict, reason, details, float(took)


# --- storms and leaks ---------------------------------------------------------------------


def _descendants(root: int | None = None) -> set[int]:
    kids: dict[int, list[int]] = collections.defaultdict(list)
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                with open(f"/proc/{d}/stat") as f:
                    fields = f.read().rsplit(")", 1)[1].split()
            except OSError:
                continue
            kids[int(fields[1])].append(int(d))
    out: set[int] = set()
    todo = [os.getpid() if root is None else root]
    while todo:
        for k in kids.get(todo.pop(), []):
            out.add(k)
            todo.append(k)
    return out


def _zombies(pids: set[int]) -> int:
    n = 0
    for p in pids:
        try:
            with open(f"/proc/{p}/stat") as f:
                n += f.read().rsplit(")", 1)[1].split()[0] == "Z"
        except OSError:
            continue
    return n


async def kill_storm(n: int, timeout_s: float, bounds: int = 1000) -> dict[str, float]:
    """`n` concurrent unpolled checks killed past `timeout_s`, with an easy prober beside."""
    ex = SolverExecutor(2, 2)
    await asyncio.sleep(1.0)
    pool, (p, c) = solver_mod._workers(), conjunction_pool(bounds, 50)
    z, easy = Z3Solver(timeout_seconds=timeout_s), partial(Z3Solver().check_entailment, *EASY)
    fds0 = len(os.listdir("/proc/self/fd"))
    probes: list[float] = []

    async def prober() -> None:
        await asyncio.sleep(0.2)
        for _ in range(10):
            t = time.perf_counter()
            await ex.run(easy)
            probes.append(time.perf_counter() - t)

    started = time.perf_counter()
    probe = asyncio.create_task(prober())
    outcomes = await asyncio.gather(*(ex.run(partial(z.check_entailment, p, c)) for _ in range(n)))
    wall = time.perf_counter() - started
    await probe
    ex.shutdown()
    await asyncio.sleep(0.5)
    kids = _descendants()
    return {
        "wall": wall,
        "killed": sum(o.details == "worker killed past the timeout" for o, _ in outcomes),
        "probe_max": max(probes),
        "fds_delta": len(os.listdir("/proc/self/fd")) - fds0,
        "live": pool.live(),
        "zombies": _zombies(kids),
    }


def worker_rss_mb(checks: int) -> tuple[float, float]:
    """(RSS MB, after `checks` mixed VALID/INVALID checks) of one reused worker."""
    from scripts.audit_load_3 import invalid_checks
    from scripts.audit_load_5 import bool_chain

    pool = solver_mod._workers()
    keep, pool.idle_max = pool.idle_max, 1
    try:
        z, (p, c), inv = Z3Solver(), bool_chain(100), invalid_checks(50)
        z.check_entailment(p, c)
        worker = pool._idle[0]

        def rss() -> float:
            with open(f"/proc/{worker.process.pid}/status") as f:
                return next(int(ln.split()[1]) for ln in f if ln.startswith("VmRSS")) / 1024

        before = rss()
        for i in range(checks):
            z.check_entailment(p, c) if i % 2 else z.check_entailment(*inv[i % len(inv)])
        return before, rss()
    finally:
        pool.idle_max = keep


ORPHAN_CHILD: Final = """
from autolab.verification.solver import Z3Solver
from scripts.audit_load_6 import conjunction_pool
p, c = conjunction_pool(1000, 100)
print("go", flush=True)
Z3Solver(timeout_seconds=60).check_entailment(p, c)
"""


def orphans_after_kill(sig: int) -> tuple[int, int, float]:
    """(descendants, still alive, s waited) after `sig` hits a parent mid-check."""
    code = f"import sys; sys.path.insert(0, {str(ROOT)!r})\n" + ORPHAN_CHILD
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    proc.stdout.readline()
    time.sleep(1.0)
    tree = _descendants(proc.pid)
    os.kill(proc.pid, sig)
    proc.wait()
    started = time.perf_counter()
    while any(_alive(p) for p in tree) and time.perf_counter() - started < 15:
        time.sleep(0.05)
    left = [p for p in tree if _alive(p)]
    for p in left:
        os.kill(p, signal.SIGKILL)
    return len(tree), len(left), time.perf_counter() - started


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


async def wake_cost_s(waiters: int) -> float:
    """s spent in `_Slots._wake` granting 2 slots to `waiters` queued callers."""
    slots = solver_mod._Slots(2)
    spent = [0.0]
    wake = slots._wake

    def timed() -> None:
        t = time.perf_counter()
        wake()
        spent[0] += time.perf_counter() - t

    slots._wake = timed  # type: ignore[method-assign]

    async def one() -> None:
        lent = await slots.acquire(False)
        await asyncio.sleep(0)
        slots.release(lent)

    await asyncio.gather(*(one() for _ in range(waiters)))
    return spent[0]


async def judges_walls(runs: int) -> list[float]:
    """Walls of test_judges_overlap_but_formalization_stays_ordered's verify, first one cold
    if nothing in this process has checked yet."""
    walls = []
    for _ in range(runs):
        trace, fake = build(chain(6, cite=True))
        started = time.perf_counter()
        await Verifier(LatencyLLM(fake, 0.1), check_faithfulness=True).verify(trace)
        walls.append(time.perf_counter() - started)
    return walls


# --- sections -----------------------------------------------------------------------------


async def section_judges() -> None:
    walls = await judges_walls(8)
    print(
        f"judges: cold {walls[0]:.3f} s, warm {' '.join(f'{w:.3f}' for w in walls[1:])}"
        f" (bound 0.8 s; over: {sum(w >= 0.8 for w in walls[1:])}/7 warm)"
    )


async def section_cold() -> None:
    for label, inline in (("python -c main", True), ("path-run main", False)):
        r = cold_start(inline=inline)
        print(
            f"cold ({label}): first verify {r['cold']:.3f} s, warm {r['warm']:.3f} s, "
            f"a worker started before the first check: {r['start_before_check']}"
        )


async def section_boot() -> None:
    for label, imports in (
        ("light", ""),
        ("autolab.cli", "from autolab.cli import main as _cli"),
        ("pytest", "import pytest"),
        ("scripts.audit_load_6", "import scripts.audit_load_6"),
    ):
        r = boot_times(imports)
        boots = r["boots"]
        assert isinstance(boots, list)
        print(
            f"boot ({label} main): forkserver+first {r['first']:.3f} s, sequential "
            f"{boots[0] * 1e3:.0f}-{boots[-1] * 1e3:.0f} ms, 4 parallel {r['parallel_wall']:.2f} s"
        )
    print(f"boot: processes that ran the main's imports for 3 boots: {main_imports_per_boot()}")


async def section_storm() -> None:
    r = await kill_storm(20, 0.5)
    print("storm 20 x 0.5 s (in-process main): " + ", ".join(f"{k} {v:.2f}" for k, v in r.items()))


async def section_newcomer() -> None:
    for hogs, t in ((8, 0.5), (16, 0.5), (16, 5.0)):
        alone, new, same = await newcomer_verify_s(hogs, t)
        print(
            f"newcomer verify, {hogs} hard runs at {t} s: alone {alone:.2f} s, new task "
            f"{new:.2f} s ({new / t:.1f} timeouts), old task {same:.2f} s"
        )
    for hogs in (8, 12, 16):
        waited, unit = await newcomer_first_check_s(hogs, 0.4)
        print(
            f"newcomer first check, {hogs} hard runs: {waited:.2f} s"
            f" = {waited / unit:.1f} hard checks"
        )


async def section_probe() -> None:
    for n, arity in ((100, 32), (300, 8), (600, 1)):
        decls = wide_reply(n, arity)
        m, p = merge_s(decls), min(parse_once_s(decls) for _ in range(3))
        print(
            f"probe {n} x arity {arity}: {len(decls) / 1024:.1f} KB, merged {m * 1e3:.0f} ms "
            f"({parses_to_merge(decls)} parses; one parse {p * 1e3:.1f} ms, x{m / p:.0f})"
        )
    for depth in (200, 400, 800, 1400):
        d = deep_sort_decl(depth)
        print(f"probe deep sort {depth}: {len(d) / 1024:.1f} KB, merged {merge_s(d) * 1e3:.0f} ms")


async def section_unguarded() -> None:
    print("unguarded main: " + " | ".join(map(str, unguarded_outcome())))


async def section_leaks() -> None:
    before, after = worker_rss_mb(3000)
    print(f"leaks: worker RSS {before:.1f} -> {after:.1f} MB over 3000 checks")
    for sig in (signal.SIGKILL, signal.SIGTERM):
        n, left, waited = orphans_after_kill(sig)
        print(
            f"leaks: parent {sig.name} mid-check: {n} helper processes,"
            f" {left} left after {waited:.2f} s"
        )


async def section_wake() -> None:
    for n in (250, 1000, 4000):
        print(f"wake: {n} waiters, {await wake_cost_s(n):.2f} s in _wake")


SECTIONS: Final[dict[str, Callable[[], Awaitable[None]]]] = {
    "judges": section_judges,
    "cold": section_cold,
    "boot": section_boot,
    "storm": section_storm,
    "newcomer": section_newcomer,
    "probe": section_probe,
    "unguarded": section_unguarded,
    "leaks": section_leaks,
    "wake": section_wake,
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
