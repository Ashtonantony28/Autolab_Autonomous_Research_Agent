"""Z3 wrapper for entailment checking.

Each step is verified in two checks. First the premises alone must be
satisfiable; an unsat premise set would entail anything (ex falso), so it is
INVALID with reason `premises_inconsistent`. Then Z3 is asked about
`premises and not claim` — if `unsat`, the claim is entailed (VALID); if
`sat`, there's a model that satisfies the premises but contradicts the claim
(INVALID); if `unknown` (timeout or undecidable), we conservatively return
UNGROUNDED.

Checks run on `SolverExecutor`, a bounded thread pool separate from asyncio's
default executor (AUDIT-2 #1). z3py drops the GIL around every C call, so
checks on several threads convoy on it; the default pool is small (AUDIT-10 #1).
A check still in Z3 after `_SLICE_S` waits on Z3, not on the GIL, so it gives
its slot to the next caller and runs on as one of at most `slow_workers` slow
checks: hard runs cannot starve easy ones (AUDIT-14 #2). With no slow room
left, its slot still admits a caller whose last check was short (AUDIT-18 #4).
The pool starts a thread for every admitted check that finds none idle
(AUDIT-18 #2). Cancelling a caller interrupts its Z3 check, including one that
has not started yet (AUDIT-12 #2).

Inputs are SMT-LIB 2 strings, parsed by `Solver.from_string` so the
boundary between LLM-produced text and Z3's AST is explicit. Every check
runs in a fresh `z3.Context`, created from default global parameters, so
checks are safe to run on worker threads, and both checks share one
`timeout_seconds` deadline.
"""

from __future__ import annotations

import asyncio
import os
import queue
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Final, TypeVar

import z3

from autolab.config import SOLVER_TIMEOUT_S
from autolab.state.models import StepReason, Verdict

_MS_PER_S: Final[int] = 1000


def _available_cpus() -> int:
    """CPUs this process may run on: its affinity where known, not the host's count."""
    process_cpu_count: Callable[[], int | None] | None = getattr(os, "process_cpu_count", None)
    if process_cpu_count is not None:
        return process_cpu_count() or 1
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or 1  # pragma: no cover - platforms without affinity


_CPUS: Final[int] = _available_cpus()
SOLVER_WORKERS: Final[int] = min(2, _CPUS)
"""Default size of the Z3 pool; more threads only contend for the GIL (AUDIT-10 #1)."""
SOLVER_SLOW_WORKERS: Final[int] = max(0, _CPUS - SOLVER_WORKERS)
"""Default number of long checks that may run beside the pool's own slots (AUDIT-14 #2).
Long checks are CPU-bound and timeouts are wall-clock, so slots and slow checks
together do not outnumber the CPUs (AUDIT-18 #5)."""
_SLICE_S: Final[float] = 0.05
"""How long a Z3 check holds its slot before it runs on as a slow check."""
_SETUP_SHARE: Final[float] = 0.5
"""Z3 sets up the entailment check, without polling its limit, in up to this share of
the time the consistency check took; with less time left the check is not started."""
_NEGATED_CLAIM: Final[str] = "claim!"
"""Not a plain identifier, so no formalized step can declare it."""
_REARM_S: Final[float] = 0.005
"""How often a requested interrupt is re-sent: Z3 drops one sent outside `Solver.check()`."""

T = TypeVar("T")


@dataclass(frozen=True)
class SmtFormula:
    """An SMT-LIB chunk plus the declarations it depends on.

    `declarations` is the cumulative `(declare-const ...)` /
    `(declare-fun ...)` block that defines every symbol used by the
    `assertion`. Keeping them separate lets the verifier compose premises
    and claims without re-parsing the full prior history.
    """

    declarations: str
    assertion: str


@dataclass(frozen=True)
class SolverOutcome:
    """Result of an entailment check."""

    verdict: Verdict
    reason: StepReason
    details: str = ""
    counter_model: str = ""


@dataclass
class Z3Solver:
    """Stateless Z3-backed entailment checker; safe to call from any thread."""

    timeout_seconds: float = SOLVER_TIMEOUT_S

    def check_entailment(
        self,
        premises: list[SmtFormula],
        claim: SmtFormula,
    ) -> SolverOutcome:
        """Return whether `claim` is entailed by `premises`.

        Conventions:
          - No premises → UNGROUNDED.
          - premises alone unsat → INVALID (`premises_inconsistent`).
          - premises and not claim is unsat → VALID.
          - premises and not claim is sat   → INVALID.
          - Any solver error / unknown / timeout → UNGROUNDED.
        """
        if not premises:
            return SolverOutcome(
                verdict="UNGROUNDED", reason="no_premises", details="no premises to entail from"
            )

        deadline = time.monotonic() + self.timeout_seconds
        z3.reset_params()  # the context below starts from default parameters (AUDIT-9 #2)
        ctx = z3.Context()
        interrupt: _Interrupt = getattr(_worker, "interrupt", None) or _Interrupt()
        if not interrupt.attach(ctx):
            return _INTERRUPTED
        try:
            return self._check(premises, claim, ctx, deadline, interrupt)
        finally:
            interrupt.detach()

    def _check(
        self,
        premises: list[SmtFormula],
        claim: SmtFormula,
        ctx: z3.Context,
        deadline: float,
        interrupt: _Interrupt,
    ) -> SolverOutcome:
        solver = z3.Solver(ctx=ctx)
        try:
            # One C call; asserting the parsed AstVector item by item costs a GIL
            # hand-off per call (AUDIT-10 #1).
            solver.from_string(_build_smt2(premises, claim))
        except z3.Z3Exception as exc:
            if interrupt.requested():
                return _INTERRUPTED
            return SolverOutcome(
                verdict="UNGROUNDED",
                reason="formalization_failed",
                details=f"SMT parse error: {exc}",
            )
        solver.set("timeout", _remaining_ms(deadline))
        started = time.monotonic()
        consistency = solver.check()
        took = time.monotonic() - started
        if consistency == z3.unsat:
            return SolverOutcome(
                verdict="INVALID",
                reason="premises_inconsistent",
                details="premises are mutually unsatisfiable",
            )
        details = f"premise consistency check returned {consistency}"
        if consistency == z3.sat and deadline - time.monotonic() < _SETUP_SHARE * took:
            # Z3 sets the entailment check up without polling its limit (AUDIT-14 #1).
            details = "too little time left for the entailment check"
        elif consistency == z3.sat:
            # Asserted, not assumed: Z3 checks LIA under an assumption in a phase that
            # ignores both the timeout and interrupts (AUDIT-14 #1).
            solver.add(z3.Bool(_NEGATED_CLAIM, ctx))
            solver.set("timeout", _remaining_ms(deadline))
            result = solver.check()
            if result == z3.unsat:
                return SolverOutcome(
                    verdict="VALID", reason="entailed", details="premises entail claim"
                )
            if result == z3.sat:
                return SolverOutcome(
                    verdict="INVALID",
                    reason="not_entailed",
                    details="premises do not entail claim",
                    # `sexpr` is formatted in C; z3py's printer keeps global state (AUDIT-10 #5).
                    counter_model=solver.model().sexpr(),
                )
            details = f"solver returned {result}"
        if interrupt.requested():
            return _INTERRUPTED
        return SolverOutcome(verdict="UNGROUNDED", reason="timeout", details=details)


_INTERRUPTED: Final = SolverOutcome(verdict="UNGROUNDED", reason="timeout", details="interrupted")


def _remaining_ms(deadline: float) -> int:
    """Milliseconds left before `deadline`, at least 1 (Z3 treats 0 as no limit)."""
    return max(1, int((deadline - time.monotonic()) * _MS_PER_S))


def _build_smt2(premises: list[SmtFormula], claim: SmtFormula) -> str:
    """Compose a single SMT-LIB 2 document from premises + negated claim.

    The negated claim is named by `_NEGATED_CLAIM`, so it holds only once the
    entailment check asserts that name. Declarations are deduplicated
    line-by-line so identical declares across premises don't conflict. Order
    is preserved (first occurrence wins).
    """
    seen: set[str] = set()
    decls: list[str] = []
    for chunk in (*premises, claim):
        for line in chunk.declarations.splitlines():
            stripped = line.strip()
            if not stripped or stripped in seen:
                continue
            seen.add(stripped)
            decls.append(stripped)

    parts: list[str] = [*decls]
    for p in premises:
        parts.append(_wrap_assert(p.assertion))
    parts.append(f"(declare-const {_NEGATED_CLAIM} Bool)")
    parts.append(f"(assert (= {_NEGATED_CLAIM} (not {claim.assertion})))")
    return "\n".join(parts)


def _wrap_assert(body: str) -> str:
    return f"(assert {body.strip()})"


class _Interrupt:
    """Lets the event loop stop the Z3 check running on one worker thread.

    Z3 ignores `Context.interrupt()` while no `Solver.check()` runs, so once
    requested the interrupt is re-sent every `_REARM_S` until the check ends.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ctx: z3.Context | None = None
        self._requested = False
        self._detached = threading.Event()

    def attached(self) -> bool:
        """Whether a Z3 context is attached, that is, a check is in progress."""
        return self._ctx is not None

    def requested(self) -> bool:
        """Whether an interrupt has been requested."""
        return self._requested

    def request(self) -> None:
        """Interrupt the attached context until it is detached, or refuse the next attach."""
        with self._lock:
            if self._requested:
                return
            self._requested = True
            if self._ctx is None:
                return
            self._ctx.interrupt()
        threading.Thread(target=self._rearm, name="z3-interrupt", daemon=True).start()

    def attach(self, ctx: z3.Context) -> bool:
        """Attach `ctx`; False, attaching nothing, if an interrupt was already requested."""
        with self._lock:
            if self._requested:
                return False
            self._ctx = ctx
            self._detached.clear()
            return True

    def detach(self) -> None:
        """Detach the context; no interrupt reaches it afterwards."""
        with self._lock:
            self._ctx = None
        self._detached.set()

    def _rearm(self) -> None:
        while not self._detached.wait(_REARM_S):
            with self._lock:
                if self._ctx is not None:
                    self._ctx.interrupt()


_worker = threading.local()


class _Job:
    """One call submitted to `_Pool`."""

    __slots__ = ("args", "fn", "future")

    def __init__(self, fn: Callable[..., Any], args: tuple[Any, ...]) -> None:
        self.future: Future[Any] = Future()
        self.fn = fn
        self.args = args

    def run(self) -> None:
        if not self.future.set_running_or_notify_cancel():
            return
        try:
            result = self.fn(*self.args)
        except BaseException as exc:  # handed to the caller, as `ThreadPoolExecutor` does
            self.future.set_exception(exc)
        else:
            self.future.set_result(result)


class _Pool:
    """Worker threads for `SolverExecutor`, at most `_max_workers` of them.

    Under one lock, every submitted call is matched with an idle thread, a new
    thread, or, at the limit, the next thread to finish a call. So a call never
    waits for a thread while one is idle or the limit allows another (AUDIT-18 #2).
    """

    def __init__(self, max_workers: int) -> None:
        self._max_workers = max_workers
        self._lock = threading.Lock()
        self._work_queue: queue.SimpleQueue[_Job | None] = queue.SimpleQueue()
        self._threads: set[threading.Thread] = set()
        self._idle = 0
        self._unclaimed = 0
        self._shutdown = False

    def submit(self, fn: Callable[..., T], /, *args: Any) -> Future[T]:
        """Run `fn(*args)` on a worker thread; RuntimeError after `shutdown`."""
        job = _Job(fn, args)
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            self._work_queue.put(job)
            self._unclaimed += 1
            self._claim()
        return job.future

    def resize(self, delta: int) -> None:
        """Move the thread limit by `delta`; a raised limit starts threads for queued calls."""
        with self._lock:
            self._max_workers += delta
            self._claim()

    def shutdown(self) -> None:
        """Refuse new calls, let queued ones run, and join every thread."""
        with self._lock:
            self._shutdown = True
            threads = list(self._threads)
            for _ in threads:
                self._work_queue.put(None)
        for thread in threads:
            thread.join()

    def _claim(self) -> None:
        while self._unclaimed and self._idle:
            self._unclaimed -= 1
            self._idle -= 1
        while self._unclaimed and len(self._threads) < self._max_workers:
            self._unclaimed -= 1
            thread = threading.Thread(target=self._work, name="z3", daemon=True)
            self._threads.add(thread)
            thread.start()

    def _work(self) -> None:
        while (job := self._work_queue.get()) is not None:
            job.run()
            del job
            if not self._next():
                return

    def _next(self) -> bool:
        """After a call: take an unclaimed one or count as idle; False ends the thread."""
        with self._lock:
            if self._unclaimed:
                self._unclaimed -= 1
                return True
            if len(self._threads) > self._max_workers:
                self._threads.discard(threading.current_thread())
                return False
            self._idle += 1
            return True


class _Slots:
    """Admission for one event loop: `workers` slots, granted in FIFO order.

    A slot whose Z3 check is stuck (past `_SLICE_S` with no slow room) also lends
    a place to one caller whose last check ended within its slice, so easy runs
    keep moving however many hard runs there are (AUDIT-18 #4).
    """

    def __init__(self, workers: int) -> None:
        self.workers = workers
        self.held = 0
        self.stuck = 0
        self.lent = 0
        self._waiters: deque[tuple[asyncio.Future[bool], bool]] = deque()

    async def acquire(self, short: bool) -> bool:
        """Wait for a place; True if a stuck slot lent it."""
        lent = self._grant(short)
        if lent is not None:
            return lent
        waiter: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        entry = (waiter, short)
        self._waiters.append(entry)
        try:
            return await waiter
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled():
                self.release(waiter.result())
            else:
                self._waiters.remove(entry)
            raise

    def release(self, lent: bool) -> None:
        if lent:
            self.lent -= 1
        else:
            self.held -= 1
        self._wake()

    def stick(self) -> None:
        self.stuck += 1
        self._wake()

    def unstick(self) -> None:
        self.stuck -= 1

    def _grant(self, short: bool) -> bool | None:
        if self.held < self.workers:
            self.held += 1
            return False
        if short and self.lent < self.stuck:
            self.lent += 1
            return True
        return None

    def _wake(self) -> None:
        for entry in list(self._waiters):
            waiter, short = entry
            if waiter.done():  # cancelled; its caller removes it
                continue
            lent = self._grant(short)
            if lent is not None:
                self._waiters.remove(entry)
                waiter.set_result(lent)


class SolverExecutor:
    """A bounded thread pool for Z3 checks, separate from asyncio's default executor.

    At most `workers` checks hold a slot at once; other callers wait as
    coroutines on a per-loop `_Slots`. A Z3 check still running after
    `_SLICE_S` frees its slot while fewer than `slow_workers` checks have done
    so; otherwise its slot lends a place to callers whose last check was short.
    The pool starts a thread for each such check. Cancelling a caller
    interrupts its check.
    """

    def __init__(
        self, workers: int = SOLVER_WORKERS, slow_workers: int = SOLVER_SLOW_WORKERS
    ) -> None:
        self.workers = workers
        self.slow_workers = slow_workers
        self._pool = _Pool(workers)
        self._slow = 0
        self._closed = False
        self._lock = threading.Lock()
        self._slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Slots] = (
            weakref.WeakKeyDictionary()
        )
        self._running: set[_Interrupt] = set()
        self._short: weakref.WeakSet[asyncio.Task[Any]] = weakref.WeakSet()

    async def run(self, fn: Callable[[], T]) -> tuple[T, float]:
        """Run `fn` on the pool; return its result and its run time in ms, queueing excluded."""
        slots = self._slot()
        task = asyncio.current_task()
        with self._lock:
            short = task in self._short
        lent = await slots.acquire(short)
        slow = stuck = long = False
        try:
            if lent:
                self._pool.resize(1)
            interrupt = _Interrupt()
            future = asyncio.wrap_future(self._pool.submit(self._call, fn, interrupt))
            try:
                while not slow and not (await asyncio.wait((future,), timeout=_SLICE_S))[0]:
                    long = True
                    slow = self._to_slow(interrupt)
                    if slow:
                        if stuck:
                            slots.unstick()
                        slots.release(lent)
                    elif not (stuck or lent) and interrupt.attached():
                        stuck = True
                        slots.stick()
                return await future
            except asyncio.CancelledError:
                future.cancel()
                interrupt.request()
                raise
        finally:
            if slow:
                with self._lock:
                    self._slow -= 1
                self._pool.resize(-1)
            else:
                if stuck:
                    slots.unstick()
                slots.release(lent)
            if lent:
                self._pool.resize(-1)
            if task is not None:
                with self._lock:
                    if long:
                        self._short.discard(task)
                    else:
                        self._short.add(task)

    def _to_slow(self, interrupt: _Interrupt) -> bool:
        """Count a running Z3 check as slow if there is room; False leaves it in its slot."""
        with self._lock:
            if self._slow >= self.slow_workers or not interrupt.attached():
                return False
            self._slow += 1
            # A thread for the next caller, so it does not wait for this check.
            self._pool.resize(1)
            return True

    def shutdown(self) -> None:
        """Interrupt running and queued checks and join the worker threads.

        Queued calls still run, so their callers get a result, not a cancellation
        (AUDIT-6 #7); a Z3 check among them returns at once as interrupted. Later
        calls raise RuntimeError.
        """
        with self._lock:
            self._closed = True
            running = list(self._running)
        for interrupt in running:
            interrupt.request()
        self._pool.shutdown()

    def _slot(self) -> _Slots:
        loop = asyncio.get_running_loop()
        with self._lock:
            slots = self._slots.get(loop)
            if slots is None:
                slots = self._slots[loop] = _Slots(self.workers)
            return slots

    def _call(self, fn: Callable[[], T], interrupt: _Interrupt) -> tuple[T, float]:
        with self._lock:
            self._running.add(interrupt)
            if self._closed:
                interrupt.request()
        _worker.interrupt = interrupt
        started = time.perf_counter()
        try:
            return fn(), (time.perf_counter() - started) * _MS_PER_S
        finally:
            _worker.interrupt = None
            with self._lock:
                self._running.discard(interrupt)


_default_executor: SolverExecutor | None = None
_default_lock = threading.Lock()


def default_executor() -> SolverExecutor:
    """The process-wide `SolverExecutor`, created on first use."""
    global _default_executor
    with _default_lock:
        if _default_executor is None:
            _default_executor = SolverExecutor()
        return _default_executor


def shutdown_default_executor() -> None:
    """Shut down the process-wide executor; the next `default_executor()` makes a new one."""
    global _default_executor
    with _default_lock:
        executor, _default_executor = _default_executor, None
    if executor is not None:
        executor.shutdown()


__all__ = [
    "SOLVER_SLOW_WORKERS",
    "SOLVER_WORKERS",
    "SmtFormula",
    "SolverExecutor",
    "SolverOutcome",
    "Z3Solver",
    "default_executor",
    "shutdown_default_executor",
]
