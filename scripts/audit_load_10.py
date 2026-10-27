"""AUDIT-38 load measurements, round 10 (stubs and Z3 only; no network, no LLM).

Each section prints the numbers quoted in `AUDIT-38.md`. The repro tests in
`tests/audit/test_audit_38.py` reuse the helpers defined here. The load sections use
only the public `SolverExecutor` / `Z3Solver` API plus `/proc`, so the same file runs
against an older tree (`PYTHONPATH=<tree>/src:.`) for version comparisons; `requeue`
also snapshots the queue through `_Slots` when the tree has SG35's `arrival`/`lend`.

    uv run python -m scripts.audit_load_10                       # every section
    uv run python -m scripts.audit_load_10 requeue bound
    uv run python -m scripts.audit_load_10 --burn 4 bound        # with 4 CPU burners
    uv run python -m scripts.audit_load_10 proto <dir>           # write the SIGSTOP prototype

Sections: requeue, bound, pause, fork. `proto <dir>` writes a copy of `src/` into `<dir>`
whose `SolverExecutor` pauses a stuck slot's worker while it lends its place; run `bound`
with `PYTHONPATH=<dir>/src:.` to measure it (AUDIT-38 #2).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import signal
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Final

from autolab.verification import solver as solver_mod
from autolab.verification.solver import SolverOutcome, Z3Solver
from scripts.audit_load_7 import burners, run_child
from scripts.audit_load_8 import newcomer_beside_fresh_task_runs_s
from scripts.audit_load_9 import UNDECIDABLE, fresh_task_load, php, stopped_newcomer_wait_s

ROOT: Final = Path(__file__).resolve().parent.parent

# --- running Z3 processes, from /proc ------------------------------------------------------


def running_workers() -> int:
    """Worker processes of this process's pool in state R (running or runnable)."""
    n = 0
    for worker in list(solver_mod._workers()._live):
        try:
            with open(f"/proc/{worker.process.pid}/stat") as f:
                n += f.read().rsplit(")", 1)[1].split()[0] == "R"
        except (OSError, IndexError, ValueError):  # gone between the listing and the read
            continue
    return n


class RunningSampler:
    """Samples `running_workers()` every `period_s` on a thread while entered."""

    def __init__(self, period_s: float = 0.002) -> None:
        self.period_s = period_s
        self.samples: list[int] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.samples.append(running_workers())
            self._stop.wait(self.period_s)

    def __enter__(self) -> RunningSampler:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()

    def share_over(self, places: int) -> float:
        """Share of samples with more than `places` Z3 processes running."""
        return sum(n > places for n in self.samples) / max(1, len(self.samples))

    def mean(self) -> float:
        return sum(self.samples) / max(1, len(self.samples))

    def summary(self, places: int) -> str:
        return (
            f"running Z3 mean {self.mean():.2f} peak {max(self.samples, default=0)},"
            f" over {places} in {self.share_over(places):.0%} of samples"
        )


# --- #1: a newcomer that gave way waits for a slot, not for the next lent place -------------


def _snapshot_requeues(log: list[tuple[int, int, float]]) -> Callable[[], None]:
    """Patch `_Slots.acquire` (SG35 trees) to log each re-queue of a stopped newcomer:
    (aged hard callers ahead, waiters ahead, s it waited). Returns the undo."""
    slots_cls: Any = solver_mod._Slots
    orig = slots_cls.acquire

    async def acquire(
        self: Any, short: bool, new: bool = False, arrival: int | None = None, lend: bool = True
    ) -> bool:
        if arrival is None or lend:
            return bool(await orig(self, short, new, arrival, lend))
        ahead = [w for w in self._waiters if w.arrival < arrival]
        aged = sum(self._rank(w) == 0 for w in ahead)
        started = time.perf_counter()
        got = bool(await orig(self, short, new, arrival, lend))
        log.append((aged, len(ahead), time.perf_counter() - started))
        return got

    slots_cls.acquire = acquire

    def undo() -> None:
        slots_cls.acquire = orig

    return undo


async def section_requeue() -> None:
    sg35 = "lend" in solver_mod._Slots.acquire.__code__.co_varnames
    for timeout_s, probes in ((0.4, 6), (1.0, 6), (5.0, 6)):
        log: list[tuple[int, int, float]] = []
        undo = _snapshot_requeues(log) if sg35 else (lambda: None)
        calls: list[int] = []
        try:
            waits, unit = await stopped_newcomer_wait_s(16, 4, timeout_s, 0.15, probes, calls)
        finally:
            undo()
        print(
            f"requeue at {timeout_s} s: 16 hard, 4 short, 150 ms newcomer waited "
            f"{' '.join(f'{w:.2f}' for w in waits)} s = "
            f"{' '.join(f'{w / unit:.1f}' for w in waits)} hard checks, started {calls}"
        )
        # The hogs' own first checks are re-queued too, before the probes: keep the last.
        for aged, ahead, waited in log[-sum(c > 1 for c in calls) or len(log) :]:
            print(f"  re-queued: {aged} aged hard of {ahead} ahead, waited {waited:.2f} s")


# --- #2: Z3 processes running at once, and the SIGSTOP alternative --------------------------


async def section_bound() -> None:
    places = 4  # 2 slots + 2 slow places
    for runs in (8, 12):
        with RunningSampler() as sampler:
            r = await fresh_task_load(runs, 0.4, 1, 8.0)
        print(
            f"bound: {runs} runs x 1 check/task at 0.4 s, 8 s: {r['done']:.0f} done,"
            f" {r['reruns']:.0f} re-run, {r['wasted_s']:.1f} s wasted, {r['restarts']:.0f}"
            f" restarts, latency p50 {r['p50_s']:.2f} max {r['max_s']:.2f} s;"
            f" {sampler.summary(places)}"
        )
    for hogs in (8, 12, 16):
        with RunningSampler() as sampler:
            waits, unit = await newcomer_beside_fresh_task_runs_s(hogs, 0.4, 1, 6)
        print(
            f"bound: easy newcomer beside {hogs} fresh-task hard runs waited "
            f"{' '.join(f'{w / unit:.1f}' for w in waits)} hard checks; {sampler.summary(places)}"
        )


PROTO_EDITS: Final[tuple[tuple[str, str], ...]] = (
    ("import queue\n", "import contextlib\nimport queue\nimport signal\n"),
    (
        "        self._hard: weakref.WeakSet[asyncio.Task[Any]] = weakref.WeakSet()\n\n"
        "    async def run",
        "        self._hard: weakref.WeakSet[asyncio.Task[Any]] = weakref.WeakSet()\n"
        "        self._lenders: list[_Interrupt] = []\n"
        "        self._frozen: set[int] = set()\n\n"
        "    def _freeze(self) -> int | None:\n"
        "        for lender in self._lenders:\n"
        "            proc = getattr(lender._target, 'process', None)\n"
        "            if proc is None or proc.pid in self._frozen:\n"
        "                continue\n"
        "            with contextlib.suppress(OSError):\n"
        "                os.kill(proc.pid, signal.SIGSTOP)\n"
        "                self._frozen.add(proc.pid)\n"
        "                return int(proc.pid)\n"
        "        return None\n\n"
        "    def _thaw(self, pid: int | None) -> None:\n"
        "        if pid is None:\n"
        "            return\n"
        "        self._frozen.discard(pid)\n"
        "        with contextlib.suppress(OSError):\n"
        "            os.kill(pid, signal.SIGCONT)\n\n"
        "    async def run",
    ),
    (
        "            slow = stuck = stopped = False\n",
        "            slow = stuck = stopped = False\n"
        "            frozen = self._freeze() if lent else None\n",
    ),
    (
        "                            if stuck:\n"
        "                                slots.unstick()\n"
        "                            slots.release(lent)\n",
        "                            if stuck:\n"
        "                                slots.unstick()\n"
        "                                self._lenders.remove(interrupt)\n"
        "                            self._thaw(frozen)\n"
        "                            frozen = None\n"
        "                            slots.release(lent)\n",
    ),
    (
        "                            stuck = not interrupt.paused() and slots.stick(lent)\n",
        "                            stuck = not interrupt.paused() and slots.stick(lent)\n"
        "                            if stuck:\n"
        "                                self._lenders.append(interrupt)\n",
    ),
    (
        "                    if stuck:\n"
        "                        slots.unstick()\n"
        "                    slots.release(lent)\n",
        "                    if stuck:\n"
        "                        slots.unstick()\n"
        "                        self._lenders.remove(interrupt)\n"
        "                    self._thaw(frozen)\n"
        "                    slots.release(lent)\n",
    ),
)
"""A stuck slot lends its own CPU: while a caller holds a place it lent, the slot's worker
is paused (SIGSTOP), and resumed (SIGCONT) when the place is given back or the lent check
moves to a slow place. Ranks, aging and the stop rule are unchanged. A prototype only."""


def write_proto(out: Path) -> Path:
    """Copy `src/` into `out` and apply `PROTO_EDITS` to its `solver.py`."""
    shutil.copytree(ROOT / "src", out / "src", dirs_exist_ok=True)
    path = out / "src" / "autolab" / "verification" / "solver.py"
    text = path.read_text()
    for old, new in PROTO_EDITS:
        if text.count(old) != 1:
            raise RuntimeError(f"edit does not apply once: {old[:60]!r}")
        text = text.replace(old, new)
    path.write_text(text)
    return out / "src"


def paused_check(check: Any, timeout_s: float, pause_at_s: float, pause_s: float) -> str:
    """Run `check` with `timeout_s` on a worker; pause the worker `pause_s` at `pause_at_s`.
    Returns verdict, wall and the CPU the worker used while paused."""
    pool = solver_mod._workers()
    pool.prestart(1)
    picked: list[Any] = []
    acquire = pool.acquire

    def recording() -> Any:
        worker = acquire()
        picked.append(worker)
        return worker

    pool.acquire = recording  # type: ignore[method-assign]
    out: list[SolverOutcome] = []
    started = time.perf_counter()
    thread = threading.Thread(
        target=lambda: out.append(Z3Solver(timeout_s).check_entailment(*check))
    )
    thread.start()
    cpu = 0.0
    try:
        if pause_s:
            time.sleep(pause_at_s)
            pid = picked[0].process.pid
            before = _cpu_s(pid)
            os.kill(pid, signal.SIGSTOP)
            time.sleep(pause_s)
            cpu = _cpu_s(pid) - before
            os.kill(pid, signal.SIGCONT)
        thread.join()
    finally:
        pool.acquire = acquire  # type: ignore[method-assign]
    wall = time.perf_counter() - started
    return f"{out[0].verdict} ({out[0].reason}) in {wall:.2f} s, {cpu:.3f} CPU s while paused"


def _cpu_s(pid: int) -> float:
    with open(f"/proc/{pid}/stat") as f:
        fields = f.read().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


async def section_pause() -> None:
    for name, check, timeout_s in (("php(8)", php(8), 5.0), ("runs-to-timeout", UNDECIDABLE, 1.0)):
        print(f"pause: {name} at {timeout_s} s alone: {paused_check(check, timeout_s, 0, 0)}")
        paused = paused_check(check, timeout_s, 0.1, 0.5)
        print(f"pause: {name} at {timeout_s} s, paused 0.5 s at 0.1 s: {paused}")


# --- #3: a fork between a worker's start and its registration --------------------------------

FORK_WINDOW: Final = """
import os, sys, threading, time
from autolab.verification import solver as solver_mod
from autolab.verification import solver_workers

started = threading.Event()
init = solver_workers.Worker.__init__

def slow_init(self, ctx, target):
    init(self, ctx, target)  # the process has started and is in multiprocessing's children
    if not started.is_set():
        started.set()
        time.sleep(0.5)  # stands in for a thread switch before `Workers._live.add`

solver_workers.Worker.__init__ = slow_init
pool = solver_mod._workers()
thread = threading.Thread(target=pool.prestart, args=(1,))
thread.start()
assert started.wait(30)
pid = os.fork()
if pid == 0:
    sys.stderr = open(os.devnull, "w")  # multiprocessing's atexit join complains
    sys.exit(0)  # a prefork worker's normal exit
os.waitpid(pid, 0)
thread.join()
time.sleep(0.3)
worker = next(iter(pool._live))
print(f"REPORT {int(worker.alive())} {worker.process.exitcode}")
"""


def fork_in_start_window() -> tuple[bool, int | None]:
    """(the owner's worker is alive, its exit code) after a child forked while the worker
    was between `Process.start()` and `Workers._live.add` exits normally."""
    out = run_child(FORK_WINDOW)
    line = next(ln for ln in out.splitlines() if ln.startswith("REPORT "))
    alive, code = line.split()[1:]
    return alive == "1", None if code == "None" else int(code)


async def section_fork() -> None:
    for _ in range(3):
        alive, code = fork_in_start_window()
        print(f"fork: owner's worker alive {alive}, exit code {code}")


SECTIONS: Final[dict[str, Callable[[], Awaitable[None]]]] = {
    "requeue": section_requeue,
    "bound": section_bound,
    "pause": section_pause,
    "fork": section_fork,
}


async def amain(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sections", nargs="*")
    parser.add_argument("--burn", type=int, default=0, help="CPU burners to run meanwhile")
    args = parser.parse_args(argv)
    if args.sections[:1] == ["proto"]:
        print(write_proto(Path(args.sections[1]).resolve()))
        return 0
    with burners(args.burn):
        for name in args.sections or SECTIONS:
            print(f"[{name}] load {os.getloadavg()[0]:.2f}", flush=True)
            await SECTIONS[name]()
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
