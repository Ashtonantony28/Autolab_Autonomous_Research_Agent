# AUDIT-33: Encoding soundness, round 9 (false negatives)

Auditor: an independent agent. It wrote none of the code and none of the earlier audits.
Base: `origin/claude/sg34-together-fork` at `10ae977` (SG34 on the round-8 base `f09ea60`).
Scope: what changed since round 8, all in SG34:
- Scheduling (`solver.py`): newcomers lent places latest first; a newcomer's check on a lent place is stopped and queued again for a slot (`_yields`, `stopped`); `_LENT_AGING`.
- Fork adoption (`solver.py`, `solver_workers.py`): pid checks, `os.register_at_fork` hooks, closing inherited idle pipes, `_forget_forkserver`.

`agents/*`, `orchestrator/*` and `tools/*` have not changed since round 6, so the AUDIT-21/25 probes of the Design cost/duration path still apply.

Every testable finding has a repro in `tests/audit/test_audit_33.py`. Each repro asserts the correct behaviour and is marked `xfail(strict=True, reason="AUDIT-33 #n")`. With `--runxfail`, each fails on its stated assertion in 5 of 5 runs. Three plain control tests pass. Nothing in the file calls a real LLM.

"Admitted" means the same as in AUDIT-21: `VerificationMonitor().decide(verdict)` returns PASS, or a Design is merged without the cost/duration pause principle 7b requires.

Summary: **0 critical, 0 high, 1 medium, 2 low.**

**Soundness held.** I found no way to get a wrong trace admitted:
- A stopped-and-rerun newcomer check never returns the stopped run's outcome (except after `shutdown`, where it is UNGROUNDED).
- No re-queue race, and no cancel during a stop, produced a VALID for a non-entailed or inconsistent check (two stress runs, 252 stops, below).
- A forked child never used, read from or killed the parent's workers, and the parent never read the child's replies.

All three findings are gaps in SG34's fork adoption. Each fails closed: a check hangs or raises, or a dead worker is reported with the wrong cause. Only #1 stops a run.

---

## Medium

### #1 A process forked while the owner is starting a worker cannot verify: its checks hang, or raise on every call

**Impact.** SG34's at-fork hook resets one lock that a worker start holds: `forkserver._forkserver._lock`. It does not reset two others that the same start path takes:
- **The resource tracker's lock.** `forkserver.ensure_running` takes `resource_tracker._resource_tracker._lock` (an RLock) on every worker start. On the first start it holds the lock while it spawns the tracker.
- **An import lock.** The first `Process.start` imports `multiprocessing.popen_forkserver` lazily, under that module's import lock.

The owner starts workers on other threads:
- SG32's prestart thread starts them as soon as a `Verifier` is built (about 0.3 s for 4 workers, back to back).
- `Workers.discard` starts a replacement after every timeout, cancel or SG34 stop.

A process forked during one of these starts gets the lock held by a thread that does not exist in it. What happens next depends on the case:
- **It hangs.** The child's pool thread blocks forever in `resource_tracker.ensure_running` (`with self._lock`, line 99), or in `importlib`'s module lock. No timeout covers worker start-up: `interrupt.attach` happens after `acquire`. A cancel leaves the thread blocked, and that thread holds the child's new forkserver lock, so every later check blocks too. The orchestrator has no timeout around `verify`, so the run never ends.
- **It raises `ImportError` on every check.** The error is "cannot import name 'Popen' from partially initialized module 'multiprocessing.popen_forkserver'". `escalate_permanent_errors` does not catch `ImportError`, so the agent node crashes the run, as in AUDIT-29 #1.
- **It raises `ReentrantCallError` on every check.** This happens when the child's pool thread gets the dead holder's thread id, so the RLock counts the call as re-entrant. `ReentrantCallError` is a `RuntimeError`, so every run in the child pauses at an error HITL.

This is the AUDIT-29 #1 pattern: `build_graph()`, then fork, as in a `multiprocessing.Pool` under "fork" or `gunicorn --preload`. SG34 flips that repro only because the repro waits for prestart to finish before it forks.

**Repro (unsimulated, scratch).** The script calls `default_executor()`, as `Verifier.__init__` does, forks at once, and the child verifies `x > 5 ⊨ x > 0`, then `x > 5 ⊨ x > 9`. Over 20 runs:

| Child outcome | Runs |
|---|---|
| `VALID INVALID` (correct) | 12 |
| first check hangs (30 s `wait_for`), then `ImportError` | 6 |
| hangs on every check | 2 |

`faulthandler` in a hung child shows the pool thread at `resource_tracker.py:99 ensure_running` (2 runs) or at `importlib._bootstrap ... acquire` from `context.py:299 _Popen` (1 run). The parent got `VALID INVALID` in every run.

**Repro (committed, deterministic).** `_TRACKER_SCRIPT` runs these steps:
1. The parent verifies once and prestarts its workers.
2. It forks a control child with nothing held. The control child verifies; this is SG34's fix working.
3. A parent thread holds the tracker's lock, exactly as `forkserver.ensure_running` does during a start.
4. The parent forks again. The second child gets 10× the control child's measured time to verify.

Tests:
- Control: `test_a_child_forked_with_nothing_held_verifies`.
- xfail: `test_a_child_forked_during_a_worker_start_verifies`. Today it gets `ReentrantCallError: Reentrant call into the multiprocessing resource tracker` in 5 of 5 runs: here the child's pool thread reuses the holder's thread id. Otherwise the child gives "no answer".

**Root cause.**
- `src/autolab/verification/solver_workers.py:277-295` (`_forget_forkserver`, `_after_fork_in_child`) resets only the forkserver's state.
- Worker start (`Worker.__init__`, line 95) runs on threads other than the one that forks.

**Suggested fix.**
- In the child, also reset `resource_tracker._resource_tracker._lock` to a new `threading.RLock()`. Keep its `_fd` and `_pid`: a forked child shares the parent's tracker by design.
- Import `multiprocessing.popen_forkserver` (and `popen_spawn_posix`) eagerly, at `solver_workers` import time on the main thread, so no start ever imports it.
- Or block forks during a start: take a module-level lock around `Worker.__init__`, and acquire it in a `before=` at-fork hook. That also closes #2's window.
- As a backstop, bound worker start-up: `acquire` with no deadline is the only unbounded wait on the check path.

---

## Low

### #2 A fork during a worker's start keeps that worker's pipe open: its death reads as a timeout, after the whole budget

**Impact.** `Worker.__init__` holds the worker's end of the pipe (`child`) from `ctx.Pipe()` until `child.close()` (line 101), across `process.start()`. A process forked in that window keeps a copy of the worker's end. `_adopt` cannot close that copy: only a local variable of a thread that no longer exists refers to it.

The window is a few to tens of milliseconds per start. Prestart is about 0.3 s of these windows back to back, and every discard opens one. Once a fork lands in the window, the owner never sees EOF when that worker dies:
- A killed, cancelled or SG34-stopped check on it waits out its whole budget (timeout + grace, 5.5 s by default) instead of returning at once. It holds its pool thread meanwhile, and a slow check holds its slow room.
- A worker that crashes reads as `fault == ""`, so the check is reported as `UNGROUNDED/timeout` ("worker killed past the timeout"), not `UNGROUNDED/worker_died`.
- A worker that dies during boot is only noticed after `_BOOT_S` (60 s).

The copy also holds the parent's forkserver "alive" pipe if the fork lands before `_forkserver_alive_fd` is set, which happens during the first start. The parent's forkserver, and its idle workers, then outlive the parent for as long as the child lives.

Verdicts are unchanged: every outcome on this path is UNGROUNDED.

**Repro.** `_LEAK_SCRIPT` wraps `ForkServerProcess.start` so that a sleeping child is forked right after the real start, before `Worker.__init__` closes `child`. It then kills each worker 0.1 s into a Fermat n=3 check.

| Worker | `fault` | Returned after |
|---|---|---|
| clean (control) | `died` | ≈ 0.1 s (the kill) |
| forked mid-`__init__` | `""` | its whole budget, set to 20× the clean time |

Tests:
- Control: `test_a_killed_worker_reads_as_died_at_once`.
- xfail: `test_a_fork_during_worker_start_does_not_hide_its_death`.

**Suggested fix.** Serialize worker starts with forks, using the `before=`/`after_in_parent=` hook lock from #1. Or create the pipe with `os.pipe2(O_CLOEXEC)`. That alone does not help a plain fork, so the lock is the fix.

### #3 A forked child reads the owner's worker exit statuses

**Impact.** The at-fork hook leaves `multiprocessing.process._children` alone. A child made by a plain `os.fork()` therefore still lists the owner's workers; a pre-forking server forks this way, while `multiprocessing`'s own fork bootstrap clears the set. The child's first worker start runs `BaseProcess.start` → `_cleanup()`, which polls each listed worker. For any worker that has exited, it reads the exit status that the forkserver wrote on the owner's sentinel pipe.

This is the one place I found where one process reads a message meant for the other. It cannot change a verdict, because exit codes feed only `_lost`'s details. The owner then reads EOF and records exit code 255. A worker that died mid-check is reported as "worker died (exit code 255)" instead of "(signal 9)", and the parent's `join` loses the true cause.

**Repro.** `_STEAL_SCRIPT`:
1. The parent prestarts two workers.
2. It forks a child that waits.
3. It kills and joins one worker. This is the control: -9.
4. It kills the other worker without joining, and then lets the child verify.
5. After the child exits, the parent joins the killed worker.

The child verifies `VALID`. The killed worker's `exitcode` is 255, not -9, in 5 of 5 runs.

Tests:
- Control: `test_the_child_verifies_and_a_joined_worker_reads_its_signal`.
- xfail: `test_a_forked_child_leaves_the_owners_exit_statuses_alone`.

**Suggested fix.** In `_after_fork_in_child`, remove the owner's workers from `multiprocessing.process._children`. Every `Worker.process` the hook adopts away is one of them, so discard each one from `_children` before forgetting it.

---

## Probed and held up

**Stopped newcomers (`run`, `_yields`).**
- `stopped` is set only on the branch that requests the interrupt. The outcome of the stopped run is returned only when `self._closed` is set, and that outcome is `_INTERRUPTED` (UNGROUNDED).
- Otherwise the loop calls `slots.acquire` again with a fresh `_Interrupt`, a new job and a new future. Nothing from the stopped run is carried over.
- `fn` is `partial(Z3Solver.check_entailment, ...)`, which is stateless, so a rerun recomputes from scratch.
- **Race 1: the reply arrives between `_yields`'s `attached()` and `request()`.** The interrupt finds no target. `check_entailment` sees `requested()`, discards the worker (whose pipe is now empty), and returns the true outcome. The outcome is then thrown away and the check rerun. This only costs time.
- **Race 2: the kill lands after the worker sent its reply.** The parent reads that reply: it carries this call's sequence number, so it is this check's own answer. The worker is still discarded.
- `detach()` always precedes `release`/`discard`, so a stop never kills a worker that another thread has since acquired.
- **A cancel during a stop.** `await future` raises, the place is released in `finally`, and the classification is skipped. No outcome is returned.
- A stopped caller re-queues with `short = new = False`. It can no longer be lent a place, so it is stopped at most once per `run`.
- After both stress runs, the place counters were all back to zero: `held`, `lent`, `stuck`, waiters, `_slow` and `_running`.

**Stress (scratch `stress.py`, not committed).**
- Setup: 30 rounds of 8–32 concurrent runs. Half of them were hard Fermat runs that start together. The rest were 1–4 random checks each: valid, not-entailed, inconsistent, a false Pythagorean claim, or Fermat n=3. Z3 timeouts were 0.2–0.4 s, and 25% of runs were cancelled within 0–0.3 s.
- Seed 1: 131 stops, 125 cancels, 0 bad verdicts.
- Seed 2: 121 stops, 150 cancels, 0 bad verdicts.
- Every not-entailed, inconsistent or false check was INVALID or UNGROUNDED, never VALID.

**Fork adoption.**
- Each child gets a new forkserver (a new pid) and its own workers. Its copies of the owner's idle pipes are closed, and it never calls `alive()`, `kill()` or `join()` on the owner's workers.
- The owner's idle workers stay alive after the child exits.
- Fork stress (scratch `forkstress.py`): the parent forked 25 children at random times while it ran 8 concurrent checks per round (0.15 s timeouts, 30% cancelled). Every child gave `VALID INVALID INVALID VALID`.
- The parent's checks gave no wrong VALID. Four easy checks timed out under the fork load: UNGROUNDED, failing closed.
- The parent cannot read a child's reply: they share no pipe end that either process reads. The leaked copies in #2 are never read.
- The forkserver-forked workers run the at-fork hooks too. They are harmless there: the worker has no `Workers` or executors to adopt, and no forkserver state to forget.

**Unchanged verdict paths.** `Worker.call`'s sequence match, `worker_died` handling and the builtin probe are unchanged since round 8. The AUDIT-29 analysis still applies.

## Re-check of earlier soundness fixes

At `10ae977`, `uv run pytest tests/audit/test_audit_{1,5,9,12,13,17,21,25,28,29,30}.py -q -p no:randomly` gave 131 passed and 28 xfailed. No xfail is a regressed soundness fix. AUDIT-21 #1 (medium) and #3 (low), and AUDIT-31 #1 (`worker_died` on a non-final step), are still open.

## How this was measured

- **Machine.** A 4-CPU container shared with other agents, CPython 3.11.17, Z3 4.16.0. Prestart of 4 workers took 0.31–0.55 s.
- **Repeat runs.**
  - `uv run pytest tests/audit/test_audit_33.py -q -p no:randomly` gave 3 passed / 3 xfailed in 5 of 5 runs.
  - With `--runxfail` it gave 3 passed / 3 failed in 5 of 5 runs, each on its stated assertion: `'ReentrantCallError…' == 'VALID'`, `'' == 'died'` and `255 == -9`.
- **Timing.** No repro asserts an absolute time.
  - #1 gives its child 10× the control child's measured fork-and-verify time (0.28–0.52 s here).
  - #2's budget is 20× the clean worker's measured kill-to-return time, and the assertion is 5× that time.
  - #3's wait after the kill is 10× the measured time to kill and join the control worker.
- **Simulation.** #1 and #2 force the fork into the window: #1 holds the tracker lock from a thread, and #2 forks from inside `start`. The unsimulated rate for #1 (8 of 20 forks right after `default_executor()`) comes from the scratch `forkearly.py`.
- **Scratch.** The scratch scripts lived in this agent's scratch folder and were deleted.
- **Gates.** `make lint typecheck` passes.

## Bottom line

SG34 did not open a false-negative path. A stopped and re-queued newcomer never returns the stopped check's outcome, and no re-queue race or cancel during a stop came back VALID in 252 stops. A forked child never uses the owner's workers, and neither process reads the other's replies.

The fork adoption is incomplete. It resets the forkserver but not the resource tracker lock or the import lock that the same worker start holds. A fork that lands during prestart (about 0.3 s right after `build_graph()`) or during a replacement start breaks every check in the child: it hangs, raises `ImportError` or pauses on `ReentrantCallError` (#1, medium, 8 of 20 immediate forks). The same window leaks the worker's pipe end, so the owner reports that worker's death late and as a timeout (#2, low). A plainly forked child also reads the owner's worker exit statuses (#3, low).

All three fail closed. The open soundness items are still AUDIT-21 #1 (medium) and #3 (low).
