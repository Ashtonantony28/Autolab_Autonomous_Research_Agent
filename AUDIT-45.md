# AUDIT-45: Encoding soundness, round 12 (false negatives)

Auditor: an independent agent. It wrote none of the code and none of the earlier audits.
Base: `origin/claude/sg37-bound-pauses` at `2a6ee8f` (SG37).
Scope: what SG37 changed, all in `verification/solver.py`:
- A lender frozen for `_FROZEN_SHARE` (1.5) times its check's timeout runs on, and the earliest lent check is frozen in its place (`_Slots._due`, `_balance`, `lending`, `wake`).
- At most `workers` paused newcomers; past that a newcomer is stopped (`interrupt.request()`), re-queued for a slot only, and `fn` runs again (`SolverExecutor._pause`, the `while True` loop in `run`).
- A lender that ends while its place is lent leaves its slot to the earliest lent check, which is thawed (`_Slots.release`, `_holders`, `_handed`).
- One `z3-watchdog` thread per worker process (`_Watchdog._serve`, `_adopt` after fork).
- Run time counts overlapping pauses and freezes once (`_Interrupt._off`, `_count`).

`agents/*`, `orchestrator/*` and `tools/*` are unchanged since round 6, so the AUDIT-21/25 probes of the Design cost/duration path still apply and AUDIT-21 #1 (medium) is still open, unchanged.

"Admitted" means the same as in AUDIT-21: `VerificationMonitor().decide(verdict)` returns PASS, or a Design is merged without the cost/duration pause that principle 7b requires.

Summary: **0 critical, 0 high, 0 medium, 1 low.**

**Soundness held.** I found no way to get a wrong trace admitted, and no way to turn a timeout or UNKNOWN into VALID. Every SG37 path returns the call's own reply or UNGROUNDED; 5 stress runs with 86 stop-and-rerun newcomers and 133 cancels returned 0 wrong verdicts in 2048 checks (below). AUDIT-41 #1 now passes.

The one finding is a bookkeeping error that fails loud: SG37's "a negative `solver_ms` under load is fixed" does not hold for a lent check frozen before its pool thread starts.

The testable finding has a repro in `tests/audit/test_audit_45.py`, marked `xfail(strict=True, reason="AUDIT-45 #1")`, with one plain control. Nothing calls a real LLM.

---

## Low

### #1 A lent check frozen before its pool thread starts reports a negative `solver_ms`

**Impact.**
- `SolverExecutor.run` calls `slots.lending(interrupt)` before `self._pool.submit(self._call, fn, interrupt)`. `lending` runs `_balance`, which freezes the earliest lent check as soon as a lender is due.
- `_Interrupt.freeze` sets `_frozen` and `_off` at once, even though `_call` has not yet set `started`.
- `ran_s()` is `now - started - _off_s - off`. The frozen interval counted in `_off_s` starts before `started`, so the time between `lending` and the pool thread entering `_call` is subtracted though it was never run time.
- When the pool thread starts the call later than the check runs after its thaw, `ran_s() < 0`. `_call` returns `ran_s() * 1000` as `solver_ms`.
- On the agent's path, `Verifier._check` builds `StepVerdict(solver_ms=...)`, and `StepVerdict.solver_ms` is `Field(ge=0.0)`. So `verify()` raises `pydantic.ValidationError` for that step. The verdict itself is the right one (VALID for a valid step); it is lost, not flipped.
- Before SG37 only lenders (already started) and newcomers that gave way (already started) were frozen, so this window is new.

It fails loud, never admits. It needs a pool-thread start latency larger than the lent check's own run after its thaw, at the moment its lender comes due: about 2 easy checks (≈ 20 ms here) was enough. I did not see it without the injected delay (0 of 855 lent checks, and 0 in stress).

**Repro (committed).** `_NEG_SCRIPT` in `tests/audit/test_audit_45.py`:
1. `SolverExecutor(workers=1, slow_workers=0)`; an easy check run 5 times measures `easy_s`; the hard check's timeout is `max(40 * easy_s, 20 * _SLICE_S)`.
2. A hard Fermat n=3 check starts and becomes a lender (`stuck == 1`).
3. 4 tasks run easy checks back to back on the lent place for 4 timeouts, past the lender's 1.5-timeout share.
4. With argument `DELAY`, `SolverExecutor._call` sleeps `DELAY * easy_s` before the original runs (a late pool thread).

| Run (this machine) | pool-thread delay | lent checks | negative `solver_ms` | min `solver_ms` | wrong verdicts |
|---|---|---|---|---|---|
| unmodified (control) | 0 | 855 | 0 | 2.75 ms | 0 |
| injected | 2 easy checks (≈ 19 ms) | 121 | 1 | −13.8 ms | 0 |
| injected | 5 easy checks (≈ 54 ms) | 53 | 1 | −5.5 ms | 0 |
| injected, 4 runs | 20 easy checks (≈ 170–190 ms) | 18–21 | 1 each | −29 to −106 ms | 0 |

Each run has exactly one: the lent check that was between `lending` and `_call` when the lender came due. The hard check ends UNGROUNDED/timeout in every run.

```
>>> StepVerdict(step_index=0, verdict='VALID', reason='entailed', details='', solver_ms=-35.4)
ValidationError: 1 validation error for StepVerdict
solver_ms
  Input should be greater than or equal to 0 [type=greater_than_equal, input_value=-35.4, ...]
```

Tests:
- Control: `test_lent_checks_beside_a_due_lender_keep_a_non_negative_run_time`.
- xfail: `test_a_lent_check_frozen_before_it_starts_keeps_a_non_negative_run_time`. Under `--runxfail` it fails on `assert got["negatives"] == 0` (1 == 0).

**Root cause.** `solver.py`: `_Interrupt._count`/`freeze` start `_off` whenever they are called, and `ran_s` subtracts all of it from `now - started`. `run` makes a check freezable (`lending`) before its call has started.

**Suggested fix (any one).** In `ran_s`, clip the off intervals to `[started, now]` (count `max(_off, started)` when `started` is set, and drop off time that ended before `started`); or have `_call` set `started` and fold any earlier `_off` into it (`started = max(started, _off)` at start); or call `lending` after `submit` returns and the call has started. Clamping `solver_ms` at 0 alone would hide the error.

---

## Probed and held up

**A due lender runs on, the earliest lent check is frozen (`_due`, `_balance`).**
- `_balance` freezes `order[:lent]` before it thaws the rest, so no more Z3 checks run than slots and places at any instant. Who is frozen changes only how long a check waits; freezing can only delay a reply, not change it.
- A frozen lent check's worker is SIGSTOPped; its parent excludes the stop from its budget (`Worker` accounting, unchanged in SG37) and the worker excludes it from the watchdog deadline (`_now`). So it cannot be killed early into a wrong outcome. A check killed late is `_KILLED`, UNGROUNDED.
- A lent check that has ended but whose coroutine has not yet run `finally` stays in `_lent_checks` and can be frozen after `ended`. Its target is already detached, so nothing is signalled; `ran_s` clamps `off` at 0 past `ended`, and `solver_ms` was computed on the pool thread before. `release` drops it frozen ("left frozen if it is"); the `_Interrupt` is never reused.
- `budget_s` is set from `args[-1]`, the clamped timeout, at the start of every check; until then it is `inf`, so a lender that has not started a check is never due.

**Stop and re-run past `workers` paused newcomers.**
- `stopped = interrupt.request()` is True only when a worker was attached and is killed. The stopped call's result (`_INTERRUPTED`, or its own reply if it arrived first) is discarded and `fn` runs again with a fresh `_Interrupt`, so no state is carried over. On the agent's path `fn` is `partial(Z3Solver.check_entailment, bare, claim)`: pure, the same SMT text each time.
- If the pool thread detaches between `_yields` and `request()`, `request()` returns False and the call's own result is returned: its real reply, or `_INTERRUPTED` (UNGROUNDED) for a check that had not attached. Never another caller's.
- After `shutdown`, `if not stopped or self._closed` returns the interrupted result, UNGROUNDED.
- Sequence numbers still pair each reply with its request (AUDIT-28 #1); a killed worker is discarded, so no stale reply can reach the re-run.
- A cancel while re-queued raises from `acquire` after the first iteration's `finally` already released its place.

**A lender that ends while lent (`release`, `_holders`, `_handed`).** Accounting of slots only. The holder is thawed and leaves `_lent_checks`, so it is not frozen again; it keeps its own `_Interrupt`, future and worker. No check receives another's result.

**One watchdog thread (`_Watchdog`).**
- It interrupts only when `_now() >= deadline`, and `_now` only moves back as the parent adds paused time, so it never fires early. A late fire is ended by the parent's kill (UNGROUNDED).
- `_ctx.interrupt()` runs under `_cond`, and `__exit__` discards the watchdog under `_cond`, so no interrupt reaches a context after `_solve` returns; each `_solve` has a fresh `z3.Context`. A worker runs one check at a time, so at most one is armed.
- If `_serve` died, the next `__enter__` starts a new thread (`is_alive()`); a check left unwatched runs to the parent's kill, UNGROUNDED.
- `_adopt` in `_after_fork_in_child` gives a forked child a fresh condition, set and thread, so it does not wait on a lock its parent's thread held.
- An interrupt can only make Z3 return `unknown` (AUDIT-41's `z3int.py` result, unchanged), which `_decide` maps to UNGROUNDED.

**Overlapping pauses counted once (`_count`).** Apart from #1, `_off` is set by the first of `_paused`/`_frozen` and closed by the last, under `_Interrupt._lock` for both `pause`/`resume` and `freeze`/`thaw`. `ran_s` drives scheduling (hardness, `_yields`, `stuck`) and `solver_ms`; neither feeds a verdict or `consistency_score`.

**Stress (scratch, not committed).** `stress.py`: per seed, executors `(workers, slow) ∈ {(1,0), (2,0), (1,1)}`, 4 rounds each of 8–24 concurrent callers, 1–4 checks each, timeouts 0.1–0.5 s, 10 % cancelled within 0–0.2 s. Checks: valid; not entailed; inconsistent premises; Fermat n=3; a false Fermat equality. `SolverExecutor._pause` was wrapped to count newcomers stopped and re-run.

| Run | Checks | Cancels | Stopped and re-run | Wrong verdicts | Negative `solver_ms` |
|---|---|---|---|---|---|
| Seed 1 | 396 | 22 | 12 | 0 | 0 |
| Seed 2 (2 runs) | 384, 387 | 23, 20 | 20, 17 | 0 | 0 |
| Seed 3 | 386 | 31 | 14 | 0 | 0 |
| Seed 4 | 495 | 37 | 23 | 0 | 0 |

Every valid check came back VALID and every Fermat check UNGROUNDED; nothing else came back VALID.

**Unconfirmed stall (not ranked).** One earlier, uninstrumented run of seed 2 stopped making progress: after 255 s the parent sat at 0.5 % CPU with one worker process in state `T` (SIGSTOPped) for 253 s, while another agent's load probe shared the machine. I killed it to instrument the script (slot and executor state dumped after 20 s without progress); the instrumented seed 2 (twice), 3 and 4 all finished in under 2 minutes each, so I have no state dump and no repro. If real, it is a frozen check that no `_balance` thaws, which fails closed (no verdict, the caller waits), not an admission. Worth a targeted look at who thaws a lent check frozen by a due lender when that lender leaves through the `slow` path (`_to_slow` → `unstick` → `release(lent=False)` while `lent > stuck`), which this audit did not get to.

## Re-check of earlier soundness fixes

At `2a6ee8f`, `uv run pytest tests/audit/test_audit_{1,5,9,12,13,17,21,25,28,29,30,33,37,41}.py -q -p no:randomly` gave 138 passed and 31 xfailed in 57 s. AUDIT-41 #1 passes (flipped by SG37). No xfail is a regressed soundness fix. Still open: AUDIT-21 #1 (medium) and #3 (low); AUDIT-33 #1, #2 and AUDIT-37 #1 (fork during a worker start).

## How this was measured

- **Machine.** A 4-CPU container shared with other agents, CPython 3.11, Z3 4.16.0.
- **Repeat runs.** `uv run pytest tests/audit/test_audit_45.py -q -p no:randomly`: 1 passed / 1 xfailed in 2 of 2 runs, 11–14 s each. With `--runxfail`: 1 passed / 1 failed on `assert got["negatives"] == 0` (1 == 0).
- **Timing.** No absolute times. The hard check's timeout is 40 × an easy check measured through the same executor in the same script (never less than 20 of `_SLICE_S`); the stream lasts 4 timeouts; the injected delay is a multiple of the easy check.
- **Simulation.** #1 injects a sleep at the start of `SolverExecutor._call` to stand in for a pool thread that starts late (scheduler latency, a new pool thread). Everything else goes through the public `SolverExecutor.run`.
- **Scratch.** `neg.py` and `stress.py` lived in this agent's private scratch folder and were deleted.
- **Gates.** `make lint typecheck` passes.

## Bottom line

SG37 did not open a false-negative path. A due lender running on, the frozen lent check, newcomers stopped and run again, the slot left to a lent check, and the shared watchdog never return a stale reply or another caller's verdict, and never turn a timeout or UNKNOWN into VALID. The only finding (#1, low) is that a lent check frozen before its call starts can report a negative `solver_ms`, which makes `Verifier.verify` raise instead of returning; it fails loud and needs a late pool thread. The open soundness items are still AUDIT-21 #1 (medium) and #3 (low).
