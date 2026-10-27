"""Persistent worker processes for Z3 checks (ADR 0009).

Z3 cannot be bounded in-process: a single simplex run ignores its limit and
interrupts, and every ctypes call retakes the parent's GIL (AUDIT-22 #2, #3). A
`Worker` runs one call at a time in its own process; the caller waits on a pipe
without holding the GIL and ends a call past its budget, or on request, by
killing the process. `Workers` keeps started processes for reuse, so start-up
stays off the hot path. Processes come from a forkserver (spawn where there is
none), never from a fork of this threaded process. A worker needs nothing from
the caller's main module, so it never re-runs it: no import cost per boot, and
a main with no `if __name__ == "__main__":` guard runs once (AUDIT-26 #4, AUDIT-25 #1).
A process forked from the owner adopts its `Workers` empty and starts its own forkserver:
it never uses, or kills, the owner's workers (AUDIT-29 #1, AUDIT-30 #2).
"""

from __future__ import annotations

import atexit
import contextlib
import ctypes
import multiprocessing
import os
import signal
import sys
import threading
import weakref
from collections.abc import Callable, Sequence
from multiprocessing import forkserver, spawn
from multiprocessing.connection import Connection
from multiprocessing.context import BaseContext
from typing import Any, Final

_BOOT_S: Final[float] = 60.0
"""How long a new worker may take to start before it counts as lost."""
_PR_SET_PDEATHSIG: Final[int] = 1

_NAME: Final[str] = "z3-worker"
_MAIN_KEYS: Final[tuple[str, ...]] = ("init_main_from_path", "init_main_from_name")
Reply = tuple[bool, Any]
"""(True, result) or (False, the error's text)."""
DIED: Final[str] = "died"
FAILED_TO_START: Final[str] = "failed to start"
OUT_OF_TURN: Final[str] = "replied out of turn"


def _die_with_parent() -> None:
    """On Linux, have the kernel kill this worker when its parent exits."""
    if not sys.platform.startswith("linux"):
        return
    # Without libc's prctl the pipe's EOF still ends the worker.
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)


def _serve(conn: Connection, target: Callable[..., Any]) -> None:
    """Worker main loop: answer each `(seq, args)` with `(seq, reply)` until the pipe closes."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the parent handles Ctrl-C
    _die_with_parent()
    try:
        conn.send(None)  # ready
        while True:
            seq, args = conn.recv()
            try:
                reply: Reply = (True, target(*args))
            except Exception as exc:  # handed to the caller, as a thread pool does
                reply = (False, f"{type(exc).__name__}: {exc}")
            conn.send((seq, reply))
    except (EOFError, OSError):  # the parent closed the pipe or exited
        return


_preparation_data = spawn.get_preparation_data


def _without_main(name: str) -> dict[str, Any]:
    """What a new process prepares from; a worker's leaves out the main module.

    Multiprocessing re-runs the parent's main in every child as `__mp_main__`; the
    forkserver never preloads a path-run main on CPython 3.11, so each boot paid its
    imports again, and an unguarded main ran its body once per worker.
    """
    data: dict[str, Any] = _preparation_data(name)
    if name == _NAME:
        for key in _MAIN_KEYS:
            data.pop(key, None)
    return data


spawn.get_preparation_data = _without_main


class Worker:
    """One worker process; `call` runs `target(*args)` in it."""

    def __init__(self, ctx: BaseContext, target: Callable[..., Any]) -> None:
        self.conn, child = ctx.Pipe()
        self.process = ctx.Process(  # type: ignore[attr-defined]
            target=_serve, args=(child, target), name=_NAME, daemon=True
        )
        self.process.start()
        child.close()
        self._ready = False
        self._seq = 0
        self.fault = ""
        """Why the last call returned None (`DIED`, `FAILED_TO_START`, `OUT_OF_TURN`);
        empty when it ran past its budget."""

    def ready(self) -> bool:
        """Wait until the worker has started; False if it died or took over `_BOOT_S`."""
        try:
            if not self._ready:
                if not self.conn.poll(_BOOT_S):
                    self.fault = FAILED_TO_START
                    return False
                self.conn.recv()
                self._ready = True
            return True
        except (EOFError, OSError):  # killed, or died
            self.fault = FAILED_TO_START
            return False

    def call(self, args: tuple[Any, ...], budget_s: float) -> Reply | None:
        """`target(*args)`'s reply, or None (see `fault`) if the worker failed to start,
        died, answered another request, or ran past `budget_s`.

        Each request carries a sequence number its reply must echo, so a reply left in
        the pipe by an earlier call is never taken for this one's (AUDIT-28 #1). The
        budget starts once the worker is ready, so start-up never counts against it.
        """
        try:
            if not self.ready():
                return None
            self._send(args)
            if not self.conn.poll(budget_s):
                return None
            match self.conn.recv():
                case (self._seq, (bool(), _) as reply):
                    matched: Reply = reply
                    return matched
            self.fault = OUT_OF_TURN
            return None
        except (EOFError, OSError):  # killed, or died
            self.fault = DIED
            return None

    def _send(self, args: tuple[Any, ...]) -> None:
        self._seq += 1
        self.conn.send((self._seq, args))

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
        self._pid = os.getpid()
        _made.add(self)

    def _own(self) -> None:
        """In a process other than the one that built this pool, `_adopt` it first."""
        if self._pid != os.getpid():
            self._adopt()

    def _adopt(self) -> None:
        """Forget the owner's workers without touching their processes; start afresh."""
        for worker in self._idle:
            worker.conn.close()  # this process's copy of the pipe only
        for worker in self._live:
            # Or this process's exit would SIGTERM them (AUDIT-35 #1, AUDIT-36 #1).
            _children().discard(worker.process)
        self._lock = threading.Lock()
        self._idle, self._live, self._ctx = [], set(), None
        self._pid = os.getpid()

    def _context(self) -> BaseContext:
        with self._lock:
            if self._ctx is None:
                ctx: BaseContext
                if "forkserver" in multiprocessing.get_all_start_methods():
                    ctx = multiprocessing.get_context("forkserver")
                    ctx.set_forkserver_preload(self._preload)
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
        self._own()
        while True:
            with self._lock:
                if len(self._idle) >= min(n, self.idle_max):
                    return
            worker = self._start()
            with self._lock:
                self._idle.append(worker)

    def acquire(self) -> Worker:
        """An idle worker, or a new one if none is idle."""
        self._own()
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
        self._own()
        with self._lock:
            idle, self._idle = self._idle, []
            for worker in idle:
                self._live.discard(worker)
        for worker in idle:
            worker.stop()


_all: list[Workers] = []
_made: weakref.WeakSet[Workers] = weakref.WeakSet()


def _children() -> set[Any]:
    """Multiprocessing's record of this process's children, whose daemons it terminates at
    exit; a raw `os.fork` child inherits its parent's."""
    children: set[Any] = vars(multiprocessing.process)["_children"]
    return children


def _forget_forkserver() -> None:
    """Drop the owner's forkserver in a forked child; the next worker starts this one's own.

    The child cannot `waitpid` a forkserver it did not start, so multiprocessing's
    `ensure_running` would raise ChildProcessError.
    """
    server: Any = forkserver._forkserver
    if server._forkserver_alive_fd is not None:
        with contextlib.suppress(OSError):
            os.close(server._forkserver_alive_fd)
    server._forkserver_address = server._forkserver_alive_fd = server._forkserver_pid = None
    server._lock = threading.Lock()


def _after_fork_in_child() -> None:
    _forget_forkserver()
    for workers in list(_made):
        workers._adopt()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def register(workers: Workers) -> Workers:
    """Stop `workers`' idle processes at interpreter exit."""
    _all.append(workers)
    return workers


@atexit.register
def _close_all() -> None:
    for workers in _all:
        workers.close()
