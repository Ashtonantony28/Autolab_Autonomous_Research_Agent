# AUDIT-37: Encoding soundness, round 10 (false negatives)

Auditor: an independent agent. It wrote none of the code and none of the earlier audits.
Base: `origin/claude/sg35-stopped-newcomers` at `1e537af` (SG35).
Scope: what changed since round 9, all in SG35:
- `solver.py` scheduling: a stopped newcomer is queued again with its first arrival number and `lend=False`; `_Interrupt.pause()`/`resume()` keep worker start, boot and replacement out of run time; a slot whose check waits for its worker is not stuck.
- `solver.py` stops: `_Interrupt.request()` now returns whether it stopped a target, and a stop that finds no target (the check has already left its worker) keeps the result instead of running the check again.
- `solver_workers.py`: a forked child removes the owner's workers from `multiprocessing.process._children`.

`agents/*`, `orchestrator/*` and `tools/*` have not changed since round 6, so the AUDIT-21/25 probes of the Design cost/duration path still apply. AUDIT-21 #1 / AUDIT-23 #1 (medium) is still open, unchanged.

"Admitted" means the same as in AUDIT-21: `VerificationMonitor().decide(verdict)` returns PASS, or a Design is merged without the cost/duration pause that principle 7b requires.

Summary: **0 critical, 0 high, 0 medium, 1 low.**

**Soundness held.** I found no way to get a wrong trace admitted:
- A kept result is always this check's own reply, or UNGROUNDED. It is never a stale reply, a stopped run's outcome or another caller's verdict.
- No re-queue, pause or stop race returned VALID for a check that is not entailed, has inconsistent premises or makes a false claim. Two stress runs had 201 stops, 44 of them kept, and none of those results was wrong (below).

The one finding is a gap in SG35's fork fix. It fails closed: a worker dies, and the owner's check on it is UNGROUNDED.

The only testable finding has a repro in `tests/audit/test_audit_37.py`, marked `xfail(strict=True, reason="AUDIT-37 #1")`, with one plain control. Nothing calls a real LLM.

---

## Low

### #1 A child forked while the owner starts a worker still kills that worker when it exits

**Impact.** SG35 fixed AUDIT-35 #1 / AUDIT-36 #1 by having a forked child drop the owner's workers from `multiprocessing.process._children`. It drops only the workers in `Workers._live` (`solver_workers.py:190-192`). A new worker gets into `_children` before it gets into `_live`:
- `BaseProcess.start` adds the process to `_children` as its last step.
- `Workers._start` (`solver_workers.py:209-212`) adds the worker to `_live` only after `Worker.__init__` returns, after `child.close()` and the field set-up.

A raw `os.fork()` in that gap gives a child whose `_children` still holds that worker. A pre-forking server forks this way while the prestart thread starts workers, or while a pool thread starts a replacement in `Workers.discard` after a timeout, cancel or stop. When the child exits normally (`sys.exit`, or a return from main), multiprocessing's atexit `_exit_function` does two things:
- It SIGTERMs the owner's worker.
- It then raises `AssertionError: can only join a child process` in the child's atexit. This is printed as "Exception ignored in atexit callback".

Effects in the owner:
- A check running on that worker ends `UNGROUNDED / worker_died` ("worker died (signal 15)", or "worker failed to start" if the signal lands during its boot) instead of running to its own end.
- An idle victim is skipped by `acquire` (`alive()` is False), and a fresh worker is started in its place.

Verdicts fail closed: every outcome on this path is UNGROUNDED, and `worker_died` does not change routing. The gap is a few bytecodes per worker start. Without simulation, I forked a waiting child every 20 ms right after `default_executor()`, during prestart: 10 runs and 9 to 17 children per run killed no worker. So the cost is a lost check now and then, not a broken run. The window falls inside AUDIT-33 #1/#2's wider "fork during a worker start" window, and the same fix closes all three.

**Repro (committed).** `_EXIT_SCRIPT` in `tests/audit/test_audit_37.py`:
1. It waits for prestart, then measures how long a raw forked child takes to exit, three times. The check's timeout is 50× the slowest of the three.
2. It runs two rounds. Each round makes `Workers.discard` start a replacement, runs a hard Fermat n=3 check on that replacement, and has a raw-forked child exit normally mid-check.
   - **Control.** The child is forked after the start. This is SG35's fix working: the worker is alive after the child exits, and the check ends `UNGROUNDED / timeout` ("solver returned unknown").
   - **Window.** The fork happens inside `ForkServerProcess.start`, right after the real start, so before `_live.add`. The worker is dead after the child exits, and the check ends `UNGROUNDED / worker_died` ("worker died (signal 15)" in 3 of 4 scratch runs, "worker failed to start" in 1).

Tests:
- Control: `test_a_child_forked_outside_a_start_spares_the_owners_worker`.
- xfail: `test_a_child_forked_during_a_worker_start_spares_that_worker`. It fails on `assert window["alive"]` in 5 of 5 runs under `--runxfail`.

**Root cause.**
- `solver_workers.py:186-195` (`Workers._adopt`) uses `_live` as the list of the owner's processes, but `_live` lags `_children`.
- `solver_workers.py:209-212` (`_start`) and `Worker.__init__` (line 100) run on threads other than the one that forks.

**Suggested fix.**
- In `_after_fork_in_child`, drop every `_children` entry whose `name == "z3-worker"` (`_NAME`), or every entry whose `_parent_pid` is not this process. A raw-fork child has none of its own yet.
- Better: take a module lock around `Workers._start` and acquire it in an `os.register_at_fork(before=...)` hook, releasing it in both after-hooks. That closes this gap and AUDIT-33 #1/#2 (held tracker/import locks, leaked pipe end) together.

---

## Probed and held up

**A stop that keeps its result (`run`, `_Interrupt.request`).**
- `_yields` stops a check only if `interrupt.attached()`. A target goes from none to the worker to none exactly once per check, so a `request()` that finds no target always comes after `detach()`. It never comes before `attach()`, where the check would have returned `_INTERRUPTED` and then been kept as a wrong UNGROUNDED.
- After `detach()`, the pool thread sees `requested()`. It discards the worker, whose pipe is already drained, and returns `reply` unchanged. That `reply` matched this call's sequence number in `Worker.call`, so it is this check's own outcome. If `reply` is None (the worker timed out or died on its own), the kept result is `_INTERRUPTED`, which is UNGROUNDED. Its cause is given as "interrupted" rather than timeout or died. That is a details-only mislabel; routing reads only `no_premises` and `premises_inconsistent`.
- `request()` kills only under `_Interrupt._lock`, and `detach()` takes that lock before `release()`. So a stop never kills a worker that another caller has since acquired, and a kept result cannot come from another caller's run.
- When `request()` does stop a target, `stopped` is True and the result is thrown away as before (AUDIT-33's probes of that path still hold).
- After `shutdown`, an already requested interrupt makes `request()` return False. The result is kept, and it is `_INTERRUPTED` or the real reply, as before.

**Run-time pauses.**
- `pause()`/`resume()` change only `ran_s()`, which is used by `_yields`, `stick`, the hard/short record and `solver_ms`. None of these feeds a verdict or the score; `solver_ms` is display only.
- The Z3 budget is still enforced by `Worker.call(args, timeout + grace)` from when the worker is ready, and that is unchanged.
- `ran_s()` on the loop thread reads `_paused` twice and `_overhead_s` once while the pool thread writes them. On CPython 3.11 there is no eval-breaker check between those loads, so the read is atomic in practice. Under free-threaded builds, `now - None` (TypeError) or an undercount would be possible. Only scheduling could be affected, not a verdict. This is not a finding on the pinned runtime.
- On the pool thread, the `solver_ms` that `_call` returns is computed after every pause has ended, so it is ≥ 0. The `StepVerdict.solver_ms ≥ 0` validator never tripped: no negative value appeared in either stress run.

**Re-queued newcomers.** The waiter is inserted at its first arrival number and `lend=False`, so `_grant(short, new and lend)` never lends it a place again: it is stopped at most once per `run`. `_lent_rank` skips it, and `lendable()` keeps it out of `lent_past`. After both stress runs, the place counters were all back to zero: `held`, `lent`, `stuck`, waiters, `_slow` and `_running`.

**Fork fix.** `_adopt` discards every `_live` worker, busy or idle, from `_children`. The control above shows that a busy worker survives the exit of a child forked outside a start. A worker that `release` or `close` is stopping leaves `_live` before `stop()` joins it, but it is being killed anyway.

**Stress (scratch, not committed).** 30 rounds of 8–32 concurrent runs through `default_executor()`:
- Half of the runs were hard Fermat runs that start together. The rest made 1–4 random checks each: valid, not entailed, inconsistent premises, a false Pythagorean claim, or a false Fermat equality.
- Z3 timeouts were 0.15–0.4 s, and 20% of runs were cancelled within 0–0.3 s.
- `_yields` and `_Interrupt.request` were wrapped to count the stops that killed a worker and the stops that kept a result.

| Run | Stops that killed | Stops that kept | Cancels | Wrong verdicts |
|---|---|---|---|---|
| Seed 1, unmodified | 133 | 0 | 116 | 0 |
| Seed 2, forced keep window | 24 | 44 | 105 | 0 |

In seed 2, `_yields` sleeps 0–0.4 s after it decides to stop, so the check often leaves its worker before `request()`.

Every check that is not entailed, has inconsistent premises or makes a false claim came back INVALID or UNGROUNDED, never VALID. No run raised. Without the forced window, the keep path was never reached in 133 stops: its window is the few bytecodes between `_yields` and `request()`.

## Re-check of earlier soundness fixes

At `1e537af`, `uv run pytest` on `tests/audit/test_audit_{1,5,9,12,13,17,21,25,28,29,30,33}.py` with `-q -p no:randomly` gave 135 passed and 30 xfailed. No xfail is a regressed soundness fix. Still open:
- AUDIT-21 #1 (medium) and #3 (low).
- AUDIT-31 #1 (`worker_died` on a non-final step).
- AUDIT-33 #1 and #2 (fork during a worker start).

## How this was measured

- **Machine.** A 4-CPU container shared with other agents, CPython 3.11, Z3 4.16.0.
- **Repeat runs.**
  - `uv run pytest tests/audit/test_audit_37.py -q -p no:randomly` gave 1 passed / 1 xfailed in 5 of 5 runs, 13–34 s each.
  - With `--runxfail` it gave 1 passed / 1 failed in 5 of 5 runs, each on `assert window["alive"]`.
- **Timing.** No absolute times. The check's timeout is 50× the slowest of three measured raw-fork child exits (0.17–0.44 s here), so the check is still running when the child exits.
- **Simulation.** #1 forces the fork into the gap by wrapping `ForkServerProcess.start`. The unsimulated rate above (0 kills in 10 runs, a child forked every 20 ms during prestart) comes from the scratch `exitrate.py`.
- **Scratch.** `stress.py`, `stress_keep.py`, `exitkill.py` and `exitrate.py` lived in this agent's private scratch folder and were deleted.
- **Gates.** `make lint typecheck` passes.

## Bottom line

SG35 did not open a false-negative path. A stop that finds its check already off its worker keeps that check's own reply, or UNGROUNDED. It never keeps a stale reply or another caller's verdict: 44 forced kept stops, 0 wrong. The run-time pauses and arrival-ordered re-queue change only scheduling.

The fork fix misses workers that are mid-start. A child forked in that gap still SIGTERMs the newest worker when it exits normally (#1, low, fails closed, not seen without simulation). The same lock that AUDIT-33 #1/#2 call for would close it.

The open soundness items are still AUDIT-21 #1 (medium) and #3 (low).
