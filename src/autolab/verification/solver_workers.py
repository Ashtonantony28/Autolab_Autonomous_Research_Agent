"""Persistent worker processes for Z3 checks (ADR 0009).

Z3 cannot be bounded in-process: a single simplex run ignores its limit and
interrupts, and every ctypes call retakes the parent's GIL (AUDIT-22 #2, #3). A
`Worker` runs one call at a time in its own process; the caller waits on a pipe
without holding the GIL and ends a call past its budget, or on request, by
killing the process. `Workers` keeps started processes for reuse, so start-up
stays off the hot path. Processes come from a forkserver (spawn where there is
none), never from a fork of this threaded process. As with any spawned process,
the main module must be safe to import (`if __name__ == "__main__":`); the
forkserver preloads it once.
"""

from __future__ import annotations

import atexit
import contextlib
import ctypes
import multiprocessing
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from multiprocessing.connection import Connection
from multiprocessing.context import BaseContext
from typing import Any, Final

_BOOT_S: Final[float] = 60.0
"""How long a new worker may take to start before it counts as lost."""
_PR_SET_PDEATHSIG: Final[int] = 1

_FORKSERVER_INIT: Final[str] = "autolab.verification._forkserver_init"
Reply = tuple[bool, Any]
"""(True, result) or (False, the error's text)."""


def _die_with_parent() -> None:
    """On Linux, have the kernel kill this worker when its parent exits."""
    if not sys.platform.startswith("linux"):
        return
    # Without libc's prctl the pipe's EOF still ends the worker.
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)


def _serve(conn: Connection, target: Callable[..., Any]) -> None:
    """Worker main loop: reply to each call with `target(*args)` until the pipe closes."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the parent handles Ctrl-C
    _die_with_parent()
    try:
        conn.send(None)  # ready
        while True:
            args = conn.recv()
            try:
                reply: Reply = (True, target(*args))
            except Exception as exc:  # handed to the caller, as a thread pool does
                reply = (False, f"{type(exc).__name__}: {exc}")
            conn.send(reply)
    except (EOFError, OSError):  # the parent closed the pipe or exited
        return


def _main_imports() -> list[str]:
    """The main module's name when it was run with `-m`: each worker re-runs it, and
    with its imports preloaded in the forkserver that costs no more than its body."""
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    name = getattr(spec, "name", None)
    return [name] if isinstance(name, str) and not name.endswith("__main__") else []


class Worker:
    """One worker process; `call` runs `target(*args)` in it."""

    def __init__(self, ctx: BaseContext, target: Callable[..., Any]) -> None:
        self.conn, child = ctx.Pipe()
        self.process = ctx.Process(  # type: ignore[attr-defined]
            target=_serve, args=(child, target), name="z3-worker", daemon=True
        )
        self.process.start()
        child.close()
        self._ready = False

    def ready(self) -> bool:
        """Wait until the worker has started; False if it died or took over `_BOOT_S`."""
        try:
            if not self._ready:
                if not self.conn.poll(_BOOT_S):
                    return False
                self.conn.recv()
                self._ready = True
            return True
        except (EOFError, OSError):  # killed, or died
            return False

    def call(self, args: tuple[Any, ...], budget_s: float) -> Reply | None:
        """`target(*args)`'s reply, or None if the worker died or ran past `budget_s`.

        The budget starts once the worker is ready, so start-up never counts against it.
        """
        try:
            if not self.ready():
                return None
            self.conn.send(args)
            if not self.conn.poll(budget_s):
                return None
            reply: Reply = self.conn.recv()
            return reply
        except (EOFError, OSError):  # killed, or died
            return None

    def interrupt(self) -> None:
        """Kill the process; a call in progress returns None."""
        self.process.kill()

    def stop(self) -> None:
        """Kill the process, wait until it is gone, and free the pipe."""
        self.process.kill()
        self.process.join()
        self.conn.close()

    def alive(self) -> bool:
        """Whether the process is still running."""
        return bool(self.process.is_alive())


class Workers:
    """Started workers for reuse, at most `idle_max` kept idle; safe from any thread."""

    def __init__(
        self, target: Callable[..., Any], idle_max: int, preload: Sequence[str] = ()
    ) -> None:
        self.target = target
        self.idle_max = idle_max
        self._preload = list(preload)
        self._lock = threading.Lock()
        self._idle: list[Worker] = []
        self._ctx: BaseContext | None = None
        self._live: set[Worker] = set()

    def _context(self) -> BaseContext:
        with self._lock:
            if self._ctx is None:
                ctx: BaseContext
                if "forkserver" in multiprocessing.get_all_start_methods():
                    ctx = multiprocessing.get_context("forkserver")
                    ctx.set_forkserver_preload([_FORKSERVER_INIT, *self._preload, *_main_imports()])
                else:  # pragma: no cover - platforms without a forkserver
                    ctx = multiprocessing.get_context("spawn")
                self._ctx = ctx
            return self._ctx

    def _start(self) -> Worker:
        worker = Worker(self._context(), self.target)
        with self._lock:
            self._live.add(worker)
        return worker

    def prestart(self, n: int) -> None:
        """Start workers until `n` (at most `idle_max`) are idle."""
        while True:
            with self._lock:
                if len(self._idle) >= min(n, self.idle_max):
                    return
            worker = self._start()
            with self._lock:
                self._idle.append(worker)

    def acquire(self) -> Worker:
        """An idle worker, or a new one if none is idle."""
        with self._lock:
            while self._idle:
                worker = self._idle.pop()
                if worker.alive():
                    return worker
                self._live.discard(worker)
        return self._start()

    def release(self, worker: Worker) -> None:
        """Give back a worker whose call returned; beyond `idle_max` it is stopped."""
        with self._lock:
            if len(self._idle) < self.idle_max:
                self._idle.append(worker)
                return
            self._live.discard(worker)
        worker.stop()

    def discard(self, worker: Worker) -> None:
        """Stop a worker whose call was cut short, then start its replacement.

        Returns once the process is gone, so a caller's place frees only then (AUDIT-22 #5).
        """
        worker.stop()
        with self._lock:
            self._live.discard(worker)
            replace = len(self._idle) < self.idle_max
        if replace:
            fresh = self._start()
            with self._lock:
                self._idle.append(fresh)

    def live(self) -> int:
        """Worker processes started and not yet stopped."""
        with self._lock:
            return len(self._live)

    def close(self) -> None:
        """Stop every idle worker; a worker in use comes back (or stops) when its call ends."""
        with self._lock:
            idle, self._idle = self._idle, []
            for worker in idle:
                self._live.discard(worker)
        for worker in idle:
            worker.stop()


_all: list[Workers] = []


def register(workers: Workers) -> Workers:
    """Stop `workers`' idle processes at interpreter exit."""
    _all.append(workers)
    return workers


@atexit.register
def _close_all() -> None:
    for workers in _all:
        workers.close()
