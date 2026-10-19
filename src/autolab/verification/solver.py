"""Z3 wrapper for entailment checking.

Each step is verified in two checks. First the premises alone must be
satisfiable; an unsat premise set would entail anything (ex falso), so it is
INVALID with reason `premises_inconsistent`. Then Z3 is asked about
`premises and not claim` — if `unsat`, the claim is entailed (VALID); if
`sat`, there's a model that satisfies the premises but contradicts the claim
(INVALID); if `unknown` (timeout or undecidable), we conservatively return
UNGROUNDED.

Checks run on `SolverExecutor`, a bounded thread pool separate from asyncio's
default executor (AUDIT-2 #1). Cancelling a caller interrupts its Z3 check.

Inputs are SMT-LIB 2 strings, parsed via `z3.parse_smt2_string` so the
boundary between LLM-produced text and Z3's AST is explicit. Every check
runs in a fresh `z3.Context`, so checks are safe to run on worker threads,
and both checks share one `timeout_seconds` deadline.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import weakref
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Final, TypeVar

import z3

from autolab.config import SOLVER_TIMEOUT_S
from autolab.state.models import StepReason, Verdict

_MS_PER_S: Final[int] = 1000
SOLVER_WORKERS: Final[int] = min(32, (os.cpu_count() or 1) + 4)
"""Default size of the Z3 pool (asyncio's default-executor formula)."""

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
        ctx = z3.Context()
        interrupt: _Interrupt | None = getattr(_worker, "interrupt", None)
        if interrupt is not None and not interrupt.attach(ctx):
            return SolverOutcome(verdict="UNGROUNDED", reason="timeout", details="interrupted")
        smt = _build_smt2(premises, claim)
        try:
            assertions = z3.parse_smt2_string(smt, ctx=ctx)
        except z3.Z3Exception as exc:
            return SolverOutcome(
                verdict="UNGROUNDED",
                reason="formalization_failed",
                details=f"SMT parse error: {exc}",
            )

        solver = z3.Solver(ctx=ctx)
        solver.set("timeout", _remaining_ms(deadline))
        # The negated claim is always the last assertion `_build_smt2` emits.
        negated_claim = assertions[len(assertions) - 1]
        for i in range(len(assertions) - 1):
            solver.add(assertions[i])
        consistency = solver.check()
        if consistency == z3.unsat:
            return SolverOutcome(
                verdict="INVALID",
                reason="premises_inconsistent",
                details="premises are mutually unsatisfiable",
            )
        if consistency != z3.sat:
            return SolverOutcome(
                verdict="UNGROUNDED",
                reason="timeout",
                details=f"premise consistency check returned {consistency}",
            )
        solver.add(negated_claim)
        solver.set("timeout", _remaining_ms(deadline))
        result = solver.check()
        if result == z3.unsat:
            return SolverOutcome(
                verdict="VALID", reason="entailed", details="premises entail claim"
            )
        if result == z3.sat:
            model = solver.model()
            return SolverOutcome(
                verdict="INVALID",
                reason="not_entailed",
                details="premises do not entail claim",
                counter_model=str(model),
            )
        return SolverOutcome(
            verdict="UNGROUNDED",
            reason="timeout",
            details=f"solver returned {result}",
        )


def _remaining_ms(deadline: float) -> int:
    """Milliseconds left before `deadline`, at least 1 (Z3 treats 0 as no limit)."""
    return max(1, int((deadline - time.monotonic()) * _MS_PER_S))


def _build_smt2(premises: list[SmtFormula], claim: SmtFormula) -> str:
    """Compose a single SMT-LIB 2 document from premises + negated claim.

    Declarations are deduplicated line-by-line so identical declares across
    premises don't conflict. Order is preserved (first occurrence wins).
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
    parts.append(f"(assert (not {claim.assertion}))")
    return "\n".join(parts)


def _wrap_assert(body: str) -> str:
    body = body.strip()
    if body.startswith("(assert"):
        return body
    return f"(assert {body})"


class _Interrupt:
    """Lets the event loop stop the Z3 check running on one worker thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ctx: z3.Context | None = None
        self._requested = False

    def request(self) -> None:
        """Interrupt the attached context now, or the next one attached."""
        with self._lock:
            self._requested = True
            if self._ctx is not None:
                self._ctx.interrupt()

    def attach(self, ctx: z3.Context) -> bool:
        """Attach `ctx`; False if an interrupt was already requested."""
        with self._lock:
            self._ctx = ctx
            return not self._requested


_worker = threading.local()


class SolverExecutor:
    """A bounded thread pool for Z3 checks, separate from asyncio's default executor.

    At most `workers` checks are submitted at once; other callers wait as
    coroutines on a per-loop semaphore. Cancelling a caller interrupts its check.
    """

    def __init__(self, workers: int = SOLVER_WORKERS) -> None:
        self.workers = workers
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="z3")
        self._lock = threading.Lock()
        self._slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
            weakref.WeakKeyDictionary()
        )
        self._running: set[_Interrupt] = set()

    async def run(self, fn: Callable[[], T]) -> tuple[T, float]:
        """Run `fn` on the pool; return its result and its run time in ms, queueing excluded."""
        async with self._slot():
            interrupt = _Interrupt()
            future = asyncio.wrap_future(self._pool.submit(self._call, fn, interrupt))
            try:
                return await future
            except asyncio.CancelledError:
                interrupt.request()
                raise

    def shutdown(self) -> None:
        """Interrupt running checks, drop queued ones and join the worker threads."""
        with self._lock:
            running = list(self._running)
        for interrupt in running:
            interrupt.request()
        self._pool.shutdown(wait=True, cancel_futures=True)

    def _slot(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        with self._lock:
            slot = self._slots.get(loop)
            if slot is None:
                slot = self._slots[loop] = asyncio.Semaphore(self.workers)
            return slot

    def _call(self, fn: Callable[[], T], interrupt: _Interrupt) -> tuple[T, float]:
        with self._lock:
            self._running.add(interrupt)
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
    "SOLVER_WORKERS",
    "SmtFormula",
    "SolverExecutor",
    "SolverOutcome",
    "Z3Solver",
    "default_executor",
    "shutdown_default_executor",
]
