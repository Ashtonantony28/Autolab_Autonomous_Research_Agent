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
- The main module must be safe to import (`if __name__ == "__main__":`), as for any spawned process. The forkserver imports it once.
- Monkeypatching z3 or `solver` in the caller no longer reaches a check. Tests that spy on Z3 call `Z3Solver._check_here`, which runs `_solve` in-process. `test_solver_workers.py` pins that the worker's target is `_solve`, and that worker and in-process outcomes match on every case and golden trace.
