"""Z3 wrapper for entailment checking.

Each step is verified in two checks. First the premises alone must be
satisfiable; an unsat premise set would entail anything (ex falso), so it is
INVALID with reason `premises_inconsistent`. Then Z3 is asked about
`premises and not claim` — if `unsat`, the claim is entailed (VALID); if
`sat`, there's a model that satisfies the premises but contradicts the claim
(INVALID); if `unknown` (timeout or undecidable), we conservatively return
UNGROUNDED.

Every check runs in a persistent worker process (`solver_workers`, ADR 0009):
the parent sends the SMT-LIB document and the timeout, and `_solve` decides it
there in a fresh `z3.Context`. A check still running `_KILL_GRACE_SHARE` past
its timeout, or whose caller is cancelled, is ended by killing its worker, so
timeouts and cancels hold for any formula shape, and solving never holds the
parent's GIL (AUDIT-22 #2, #3). A killed check is never VALID.

Checks are scheduled by `SolverExecutor`, a bounded thread pool separate from
asyncio's default executor (AUDIT-2 #1); each of its threads waits on one
worker. A check still running after `_SLICE_S` gives its slot to the next
caller and runs on as one of at most `slow_workers` slow checks: hard runs
cannot starve easy ones (AUDIT-14 #2). With no slow room left, its slot still
admits a caller whose last check was short (AUDIT-18 #4), and only a slot can
lend (AUDIT-24 #3). A run is hard by its check's run time (AUDIT-24 #2), and a
caller passed over `workers` times is served next (AUDIT-22 #4). The pool starts
a thread for every admitted check that finds none idle (AUDIT-18 #2).

Inputs are SMT-LIB 2 strings, parsed by Z3 in one call so the boundary
between LLM-produced text and Z3's AST is explicit. Z3's simplex on a long pool
of Real bounds is many times slower in one call, so such a pool of more than
`_LOAD_ABOVE` premises is asserted one formula at a time, each after a check
that keeps the simplex feasible (AUDIT-18 #3); in a worker that costs no GIL
hand-offs. The claim `true` needs no entailment check (AUDIT-18 #7).
"""

from __future__ import annotations

import asyncio
import itertools
import math
import os
import queue
import re
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Final, Protocol, TypeVar

import z3

from autolab.config import SOLVER_TIMEOUT_S
from autolab.state.models import StepReason, Verdict
from autolab.verification import solver_workers

_MS_PER_S: Final[int] = 1000

_CGROUP_V2: Final[tuple[str, ...]] = ("/sys/fs/cgroup/cpu.max",)
_CGROUP_V1: Final[tuple[tuple[str, str], ...]] = tuple(
    (f"/sys/fs/cgroup/{d}/cpu.cfs_quota_us", f"/sys/fs/cgroup/{d}/cpu.cfs_period_us")
    for d in ("cpu", "cpu,cpuacct")
)


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _quota(quota: str, period: str) -> int | None:
    """CPUs a quota allows, rounded up; None for no quota."""
    try:
        q, p = int(quota), int(period)
    except ValueError:
        return None
    return math.ceil(q / p) if q > 0 and p > 0 else None


def _own_cgroup_files() -> tuple[list[str], list[tuple[str, str]]]:
    """This process's own cgroup quota files, for hosts without a cgroup namespace."""
    v2: list[str] = []
    v1: list[tuple[str, str]] = []
    for line in (_read("/proc/self/cgroup") or "").splitlines():
        _, controllers, path = [*line.split(":", 2), "", ""][:3]
        if path in ("", "/"):
            continue
        if not controllers:
            v2.append(f"/sys/fs/cgroup{path}/cpu.max")
        elif "cpu" in controllers.split(","):
            base = f"/sys/fs/cgroup/{controllers}{path}"
            v1.append((f"{base}/cpu.cfs_quota_us", f"{base}/cpu.cfs_period_us"))
    return v2, v1


def _cgroup_cpus() -> int | None:
    """CPUs the cgroup CPU quota allows (v2 `cpu.max`, v1 cfs quota/period), if any."""
    own_v2, own_v1 = _own_cgroup_files()
    found: list[int] = []
    for path in (*_CGROUP_V2, *own_v2):
        text = _read(path)
        if text is not None:
            quota, _, period = text.strip().partition(" ")
            if (cpus := _quota(quota, period or "100000")) is not None:
                found.append(cpus)
    for quota_path, period_path in (*_CGROUP_V1, *own_v1):
        quota_text, period_text = _read(quota_path), _read(period_path)
        if quota_text is None or period_text is None:
            continue
        if (cpus := _quota(quota_text.strip(), period_text.strip())) is not None:
            found.append(cpus)
    return min(found) if found else None


def _available_cpus() -> int:
    """CPUs this process may use: its affinity and its cgroup quota (AUDIT-22 #6)."""
    process_cpu_count: Callable[[], int | None] | None = getattr(os, "process_cpu_count", None)
    if process_cpu_count is not None:
        cpus = process_cpu_count() or 1
    elif hasattr(os, "sched_getaffinity"):
        cpus = len(os.sched_getaffinity(0))
    else:  # pragma: no cover - platforms without affinity
        cpus = os.cpu_count() or 1
    quota = _cgroup_cpus()
    return max(1, min(cpus, quota)) if quota is not None else cpus


_CPUS: Final[int] = _available_cpus()
SOLVER_WORKERS: Final[int] = min(2, _CPUS)
"""Default size of the Z3 pool (AUDIT-10 #1)."""
SOLVER_SLOW_WORKERS: Final[int] = max(0, _CPUS - SOLVER_WORKERS)
"""Default number of long checks that may run beside the pool's own slots (AUDIT-14 #2).
Long checks are CPU-bound and timeouts are wall-clock, so slots and slow checks
together do not outnumber the CPUs (AUDIT-18 #5)."""
_SLICE_S: Final[float] = 0.05
"""How long a Z3 check holds its slot before it runs on as a slow check."""
_HARD_SLICES: Final[int] = 4
"""A caller whose last check ran this many slices of time loses its priority and lent
places; an easy check slowed by load stays well under it (AUDIT-18 #4, AUDIT-24 #2)."""
_SETUP_SHARE: Final[float] = 0.5
"""Z3 sets up the entailment check, without polling its limit, in up to this share of
the time the consistency check took; with less time left the check is not started."""
_NEGATED_CLAIM: Final[str] = "claim!"
"""Not a plain identifier, so no formalized step can declare it."""
_LOAD_ABOVE: Final[int] = 100
"""Real pools of more premises are loaded one formula at a time; others in one call."""
_REAL: Final[re.Pattern[str]] = re.compile(r"\bReal\b")
_RESET_SHARE: Final[float] = 0.9
"""While loading, the timeout is set again once less than this share of it is left."""
_KILL_GRACE_SHARE: Final[float] = 0.1
"""A worker still running this share of its timeout past it is killed."""
_KILL_GRACE_MIN_S: Final[float] = 0.01
"""The least grace, so a short check is not killed for scheduling noise."""
_IDLE_WORKERS: Final[int] = max(2, 2 * SOLVER_WORKERS + SOLVER_SLOW_WORKERS)
"""Worker processes kept idle: every slot, slow check and lent place of the default pool."""

T = TypeVar("T")


def _clock() -> float:
    """The clock a check's run time is measured by, on its pool thread."""
    return time.perf_counter()


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


class SolverWorkerError(RuntimeError):
    """A check raised in its worker process; the message is the worker's error."""


@dataclass
class Z3Solver:
    """Stateless Z3-backed entailment checker; safe to call from any thread."""

    timeout_seconds: float = SOLVER_TIMEOUT_S

    def check_entailment(
        self,
        premises: list[SmtFormula],
        claim: SmtFormula,
    ) -> SolverOutcome:
        """Return whether `claim` is entailed by `premises`, decided in a worker process.

        Conventions:
          - No premises → UNGROUNDED.
          - premises alone unsat → INVALID (`premises_inconsistent`).
          - premises and not claim is unsat → VALID.
          - premises and not claim is sat   → INVALID.
          - Any solver error / unknown / timeout / killed worker → UNGROUNDED.

        Raises:
            SolverWorkerError: the check raised in its worker.
        """
        if not premises:
            return _NO_PREMISES
        args = self._args(premises, claim)
        interrupt: _Interrupt = getattr(_worker, "interrupt", None) or _Interrupt()
        if interrupt.requested():
            return _INTERRUPTED
        pool = _workers()
        worker = pool.acquire()
        if not interrupt.attach(worker):
            pool.release(worker)
            return _INTERRUPTED
        try:
            reply = worker.call(args, self.timeout_seconds + _grace_s(self.timeout_seconds))
        except BaseException:  # e.g. a per-test timeout's signal: the worker is mid-check
            interrupt.detach()
            pool.discard(worker)
            raise
        interrupt.detach()
        if reply is None or interrupt.requested():
            pool.discard(worker)
        else:
            pool.release(worker)
        if reply is None:
            return _INTERRUPTED if interrupt.requested() else _KILLED
        ok, value = reply
        if not ok:
            raise SolverWorkerError(value)
        outcome: SolverOutcome = value
        return outcome

    def _check_here(self, premises: list[SmtFormula], claim: SmtFormula) -> SolverOutcome:
        """`check_entailment` decided in this process, as its worker decides it (tests)."""
        if not premises:
            return _NO_PREMISES
        return _solve(*self._args(premises, claim))

    def _args(self, premises: list[SmtFormula], claim: SmtFormula) -> tuple[str, int, bool, float]:
        return (
            _build_smt2(premises, claim),
            len(premises),
            claim.assertion.strip() == "true",
            self.timeout_seconds,
        )


_NO_PREMISES: Final = SolverOutcome(
    verdict="UNGROUNDED", reason="no_premises", details="no premises to entail from"
)
_INTERRUPTED: Final = SolverOutcome(verdict="UNGROUNDED", reason="timeout", details="interrupted")
_KILLED: Final = SolverOutcome(
    verdict="UNGROUNDED", reason="timeout", details="worker killed past the timeout"
)


def _grace_s(timeout_s: float) -> float:
    """How long past its timeout a check's worker may run before it is killed."""
    return max(_KILL_GRACE_MIN_S, _KILL_GRACE_SHARE * timeout_s)


def _solve(smt2: str, premises: int, true_claim: bool, timeout_s: float) -> SolverOutcome:
    """Decide `smt2` (`premises` assertions, then the negated claim's definition).

    Runs in a worker process. Every check gets a fresh context from default global
    parameters (AUDIT-9 #2), and both checks share one `timeout_s` deadline.
    """
    deadline = time.monotonic() + timeout_s
    z3.reset_params()
    ctx = z3.Context()
    solver = z3.Solver(ctx=ctx)
    loading: z3.AstVector | None = None
    try:
        if _loaded(smt2, premises):
            loading = z3.parse_smt2_string(smt2, ctx=ctx)
        else:
            solver.from_string(smt2)
    except z3.Z3Exception as exc:
        return SolverOutcome(
            verdict="UNGROUNDED",
            reason="formalization_failed",
            details=f"SMT parse error: {exc}",
        )
    consistency = z3.sat if loading is None else _load(solver, loading, deadline)
    started = time.monotonic()
    if consistency == z3.sat:
        solver.set("timeout", _remaining_ms(deadline))
        consistency = solver.check()
    took = time.monotonic() - started
    if consistency == z3.unsat:
        return SolverOutcome(
            verdict="INVALID",
            reason="premises_inconsistent",
            details="premises are mutually unsatisfiable",
        )
    details = f"premise consistency check returned {consistency}"
    if consistency == z3.sat and true_claim:
        # Anything entails `true`: no entailment check, so no guard (AUDIT-18 #7).
        return SolverOutcome(verdict="VALID", reason="entailed", details="premises entail claim")
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
    return SolverOutcome(verdict="UNGROUNDED", reason="timeout", details=details)


def _loaded(smt2: str, premises: int) -> bool:
    """Whether `_solve` loads the pool one formula at a time: long pools over Reals.

    Other pools are cheapest in one call (AUDIT-22 #2); a pool loaded either way
    reaches the same verdict, and the worker's deadline bounds both.
    """
    return premises > _LOAD_ABOVE and _REAL.search(smt2) is not None


def _remaining_ms(deadline: float) -> int:
    """Milliseconds left before `deadline`, at least 1 (Z3 treats 0 as no limit)."""
    return max(1, int((deadline - time.monotonic()) * _MS_PER_S))


def _load(solver: z3.Solver, assertions: z3.AstVector, deadline: float) -> z3.CheckSatResult:
    """Assert `assertions` one at a time, checking before each but the first.

    One new bound at a time keeps every simplex run short, and the deadline is
    polled between them (AUDIT-18 #3). The checks go through the C API: they
    only keep the simplex feasible, and the caller's `Solver.check()` is the
    consistency check.
    Returns unsat once a prefix is unsat, unknown once the deadline passes, else
    sat (all asserted, consistency not yet decided).
    """
    ref, handle, vector = solver.ctx.ref(), solver.solver, assertions.vector
    limit = 0
    for i in range(len(assertions)):
        if i:
            remaining = _remaining_ms(deadline)
            if remaining < _RESET_SHARE * limit or not limit:
                limit = remaining
                solver.set("timeout", limit)
            if z3.Z3_solver_check(ref, handle) == z3.Z3_L_FALSE:
                return z3.unsat
            if time.monotonic() >= deadline:
                return z3.unknown
        z3.Z3_solver_assert(ref, handle, z3.Z3_ast_vector_get(ref, vector, i))
    return z3.sat


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


_workers_lock = threading.Lock()
_shared_workers: solver_workers.Workers | None = None


def _workers() -> solver_workers.Workers:
    """The process-wide worker processes, created on first use."""
    global _shared_workers
    with _workers_lock:
        if _shared_workers is None:
            _shared_workers = solver_workers.register(
                solver_workers.Workers(
                    _solve, _IDLE_WORKERS, preload=("__main__", solver_workers.__name__, __name__)
                )
            )
        return _shared_workers


class _Stoppable(Protocol):
    def interrupt(self) -> None: ...


class _Interrupt:
    """Lets the event loop stop the check running on one pool thread.

    The attached target is the check's worker; `request` kills it, so the check
    stops wherever it is. The pool thread records when the call started and ended.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._target: _Stoppable | None = None
        self._requested = False
        self.started: float | None = None
        self.ended: float | None = None

    def ran_s(self) -> float:
        """How long the call has run, or ran; 0 before it starts."""
        if self.started is None:
            return 0.0
        return (_clock() if self.ended is None else self.ended) - self.started

    def attached(self) -> bool:
        """Whether a target is attached, that is, a check is in progress."""
        return self._target is not None

    def requested(self) -> bool:
        """Whether an interrupt has been requested."""
        return self._requested

    def request(self) -> None:
        """Stop the attached target, or refuse the next attach."""
        with self._lock:
            if self._requested:
                return
            self._requested = True
            if self._target is not None:
                self._target.interrupt()

    def attach(self, target: _Stoppable) -> bool:
        """Attach `target`; False, attaching nothing, if an interrupt was already requested."""
        with self._lock:
            if self._requested:
                return False
            self._target = target
            return True

    def detach(self) -> None:
        """Detach the target; no interrupt reaches it afterwards."""
        with self._lock:
            self._target = None


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


class _Waiter:
    __slots__ = ("future", "passed", "short")

    def __init__(self, future: asyncio.Future[bool], short: bool) -> None:
        self.future = future
        self.short = short
        self.passed = 0


class _Slots:
    """Admission for one event loop: `workers` slots, granted in FIFO order, callers
    whose last check was short first.

    A caller that later arrivals have overtaken for `workers` slots is served next,
    so every caller waits a bounded number of grants (AUDIT-22 #4). A slot whose Z3
    check is stuck (past `_SLICE_S` with no slow room) also lends a place to one
    caller whose last check was short, so easy runs keep moving however
    many hard runs there are (AUDIT-18 #4); a lent place never lends (AUDIT-24 #3).
    """

    def __init__(self, workers: int) -> None:
        self.workers = workers
        self.held = 0
        self.stuck = 0
        self.lent = 0
        self._waiters: deque[_Waiter] = deque()

    async def acquire(self, short: bool) -> bool:
        """Wait for a place; True if a stuck slot lent it."""
        lent = self._grant(short)
        if lent is not None:
            return lent
        entry = _Waiter(asyncio.get_running_loop().create_future(), short)
        self._waiters.append(entry)
        try:
            return await entry.future
        except asyncio.CancelledError:
            if entry.future.done() and not entry.future.cancelled():
                self.release(entry.future.result())
            else:
                self._waiters.remove(entry)
            raise

    def release(self, lent: bool) -> None:
        if lent:
            self.lent -= 1
        else:
            self.held -= 1
        self._wake()

    def stick(self, lent: bool) -> bool:
        """Count a slot's check as stuck, lending a place; False, counting nothing, if lent."""
        if lent:
            return False
        self.stuck += 1
        self._wake()
        return True

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

    def _rank(self, entry: _Waiter) -> int:
        return 0 if entry.passed >= self.workers else 1 if entry.short else 2

    def _wake(self) -> None:
        while True:
            for entry in sorted(self._waiters, key=self._rank):
                if entry.future.done():  # cancelled; its caller removes it
                    continue
                lent = self._grant(entry.short)
                if lent is not None:
                    break
            else:
                return
            index = self._waiters.index(entry)
            del self._waiters[index]
            entry.future.set_result(lent)
            if not lent:
                for ahead in itertools.islice(self._waiters, index):
                    ahead.passed += 1


class SolverExecutor:
    """A bounded thread pool for Z3 checks, separate from asyncio's default executor.

    At most `workers` checks hold a slot at once; other callers wait as
    coroutines on a per-loop `_Slots`. A Z3 check still running after
    `_SLICE_S` frees its slot while fewer than `slow_workers` checks have done
    so; otherwise its slot lends a place to callers whose last check was short.
    The pool starts a thread for each such check. Cancelling a caller
    kills its check's worker.
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
        # Worker processes start off the caller's thread and before the first check.
        threading.Thread(
            target=_workers().prestart, args=(workers + slow_workers,), daemon=True
        ).start()

    async def run(self, fn: Callable[[], T]) -> tuple[T, float]:
        """Run `fn` on the pool; return its result and its run time in ms, queueing excluded."""
        slots = self._slot()
        task = asyncio.current_task()
        with self._lock:
            short = task in self._short
        lent = await slots.acquire(short)
        slow = stuck = False
        interrupt = _Interrupt()
        try:
            if lent:
                self._pool.resize(1)
            job = self._pool.submit(self._call, fn, interrupt)
            future = asyncio.wrap_future(job)
            try:
                while not slow and not (await asyncio.wait((future,), timeout=_SLICE_S))[0]:
                    slow = self._to_slow(interrupt)
                    if slow:
                        if stuck:
                            slots.unstick()
                        slots.release(lent)
                    elif not (stuck or lent) and interrupt.attached():
                        stuck = slots.stick(lent)
                return await future
            except asyncio.CancelledError:
                future.cancel()
                interrupt.request()
                raise
        finally:
            if slow:
                # Given back when the check has ended (its worker is gone if it was
                # killed), not when its caller stops waiting (AUDIT-22 #5).
                job.add_done_callback(self._end_slow)
            else:
                if stuck:
                    slots.unstick()
                slots.release(lent)
            if lent:
                self._pool.resize(-1)
            if task is not None:
                with self._lock:
                    if interrupt.ran_s() >= _HARD_SLICES * _SLICE_S:
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

    def _end_slow(self, _: Future[Any]) -> None:
        with self._lock:
            self._slow -= 1
        self._pool.resize(-1)

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
        interrupt.started = started = _clock()
        try:
            result = fn()
        finally:
            interrupt.ended = ended = _clock()
            _worker.interrupt = None
            with self._lock:
                self._running.discard(interrupt)
        return result, (ended - started) * _MS_PER_S


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
    _workers().close()


__all__ = [
    "SOLVER_SLOW_WORKERS",
    "SOLVER_WORKERS",
    "SmtFormula",
    "SolverExecutor",
    "SolverOutcome",
    "SolverWorkerError",
    "Z3Solver",
    "default_executor",
    "shutdown_default_executor",
]
