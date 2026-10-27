# ADR-0009: Solver checks run in worker processes

- **Status:** Accepted (default chosen in SG29; owner may revisit)
- **Date:** 2026-10-20
- **Amends:** ADR-0007 (where a check runs; its rules are unchanged)

## Context

Three rounds of in-process fixes (SG19, SG22, SG27) did not bound Z3:

- A single simplex run on Real bounds ignores both its `timeout` and `Context.interrupt()`. A pool of ≤ 100 long premises overran its timeout 5–20×, and a cancel was ignored for 2–10 s (AUDIT-22 #3).
- Every z3py call goes through ctypes and retakes the GIL. SG27's per-premise loading made one hand-off per premise, so a check beside a Python thread ran 80–116× slower and timed out (AUDIT-22 #2).
- A check on the main thread could not be stopped by pytest-timeout's signal (AUDIT-10 #2).

## Decision

- **Where a check runs.** `Z3Solver.check_entailment` builds the SMT-LIB document in the caller's process. It sends `(document, premise count, claim is "true", timeout)` to a persistent worker process (`verification/solver_workers.py`), and the worker runs `solver._solve`. That is the old `_check` logic, with the same verdicts, reasons and details. The worker replies with the `SolverOutcome`.
- **Bounding.** The caller waits on the pipe. It does not hold the GIL while it waits. Two things kill the worker and start a replacement:
  - The check is still running at `timeout + max(10 ms, 10% of timeout)`. The result is UNGROUNDED/`timeout`, "worker killed past the timeout".
  - The caller is cancelled, or the executor shuts down. `_Interrupt.request` kills the attached worker, and the result is UNGROUNDED/`timeout`, "interrupted".

  A killed check returns no reply, so it can never be VALID. Z3's own limits still apply inside the worker, so the kill is a backstop.
- **Start-up off the hot path.**
  - Workers are forked from a forkserver that preloads `solver` (spawn only where there is no forkserver). They are never forked from this threaded process.
  - `SolverExecutor()` pre-starts workers on a background thread, and workers are reused.
  - A killed worker's replacement starts before the caller's place is given back.
  - A check's timeout starts once its worker is ready.
  - Up to `2 × SOLVER_WORKERS + SOLVER_SLOW_WORKERS` workers stay idle.
- **Cleanup.**
  - Workers are daemonic and get `PR_SET_PDEATHSIG` on Linux.
  - An `atexit` hook stops idle workers, and `shutdown_default_executor()` stops them as well.
  - A worker whose call raised in the caller (for example a per-test timeout) is killed.
- **Scheduling.** SG26's slots, slow lane and lent places are unchanged. Each pool thread waits on one worker. A slow check's place is returned when its job ends, that is, once a killed worker is gone, not when its caller stops waiting (AUDIT-22 #5).
- **Loading.**
  - SG27's one-formula-at-a-time loading is reverted for Boolean and Int pools, which are cheapest in one call (AUDIT-22 #2).
  - It is kept for Real pools of more than `_LOAD_ABOVE` premises. In one call their simplex costs about 9× the Int twin (AUDIT-18 #3), and inside a worker loading costs no GIL hand-offs.
- **Sizing.** The CPU count is the minimum of the affinity and the cgroup CPU quota: v2 `cpu.max`, and v1 `cpu.cfs_quota_us` / `cpu.cfs_period_us`, rounded up (AUDIT-22 #6).

## Consequences

- Timeouts and cancels hold for any formula shape. The parent's event loop and other threads never wait on Z3's GIL hand-offs.
- Each check costs about 0.5 ms of IPC (a trivial check: 5.4 ms in a worker against 4.9 ms in-process, mostly context creation). Bench step p95 is unchanged within noise.
- Each idle worker costs one process. The forkserver shares the preloaded pages.
- ~~The main module must be safe to import (`if __name__ == "__main__":`), as for any spawned process. The forkserver imports it once.~~ Wrong on CPython 3.11: the forkserver never preloads a path-run main, so every worker boot re-ran it (AUDIT-26 #4). Since SG32 workers do not run the main module at all; see the amendment below.
- Monkeypatching z3 or `solver` in the caller no longer reaches a check. Tests that spy on Z3 call `Z3Solver._check_here`, which runs `_solve` in-process. `test_solver_workers.py` pins that the worker's target is `_solve`, and that worker and in-process outcomes match on every case and golden trace.

## Amendment (SG31, 2026-10-21)

- **Matched replies.** Each request carries a sequence number that its reply echoes. A reply with another number, or any other message, is not taken: the worker counts as lost and is discarded, so a reply left in the pipe by a check cut short can never answer a later one (AUDIT-28 #1). The `except BaseException: discard` stays as the first guard, and `worker.ready()` now runs inside it (AUDIT-25 #3).
- **Lost workers.** A worker that dies, fails to start or replies out of turn before its budget runs out gives UNGROUNDED/`worker_died`, with details such as "worker died (signal 9)" or "worker failed to start". Only a worker still running past `timeout + grace` is "worker killed past the timeout" (AUDIT-25 #2, AUDIT-27 #1).
- **No retry on a fresh worker.** A death fails closed, as a timeout does. A crash on a given formula (a Z3 abort, the OOM killer on a large pool) would likely recur and double the step's cost, and a worker that cannot start fails again at once. The verification monitor's retry or escalation already re-runs the step.
- **Timeout cap.** A timeout above `TIMEOUT_MAX_S` (10^6 s) is clamped to it, so the wait on the pipe and Z3's `timeout` stay within 2^31 ms (AUDIT-27 #2).

## Amendment (SG32, 2026-10-21)

- **No main module in workers.** Multiprocessing rebuilds the parent's `__main__` in every child (`init_main_from_path` / `init_main_from_name` in `spawn.get_preparation_data`). On CPython 3.11 the forkserver's `__main__` preload is a no-op (it keeps only `main_path`, which `get_preparation_data` never emits), so each boot re-ran a path-run main: 0.1–0.2 s under `pytest`, about 1 s for a main importing `anthropic`, and an unguarded script ran its body, LLM calls included, once per worker (AUDIT-26 #4, AUDIT-25 #1). A worker's target is `solver._solve`, which needs nothing from the main module, so `solver_workers` wraps `get_preparation_data` to drop those two keys for processes named `z3-worker` only. Other processes are prepared as before. The main module no longer needs a guard for the solver's sake, and the `-m` preload and `_forkserver_init` are gone.
- **Why not detect an unguarded main instead.** With the main never re-run there is nothing left to detect: an unguarded script's check is VALID and its body runs once. A worker that still dies at boot is reported as `worker_died`, "worker failed to start" (SG31).
- **Start-up when a `Verifier` is built.** A `Verifier` without its own executor obtains `default_executor()` in its constructor, so the workers start during the formalization's LLM calls instead of at the first check (AUDIT-26 #3).
- **Newcomers rank as short.** A task with no completed check has not shown it is hard, so it ranks with short callers and a new run's first check never queues behind every hard caller (AUDIT-26 #1). It is lent no place: its check may be hard, and a hard run's first check on a lent place held an easy caller for a whole timeout (the AUDIT-18 #4 load). A task becomes hard after one check of `_HARD_SLICES` slices. SG30's aging still bounds every caller's wait.
- **What this does not bound.** Runs that start together are all newcomers, and a fair queue serves them in arrival order. The AUDIT-26 #1 load (`newcomer_first_check_s`) now sends its newcomer once every hard run has completed a check, which is the case it names. Before, it arrived behind 8 queued first checks of the hard runs themselves.

## Amendment (SG34, 2026-10-21)

- **Newcomers are lent places, latest first.** Supersedes "It is lent no place" above. In LangGraph every node and retry is a fresh task, so runs that start together are the normal case, and a FIFO queue of their first checks held an easy newcomer for several hard checks (AUDIT-30 #1). A stuck slot now lends to short callers first, then to newcomers latest first. A newcomer's check on a lent place that runs `_HARD_SLICES` slices is stopped (its worker killed) and queued again as hard, so a lent place holds a hard check for at most that long; so is one that has run a slice while a short caller waits, queued again for a slot only (not counted as hard), so it cannot take the place back between the short caller's checks (the AUDIT-18 #4 load). A stopped check is re-run, as every check is pure. Slots still go in arrival order to short callers and newcomers alike (AUDIT-32 #4).
- **Aging per kind of place.** Slots age by slot grants (SG30, unchanged). Lent places age by lent grants: a caller later ones were lent `_LENT_AGING` × `workers` places ahead of is lent the next, so no newcomer is overtaken without bound.
- **Cost.** A hard run's first check on a lent place wastes up to `_HARD_SLICES` slices of CPU and one worker restart. Measured (`audit_load_8`, 2 + 2 places, 0.4 s timeout): a newcomer beside 8–16 hard runs that started together waits 0.1–0.6 hard checks (was 1.8–3.9); a 10-step easy verify beside 16 at 5 s takes 0.7 s (was 20.3 s); hard runs' throughput and longest wait under `starve` are no worse than round 8 measured.
- **Fork safety.** A process forked after the pool started does not own its workers or the forkserver. An `os.register_at_fork` hook, and a pid check in `Workers` and `SolverExecutor`, make the child adopt both empty: it closes its copies of the idle workers' pipes, forgets multiprocessing's forkserver, and starts its own, never using or killing the parent's (AUDIT-29 #1, AUDIT-30 #2).

## Amendment (SG35, 2026-10-22)

- **A stopped newcomer keeps its place.** Supersedes "queued again for a slot only (not counted as hard)" above in one respect: a caller keeps the arrival number of its first request when its check is stopped and queued again. Stopped after a slice for a short caller, it still ranks as a newcomer for slots (but is lent no place); stopped at `_HARD_SLICES`, it ranks as hard, ahead of hard runs that arrived after it. Before, both went to the back of the hard queue: a 150 ms check waited 18.7–20.5 s beside 16 hard runs at the default timeout (AUDIT-34 #1).
- **Waiting for a worker is not run time.** A check's run time, for `solver_ms`, hardness and a stop, excludes the time it starts, boots or replaces its worker, while it waits as well as after; and a slot whose check waits for its worker is not stuck, so it lends nothing (AUDIT-34 #1).
- **A stop that comes too late keeps the result.** If a newcomer's check had already left its worker when the stop came, its result is returned, not run again.
- **Fork exit.** A forked child also drops the owner's worker processes from multiprocessing's `process._children`, so its normal exit (`sys.exit`, the end of `__main__`) no longer sends them SIGTERM through multiprocessing's atexit hook (AUDIT-35 #1, AUDIT-36 #1), and its own worker starts no longer poll, and so reap, the owner's workers (AUDIT-33 #3).
- **Not changed: the CPU bound (AUDIT-34 #2 stays open).** Newcomers are still lent places beside every slot and slow place, so up to `2 × workers + slow_workers` Z3 checks run at once. Holding them to `workers + slow_workers` gives a newcomer only a slot, and SG30's aging (no caller overtaken for more than `workers` slots) then serves a burst of newcomers nearly in arrival order: with every check in a fresh task, an easy newcomer beside 12 hard runs waited 3.1 and 5.7 hard checks (AUDIT-30 #1 asks for ≤ 1.5). Keeping both needs an owner's choice between the bound, the AUDIT-30 #1 loads and SG30's aging bound.

## Amendment (SG36, 2026-10-27)

- **The lender is paused.** Supersedes "Not changed: the CPU bound" above (the owner's choice, after AUDIT-38's prototype). While a place is lent, the worker of the stuck slot check that lent it is stopped with SIGSTOP and continued with SIGCONT when the lent check ends. `_Slots` keeps the stuck checks and, after every grant, release, stick and unstick, pauses one per lent place (the longest stuck first) and continues the rest, so at most `workers + slow_workers` Z3 checks run at once. A paused check is not moved to a slow place. Measured (`audit_load_10 bound`, 2 + 2 places): 3.0–3.8 Z3 processes run on average, over 4 in 0–1 % of samples (5.1–5.6 before); an easy newcomer beside 8–16 hard runs that started together waits 0.0–0.5 hard checks, and a 150 ms newcomer beside 16 hard and 4 short runs at 5 s waits 0.17–0.18 s (3.4–8.4 s before).
- **A paused check's budget excludes the pause.** Z3's `timeout` is wall-clock and runs on while the process is stopped (measured: a 1.0 s check paused 0.5 s returns at 1.00 s having used 0.5 CPU s), and a check cut early and run again loses all its progress. So Z3's `timeout` is no longer set: a watchdog thread in the worker calls `ctx.interrupt()` once a deadline passes on a clock that excludes paused time (the parent writes the paused total to shared memory before SIGCONT), and again every `_INTERRUPT_EVERY_S`, since Z3 forgets an interrupt that lands between checks. The parent's waits for a worker's boot and reply, `solver_ms`, hardness and the stop rule exclude paused time too; a check that a frozen call starts is paused from the start. Measured: a step's p50 in `bench_verifier` is 6.10 ms (5.83 ms before), the watchdog thread's cost. `_RESET_SHARE` is gone: the watchdog's deadline spans the whole call, including loading.
- **A newcomer that gives way is paused, not killed.** It releases its lent place, keeps its arrival number (SG35), and continues once it gets a place again; its check runs once, so the up to `_HARD_SLICES` slices of CPU and the worker restart SG34 spent are no longer lost. While it waits it holds a worker process and a pool thread.
- **It stays a newcomer (AUDIT-38 #1).** Until it shows it is hard, a newcomer that gave way is lent places again, after short callers, so it no longer waits for a slot to free. Since giving way now costs nothing, it gives way again as soon as the next short caller queues (not after a slice): a 10-step easy verify beside 3 hard runs (1 slot, 1 slow place) takes 0.17 s, against 0.18 s before SG36, where with "stop it only once it shows it is hard" it took 0.32 s, its checks waiting behind a newcomer that took the place between them (AUDIT-18 #4).
- **Safety.** Signals are POSIX-only (`hasattr(signal, "SIGSTOP")`); elsewhere nothing is paused and the bound is not held. A worker is always continued before it is killed, interrupted, released to the pool or left at exit (multiprocessing's exit hook SIGTERMs and joins, which hangs on a stopped process); a worker that exits while paused is reported dead as before; a forked child never signals the owner's workers.
