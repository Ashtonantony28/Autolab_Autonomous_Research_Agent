# AUDIT-41: Encoding soundness, round 11 (false negatives)

Auditor: an independent agent. It wrote none of the code and none of the earlier audits.
Base: `origin/claude/sg36-pause-lender` at `3d2d7b4` (SG36).
Scope: what changed in SG36, all in `verification/solver.py` and `verification/solver_workers.py`:
- A lending slot's worker is paused with SIGSTOP and continued with SIGCONT while a newcomer uses the lent place (`_Slots._balance`, `_Interrupt.freeze`/`thaw`).
- A newcomer that gives way is paused, not killed, and queued again (`SolverExecutor.run`, `_again`).
- Z3's own `timeout` is gone. A `_Watchdog` thread in the worker interrupts the context once a deadline passes on `_now()`, a clock that excludes paused time. The parent writes the paused total to shared memory before SIGCONT.
- `Worker._signal` tolerates a worker that was reaped after `exitcode` was read.

`agents/*`, `orchestrator/*` and `tools/*` are unchanged since round 6 (`git diff 9792264 --stat` touches none of them). The AUDIT-21/25 probes of the Design cost/duration path still apply, and AUDIT-21 #1 (medium) is still open, unchanged.

"Admitted" means the same as in AUDIT-21: `VerificationMonitor().decide(verdict)` returns PASS, or a Design is merged without the cost/duration pause that principle 7b requires.

Summary: **0 critical, 0 high, 1 medium, 0 low.**

**Soundness held.** I found no way to get a wrong trace admitted, and no way to turn a timeout or UNKNOWN into VALID:
- A paused, resumed, cancelled or re-granted check returns its own reply or UNGROUNDED. It never returns a stale reply or another caller's verdict.
- The watchdog can only end a check early as `unknown`, which is UNGROUNDED. It cannot produce `sat` or `unsat`.
- Two stress runs made 349 pauses (one with forced signal and detach jitter) and returned 0 wrong verdicts (below).

The one finding is a liveness regression. It fails closed, late: a frozen lender is never thawed while a stream of short callers keeps its lent place busy, so `timeout_seconds` no longer bounds a check's wall time.

The testable finding has a repro in `tests/audit/test_audit_41.py`, marked `xfail(strict=True, reason="AUDIT-41 #1")`, with one plain control. Nothing calls a real LLM.

---

## Medium

### #1 A frozen lender stays frozen as long as short callers keep its lent place busy

**Impact.**
- `_Slots._balance` freezes `_lenders[:lent]`: one stuck slot check per lent place, longest stuck first. It thaws a lender only when `lent` drops below that lender's index.
- Nothing ages a frozen lender. `_LENT_AGING` and `passed` age waiters, and a lender is not a waiter.
- Short callers keep coming, for example steps of other concurrent `verify()` calls, each its own `run`. Each one releases the lent place and `_wake` lends it straight to the next waiter, so `lent` never drops. The lender stays SIGSTOPped the whole time.
- Its paused time is excluded from its budget on both sides of the pipe (`Worker._poll`, `_now()`), so nothing ends it either.

So the step's wall time is the length of the short stream plus its timeout, with no bound. Before SG36 the lender ran on beside the lent check (that was AUDIT-34 #2's CPU overrun) and ended at its timeout.

The frozen lender also keeps holding its slot (`held`). A hard caller, which is not lendable, waits for that slot, so it waits as long as the stream lasts too. This breaks the bounded wait that AUDIT-22 #4 and the `_Slots` docstring promise ("every caller waits a bounded number of grants"): lent grants do not count toward a hard waiter's `passed`.

The verdict fails closed: the check still ends UNGROUNDED/timeout, just late. But a verify that includes one hard step can be held up without limit by concurrent easy verifies. The Phase 2 bound "a 10-step trace verifies in < 30 s" then depends on what else is running.

**Repro (committed).** `_STARVE_SCRIPT` in `tests/audit/test_audit_41.py`:
1. It builds a `SolverExecutor(workers=1, slow_workers=0)` and runs an easy check 5 times to measure `easy_s`.
2. It sets the timeout to `max(40 * easy_s, 20 * _SLICE_S)`.
3. It starts a hard Fermat n=3 check and waits until `_Slots.stuck == 1`.
4. **Control.** Nothing else runs.
5. **Stream.** 4 tasks run easy checks back to back for 6 timeouts.

| Run (this machine) | timeout | hard check's wall time | easy checks run |
|---|---|---|---|
| SG36, control (no stream) | 1.0 s | < 3 timeouts (test passes) | 0 |
| SG36, stream of 6 timeouts | 1.0 s | 6.82–7.03 s | 627–707 |
| SG36, stream of 3 timeouts (scratch) | 1.0 s | 3.82 s | 345 |
| SG36, stream of 12 timeouts (scratch) | 2.73 s | 35.37 s | 3269 |
| Base `9792264` (pre-SG36), stream of 6 timeouts (scratch) | 1.0 s | **1.00 s** | 791 |

The hard check's wall time grows linearly with the stream: about stream + 1 timeout.

Tests:
- Control: `test_a_stuck_check_alone_ends_near_its_timeout`.
- xfail: `test_a_frozen_lender_ends_near_its_timeout_beside_a_short_stream`. Under `--runxfail` it fails on `assert got["hard_s"] < 3 * got["timeout_s"]` (7.03 < 3.0).

**Root cause.**
- `solver.py` `_Slots._balance`: who stays frozen depends only on how long a check has been stuck. It does not depend on how long the check has been frozen, or on how many places were lent on its behalf.
- `_Slots.stick`/`unstick`: a frozen lender keeps `stuck` (so it can keep lending) and keeps `held`.

**Suggested fix (any one).**
- Age lenders. After a lender has lent `_LENT_AGING * workers` places, or has been frozen for one slice per place lent, stop lending on its behalf. Either let `lent` drain without re-lending (don't count it in `stuck` while it is due), or thaw it and run one lent check unpaused. Its timeout then bounds it again, plus a bounded number of lent checks.
- Or cap a lender's total frozen time at a multiple of its own timeout. After that it runs on as before SG36: an overrun bounded by one check, the AUDIT-34 #2 trade-off for one place.

---

## Probed and held up

**Watchdog on a pause-aware clock (`_solve`, `_Watchdog`, `_now`).**
- `_now()` is `monotonic − paused_s()`, and the parent only ever raises `paused_s`, so the watchdog never fires before the real, pause-excluded deadline.
- A fire can only make Z3 return `unknown`. Scratch `z3int.py`, Z3 4.16.0:
  - An interrupt between checks is forgotten: the next `check()` returns `sat` on a satisfiable pool, not a wrong `unsat`.
  - `model().sexpr()` and `Solver.add` after an interrupt do not raise, so a late fire during counter-model extraction does not become a `SolverWorkerError`.
  - `from_string` ignores interrupts: a 200 000-assertion parse ran 6.3 s under an interrupt every ms. A slow parse is still ended by the parent's kill past the grace (`_KILLED`, UNGROUNDED), as before SG36, when Z3's `timeout` did not cover parsing either.
- In `_decide`, every path that is not `unsat`/`sat` ends UNGROUNDED. `_load` returns `unknown` once `_now() >= deadline`, and the watchdog fires only after that point. An interrupt never lands in another call's context: `__exit__` joins the thread before the context is dropped, and each call has a fresh `z3.Context`.
- The shared paused total is written before SIGCONT and read only by the continued worker. An aligned 8-byte double is not torn on x86-64.
- The parent's `_poll` and the worker's deadline exclude the same intervals, so the parent kills only past timeout + grace of unpaused time.

**Paused and resumed checks: no stale reply or other caller's verdict.**
- The invariant "a target is SIGSTOPped iff `_frozen`" holds under `_Interrupt._lock`:
  - `attach` pauses if frozen.
  - `detach` continues before the target is cleared.
  - `freeze`/`thaw` act only on the attached target.
- So a worker reaches `Workers.release` continued. A frozen `_Interrupt` cannot pause a worker that another caller has since acquired, because its target is None after `detach`.
- Lock order: `_Interrupt._lock` → `Worker._lock`. Nothing takes the reverse.
- Sequence numbers still pair each reply with its request (AUDIT-28 #1). A reply sent in the moment between SIGCONT and SIGKILL in `Worker.interrupt` is this call's own. It is kept only on AUDIT-37's "stop that keeps its result" path, which is sound.

**Cancel during a pause.**
- A cancel during `_again` gives back any place granted meanwhile. `request()` then continues and kills the worker (`Worker.interrupt` resumes first).
- On the pool thread, the call gets EOF, so the result is `None` → `_INTERRUPTED`, which is UNGROUNDED.
- `finally` releases nothing more, because `held` was already False.

**A newcomer paused while its check ends** (`_again` returns None). It holds no place, it is thawed, and `return await future` hands back its own result. `fn` runs once.

**SIGCONT/SIGSTOP to a reaped or reused pid.**
- Workers are children of the forkserver. It reaps a worker and then writes the status to the owner's sentinel, so for an instant `exitcode is None` while the pid is free.
- In that instant `os.kill` raises `ProcessLookupError`, which `_signal` now catches. A wrong-process signal would need the pid space to wrap within those microseconds: not reachable in practice (pid_max is 32 768 here, so about 32 000 process starts in that instant). `Process.kill()` before SG36 had the same window.
- A signal to some other worker of the same pool would fail closed anyway. Its parent does not count a foreign stop as paused, so the check runs out its budget, `_KILLED`, UNGROUNDED.

**Concurrent `exitcode` reads.** Two threads polling one forkserver sentinel at once can leave `returncode = 255` ("exit code 255" in `_lost`'s details). Every `_signal` from the event loop runs under `_Interrupt._lock`, and the pool thread reads `exitcode` only after `detach`. So the two never overlap except in the atexit hook `_close_all`. That would mislabel only the details.

**Fork adoption while paused.**
- A raw-fork child's `_adopt` forgets the owner's `_live` workers, paused or not, and the child's executor `_adopt` forgets its `_Slots`/`_lenders`. So the child never signals the owner's workers.
- The child's atexit `_close_all` sees an empty `_live`.
- AUDIT-37 #1 (a worker mid-start) is still open and is not pause-specific.

**Run-time accounting.** `ran_s()` can subtract the same time twice when a `_paused` (worker start/replace) window overlaps a `_frozen` one. That window is reachable only around a lender's or newcomer's detach/discard, and only lowers `ran_s`, which drives scheduling (hardness, `_yields`, `stuck`). It cannot go negative there: a discard follows only a kill past the timeout or a cancel. `solver_ms < 0` was never seen (stress counted 0).

**Stress (scratch, not committed).** `stress.py`: 15 rounds per seed of 6–20 concurrent callers, 1–4 checks each, on `SolverExecutor(workers ∈ {1, 2}, slow_workers ∈ {0, 1})`.
- Checks: valid; not entailed; pigeonhole 7 and 8 (inconsistent premises); Fermat n=3 (undecidable here); a false Fermat equality.
- Z3 timeouts were 0.1–0.6 s, and 15 % of checks were cancelled within 0–0.3 s.
- `Worker.pause` was wrapped to count pauses.

| Run | Pauses | Cancels | VALID on a non-entailed check | VALID on an inconsistent pool | Negative `solver_ms` |
|---|---|---|---|---|---|
| Seed 1, unmodified | 173 | 53 | 0 | 0 | 0 |
| Seed 2, `_signal` ±0–20 ms and `detach` +0–10 ms jitter | 176 | 52 | 0 | 0 | 0 |

Every valid check came back VALID, and every Fermat check UNGROUNDED. Every other check came back INVALID or UNGROUNDED.

## Re-check of earlier soundness fixes

At `3d2d7b4`, `uv run pytest tests/audit/test_audit_{1,5,9,12,13,17,21,25,28,29,30,33,37}.py -q -p no:randomly` gave 136 passed and 31 xfailed (AUDIT-37's set plus its own control and xfail). No xfail is a regressed soundness fix. Still open:
- AUDIT-21 #1 (medium) and #3 (low).
- AUDIT-33 #1 and #2, and AUDIT-37 #1 (fork during a worker start).

## How this was measured

- **Machine.** A 4-CPU container shared with other agents, CPython 3.11.17, Z3 4.16.0.
- **Repeat runs.**
  - `uv run pytest tests/audit/test_audit_41.py -q -p no:randomly` gave 1 passed / 1 xfailed in 3 of 3 runs, 11–12 s each.
  - With `--runxfail` it gave 1 passed / 1 failed, on `hard_s < 3 * timeout_s` (7.03 s against 3.0 s).
- **Timing.** No absolute times. The hard check's timeout is 40 × an easy check measured through the same executor in the same script, and never less than 20 of the solver's slices (`_SLICE_S`). The stream lasts 6 timeouts, and the bound is 3 timeouts.
- **Baseline.** Pre-SG36 numbers come from the same scratch script run against `git archive 9792264` in this agent's scratch folder.
- **Simulation.** None for #1; it is reached through the public `SolverExecutor.run`. Stress seed 2 adds sleeps around `Worker._signal` and `_Interrupt.detach`.
- **Scratch.** `z3int.py`, `stopterm.py`, `starve.py`, `stress.py` and the base archive lived in this agent's private scratch folder and were deleted.
- **Gates.** `make lint typecheck` passes.

## Bottom line

SG36 did not open a false-negative path. Pausing, resuming, cancelling during a pause, and adopting after a fork never return a stale reply or another caller's verdict. The watchdog can only make a check UNGROUNDED, never VALID: 349 pauses under stress, 0 wrong.

The cost is liveness (#1, medium, fails closed). A frozen lender is never thawed while short callers keep its lent place busy, so its wall time, and the wait of any hard caller queued for its slot, grows with the stream. Measured: 1.0 timeout before SG36, 6.8 timeouts beside a 6-timeout stream, 13 timeouts beside a 12-timeout stream.

The open soundness items are still AUDIT-21 #1 (medium) and #3 (low).
