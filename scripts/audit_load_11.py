"""AUDIT-42 load measurements, round 11 (stubs and Z3 only; no network, no LLM).

Each section prints the numbers quoted in `AUDIT-42.md`. The repro tests in
`tests/audit/test_audit_42.py` reuse the helpers defined here. The load sections use the
public `SolverExecutor` / `Z3Solver` API plus `/proc`, so the same file runs against an
older tree (`PYTHONPATH=<tree>/src:.`) for version comparisons; `giveway` also counts
give-ways through `SolverExecutor._again` when the tree has SG36's.

    uv run python -m scripts.audit_load_11                       # every section
    uv run python -m scripts.audit_load_11 lender giveway
    uv run python -m scripts.audit_load_11 --burn 4 watchdog     # with 4 CPU burners

Sections: lender, giveway, held, watchdog, vocab, drift.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Final

import z3

from autolab.verification import solver as solver_mod
from autolab.verification.solver import SolverExecutor, SolverOutcome, Z3Solver
from scripts.audit_load_7 import EASY, burners
from scripts.audit_load_8 import merge_into_vocab_s
from scripts.audit_load_9 import UNDECIDABLE


def _easy() -> SolverOutcome:
    return Z3Solver().check_entailment(*EASY)


def _hard(timeout_s: float) -> Callable[[], SolverOutcome]:
    return partial(Z3Solver(timeout_s).check_entailment, *UNDECIDABLE)


async def _timed(make: Callable[[], Awaitable[Any]]) -> float:
    started = time.perf_counter()
    await make()
    return time.perf_counter() - started


async def _cancel(tasks: Sequence[asyncio.Task[Any]]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def until(condition: Callable[[], bool], limit_s: float = 10.0) -> None:
    """Poll `condition` every 5 ms; AssertionError after `limit_s`."""
    deadline = time.monotonic() + limit_s
    while not condition():
        assert time.monotonic() < deadline, "condition never held"
        await asyncio.sleep(0.005)


class ShortStream:
    """`n` short callers, each making back-to-back easy checks on `executor` while entered."""

    def __init__(self, executor: SolverExecutor, n: int) -> None:
        self.executor = executor
        self.n = n
        self.checks = 0
        self._stop = False
        self._tasks: list[asyncio.Task[None]] = []

    async def _loop(self) -> None:
        while not self._stop:
            await self.executor.run(_easy)
            self.checks += 1

    async def __aenter__(self) -> ShortStream:
        self._tasks = [asyncio.create_task(self._loop()) for _ in range(self.n)]
        return self

    async def __aexit__(self, *_: object) -> None:
        self._stop = True
        await _cancel(self._tasks)


# --- #1: a frozen lender stays frozen while short callers keep its lent place busy --------


@dataclass
class LenderRun:
    """What `lender_walls_s` measured."""

    alone_s: float
    walls_s: list[float]
    hards: int
    easy_checks: int
    elapsed_s: float


async def lender_walls_s(
    workers: int, slow: int, hards: int, shorts: int, timeout_s: float, cap_s: float
) -> LenderRun:
    """`hards` runs-to-timeout checks, each in a new task, fill the slots (and slow places);
    once they are stuck, `shorts` short callers stream easy checks. Returns each hard
    check's wall (those done within `cap_s`) and one such check's wall alone."""
    ex = SolverExecutor(workers, slow)
    pending: list[asyncio.Task[float]] = []
    try:
        await ex.run(_easy)
        alone = await asyncio.create_task(_timed(lambda: ex.run(_hard(timeout_s))))
        pending = [
            asyncio.create_task(_timed(partial(ex.run, _hard(timeout_s)))) for _ in range(hards)
        ]
        await asyncio.sleep(0.3)  # every slot's check is stuck, or slow
        started = time.perf_counter()
        async with ShortStream(ex, shorts) as stream:
            done, _ = await asyncio.wait(pending, timeout=cap_s)
        elapsed = time.perf_counter() - started
        return LenderRun(alone, sorted(t.result() for t in done), hards, stream.checks, elapsed)
    finally:
        await _cancel(pending)
        ex.shutdown()


async def section_lender() -> None:
    for workers, slow, hards, shorts in (
        (1, 0, 1, 0),
        (1, 0, 1, 1),
        (1, 0, 1, 2),
        (1, 0, 1, 4),
        (2, 2, 4, 0),
        (2, 2, 4, 2),
        (2, 2, 4, 4),
        (2, 2, 4, 8),
    ):
        r = await lender_walls_s(workers, slow, hards, shorts, 1.0, 10.0)
        print(
            f"lender {workers}+{slow}, {hards} hard at 1.0 s (alone {r.alone_s:.2f} s), "
            f"{shorts} short loops: {len(r.walls_s)}/{hards} done within 10 s, walls "
            f"{' '.join(f'{w:.2f}' for w in r.walls_s) or '-'}; "
            f"{r.easy_checks} easy checks in {r.elapsed_s:.1f} s"
        )


# --- #3: a newcomer that gave way gives way again to every short caller --------------------


class GiveWays:
    """Counts SG36's re-queues of a paused newcomer (`SolverExecutor._again`) while entered."""

    def __init__(self) -> None:
        self.count = 0
        self._orig: Any = None

    def __enter__(self) -> GiveWays:
        cls: Any = SolverExecutor
        self._orig = vars(cls).get("_again")
        if self._orig is not None:
            inner = self._orig.__func__

            async def again(*args: Any) -> Any:
                self.count += 1
                return await inner(*args)

            cls._again = staticmethod(again)
        return self

    def __exit__(self, *_: object) -> None:
        if self._orig is not None:
            cls: Any = SolverExecutor
            cls._again = self._orig


@dataclass
class GaveWayRun:
    """What `gave_way_waits_s` measured: a probe's wall alone, and each probe's (wall, or
    None past the cap; give-ways)."""

    alone_s: float
    probes: list[tuple[float | None, int]] = field(default_factory=list)


async def gave_way_waits_s(
    workers: int,
    slow: int,
    hards: int,
    shorts: int,
    hard_s: float,
    probe_s: float,
    probes: int,
    cap_s: float,
) -> GaveWayRun:
    """`hards` runs make back-to-back runs-to-timeout checks of `hard_s`, `shorts` short
    callers stream easy checks, and each probe is a new task making one runs-to-timeout
    check of `probe_s` (more than a slice, fewer than `_HARD_SLICES`: not hard)."""
    ex = SolverExecutor(workers, slow)
    stop = [False]
    hogs: list[asyncio.Task[None]] = []

    async def hog() -> None:
        while not stop[0]:
            await ex.run(_hard(hard_s))

    try:
        await ex.run(_easy)
        run = GaveWayRun(await asyncio.create_task(_timed(lambda: ex.run(_hard(probe_s)))))
        hogs = [asyncio.create_task(hog()) for _ in range(hards)]
        await asyncio.sleep(0.3)
        async with ShortStream(ex, shorts):
            await asyncio.sleep(0.3)
            for _ in range(probes):
                with GiveWays() as gave:
                    probe = asyncio.create_task(_timed(lambda: ex.run(_hard(probe_s))))
                    done, _ = await asyncio.wait((probe,), timeout=cap_s)
                    if not done:
                        await _cancel([probe])
                    run.probes.append((probe.result() if done else None, gave.count))
        return run
    finally:
        stop[0] = True
        await _cancel(hogs)
        ex.shutdown()


async def section_giveway() -> None:
    for workers, slow, hards, shorts in (
        (1, 0, 1, 1),
        (1, 0, 1, 2),
        (1, 0, 1, 4),
        (2, 2, 4, 2),
        (2, 2, 4, 4),
        (2, 2, 16, 4),
        (2, 2, 16, 8),
    ):
        r = await gave_way_waits_s(workers, slow, hards, shorts, 5.0, 0.15, 3, 8.0)
        shown = ", ".join(
            f"{'>8' if w is None else f'{w:.2f}'} s ({n} give-ways)" for w, n in r.probes
        )
        print(
            f"giveway {workers}+{slow}, {hards} hard at 5 s, {shorts} short loops: "
            f"150 ms newcomer (alone {r.alone_s:.2f} s) {shown}"
        )


# --- #2: paused newcomers each hold a worker process and a pool thread --------------------


def _pss_kb(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except OSError:  # the process is gone
        return 0
    return 0


def _state(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):  # the process is gone
        return "gone"


@dataclass
class Held:
    """Peaks `burst_held` sampled from the process-wide worker pool and `/proc`."""

    live: int = 0
    stopped: int = 0
    running: int = 0
    threads: int = 0
    pss_mb: float = 0.0
    total_s: float = 0.0
    max_wall_s: float = 0.0


class HeldSampler:
    """Samples worker processes (live, stopped, running), threads and worker PSS."""

    def __init__(self, period_s: float = 0.02, pss_every: int = 10) -> None:
        self.peak = Held()
        self._period_s = period_s
        self._pss_every = pss_every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        tick = 0
        while not self._stop.is_set():
            live = list(solver_mod._workers()._live)
            states = [_state(w.process.pid) for w in live]
            p = self.peak
            p.live = max(p.live, len(live))
            p.stopped = max(p.stopped, states.count("T"))
            p.running = max(p.running, states.count("R"))
            p.threads = max(p.threads, threading.active_count())
            if self._pss_every and tick % self._pss_every == 0:
                pss = sum(_pss_kb(w.process.pid) for w in live) / 1024
                p.pss_mb = max(p.pss_mb, pss)
            tick += 1
            self._stop.wait(self._period_s)

    def __enter__(self) -> HeldSampler:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()


async def burst_held(
    n: int, timeout_s: float, shorts: int, workers: int = 2, slow: int = 2
) -> Held:
    """`n` new tasks each make one runs-to-timeout check at once, as a burst of LangGraph
    runs does, beside `shorts` short callers; peaks of what the pool holds meanwhile."""
    ex = SolverExecutor(workers, slow)
    tasks: list[asyncio.Task[float]] = []
    try:
        await ex.run(_easy)
        with HeldSampler() as sampler:
            async with ShortStream(ex, shorts):
                started = time.perf_counter()
                tasks = [
                    asyncio.create_task(_timed(partial(ex.run, _hard(timeout_s)))) for _ in range(n)
                ]
                walls = await asyncio.gather(*tasks)
                sampler.peak.total_s = time.perf_counter() - started
                sampler.peak.max_wall_s = max(walls)
        return sampler.peak
    finally:
        await _cancel(tasks)
        ex.shutdown()


async def section_held() -> None:
    for n, shorts in ((16, 0), (16, 2), (64, 0), (64, 2), (128, 0)):
        h = await burst_held(n, 1.0, shorts)
        print(
            f"held 2+2, {n} fresh hard checks at 1.0 s, {shorts} short loops: all done in "
            f"{h.total_s:.1f} s (4 places: {n / 4:.1f}); peak worker processes {h.live}, "
            f"stopped {h.stopped}, running {h.running}; threads {h.threads}; "
            f"worker PSS {h.pss_mb:.0f} MB"
        )


# --- #4: a watchdog thread per check -----------------------------------------------------


def _bare_solve(smt2: str, premises: int, true_claim: bool, timeout_s: float) -> SolverOutcome:
    """`solver._solve` without its `_Watchdog`: the same work, no thread."""
    deadline = solver_mod._now() + timeout_s
    z3.reset_params()
    ctx = z3.Context()
    return solver_mod._decide(ctx, smt2, premises, true_claim, deadline)


@dataclass
class WatchdogCost:
    """Medians of `_solve` with and without its watchdog, interleaved in one process."""

    with_ms: float
    without_ms: float
    extra_ms: float


def watchdog_cost(runs: int = 400) -> WatchdogCost:
    """An easy check decided in-process by `_solve` and by `_bare_solve`, interleaved, so
    load falls on both alike."""
    args = Z3Solver()._args(*EASY)
    with_s: list[float] = []
    without_s: list[float] = []
    for _ in range(runs):
        started = time.perf_counter()
        solver_mod._solve(*args)
        with_s.append(time.perf_counter() - started)
        started = time.perf_counter()
        _bare_solve(*args)
        without_s.append(time.perf_counter() - started)
    return WatchdogCost(
        statistics.median(with_s) * 1e3,
        statistics.median(without_s) * 1e3,
        (sum(with_s) - sum(without_s)) / runs * 1e3,
    )


def watchdog_threads(checks: int) -> int:
    """Threads named `z3-watchdog` started while `_solve` decides `checks` easy checks."""
    started = [0]
    start = threading.Thread.start

    def counted(self: threading.Thread) -> None:
        if self.name.startswith("z3-watchdog"):
            started[0] += 1
        start(self)

    args = Z3Solver()._args(*EASY)
    threading.Thread.start = counted  # type: ignore[method-assign]
    try:
        for _ in range(checks):
            solver_mod._solve(*args)
    finally:
        threading.Thread.start = start  # type: ignore[method-assign]
    return started[0]


async def section_watchdog() -> None:
    for _ in range(3):
        c = watchdog_cost()
        print(
            f"watchdog: _solve median {c.with_ms:.2f} ms with its thread, {c.without_ms:.2f} ms"
            f" without; mean extra {c.extra_ms:.2f} ms per check"
        )
    print(f"watchdog: threads started for 20 checks: {watchdog_threads(20)}")


# --- #5: AUDIT-30 #3's ratio under load ------------------------------------------------------


async def section_vocab() -> None:
    out = []
    for _ in range(10):
        alone, loaded = merge_into_vocab_s(0), merge_into_vocab_s(20_000)
        out.append(f"{loaded / alone:.1f} ({alone * 1e3:.0f}/{loaded * 1e3:.0f} ms)")
    print(f"vocab: loaded/alone {' '.join(out)}")


# --- the pause-aware clock --------------------------------------------------------------------


def paused_through_worker(timeout_s: float, cycles: int, stop_s: float, run_s: float) -> str:
    """A runs-to-timeout check paused `cycles` times through `Worker.pause`/`resume`."""
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
    thread = threading.Thread(target=lambda: out.append(_hard(timeout_s)()))
    thread.start()
    paused = 0.0
    try:
        time.sleep(0.05)
        for _ in range(cycles):
            if not thread.is_alive():
                break
            picked[0].pause()
            at = time.perf_counter()
            time.sleep(stop_s)
            picked[0].resume()
            paused += time.perf_counter() - at
            time.sleep(run_s)
        thread.join()
    finally:
        pool.acquire = acquire  # type: ignore[method-assign]
    wall = time.perf_counter() - started
    return (
        f"{timeout_s} s check, {cycles} pauses: {out[0].details!r} in {wall:.3f} s, "
        f"paused {paused:.3f} s, so {wall - paused:.3f} s unpaused"
    )


async def section_drift() -> None:
    for cfg in (
        (1.0, 0, 0.0, 0.0),
        (1.0, 1, 0.5, 0.0),
        (1.0, 50, 0.01, 0.01),
        (1.0, 400, 0.002, 0.001),
    ):
        print(f"drift: {paused_through_worker(*cfg)}")


SECTIONS: Final[dict[str, Callable[[], Awaitable[None]]]] = {
    "lender": section_lender,
    "giveway": section_giveway,
    "held": section_held,
    "watchdog": section_watchdog,
    "vocab": section_vocab,
    "drift": section_drift,
}


async def amain(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sections", nargs="*")
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
