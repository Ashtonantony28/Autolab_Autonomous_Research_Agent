# AUDIT-38: Performance under load, round 10

Auditor: independent. I wrote none of the audited code or the earlier audits. Base: `origin/claude/sg35-stopped-newcomers` @ `1e537af` (SG35 on top of round 9).
Machine: 4 CPUs, Linux, Python 3.11.17, z3 4.16.0, no cgroup CPU quota, so the defaults are 2 slots and 2 slow places. Other agents shared the machine: load average was 0.2 at the start and 5.8–13.5 for most measurements, so compared trees were always measured back to back.
All runs use Z3 checks or stubs. Nothing touches the network. No source file or existing test was changed.

- **Repros:** `tests/audit/test_audit_38.py` has 3 tests, each `xfail(strict=True, reason="AUDIT-38 #n")` and asserting the correct behaviour; the load test also carries `perf`. With `--runxfail -p no:randomly` all 3 fail on their assertions (2 of 2 runs, 6.5 s). Without it, 3 xfailed (6.3 s).
- **Measurements:** `uv run python -m scripts.audit_load_10 [--burn N] [section ...]`, sections `requeue`, `bound`, `pause`, `fork`. `scripts.audit_load_10 proto <dir>` writes the SIGSTOP prototype used in #2. AUDIT-34's own load was re-run with `scripts.audit_load_9 requeue waste`.
- **Versions:** "pre-SG35" is `git archive 6c16dce`, "pre-SG34" is `git archive dcef66c`, both run with `PYTHONPATH=<archive>/src:.` from the same venv.

| # | Severity | Title |
|---|---|---|
| 1 | Medium | A newcomer that gave way to a short caller is queued for a slot only. Its lent place idles once the short check ends, and the newcomer waits for a stuck hard check to end: 3.1–5.1 s at the default 5 s timeout with no hard run ahead, 8.1–8.8 s behind 4 aged ones, once 13.4 s. This is exactly pre-SG34's wait. |
| 2 | Medium | AUDIT-34 #2 is unchanged: 5.2–5.6 Z3 processes run at once on 4 CPUs (more than 4 in 85–96 % of samples), and a fifth of Z3 time goes to killed checks. The ADR's trade-off is not forced: a prototype in which the lending slot pauses its own worker (SIGSTOP) keeps the bound (3.9–4.0 running) with the same newcomer waits and untouched aging. |
| 3 | Low | A child forked between a worker's `Process.start()` and `Workers._live.add` keeps that worker in multiprocessing's `_children`, so its normal exit SIGTERMs the owner's worker (3 of 3 with the window widened; 0 of 255 natural forks). |

In total: two medium findings and one low. None is critical or high.

**AUDIT-34 #1 is fixed.** The same load as round 9 now matches pre-SG34 sample for sample (table under #1), against 15.4–18.6 s on pre-SG35 today and 18.7–20.5 s in round 9. **AUDIT-34 #2 stays open** (#2 here).

---

## 1. Medium: a newcomer that gave way waits for a slot while its lent place idles

**What SG35 does.** A newcomer stopped on a lent place after one slice, because a short caller waits, is queued again with its first arrival number at the newcomer rank, but with `lend=False`: it may take a slot only (`SolverExecutor.run`: `short, new, lend = False, not hard, False`).

**Why that is the wrong resource.** A slot lends a place exactly when its check is stuck, that is, a hard check that will hold the slot up to its whole timeout. Short callers' checks take about a millisecond. So a moment after the short caller takes the lent place, it gives it back, and nobody may use it (`_grant(short=False, new and lend=False)` returns None for the newcomer; hard callers are never lent). The newcomer waits for the next slot, which frees only when a stuck hard check ends or moves to a slow place. Aged hard callers (SG30 rank 0) ahead of it go first, so the wait grows with them.

### Measurements (`audit_load_10 requeue`, `audit_load_9 requeue`)

Setup as AUDIT-34 #1: 2 slots and 2 slow places, 16 known-hard runs, 4 short runs, and a probe that is one 150 ms check in a new task. The queue snapshot is taken when the probe is queued again.

| Load | HEAD | pre-SG34, same session | pre-SG35, same session |
|---|---|---|---|
| 0.4 s timeout, 12 hard, 3 short (hard checks) | 0.5 1.8 0.7 1.8 1.1 | (round 9: 0.7–2.2) | 2.8 2.9 2.9 3.1 3.1 |
| 0.4 s, 16 hard, 3 short (hard checks) | 0.6 0.5 3.0 0.7 2.3 | (round 9: 1.1–2.4) | 4.2 4.0 4.9 4.4 4.9 |
| **5 s, 16 hard, 4 short (s)** | 5.32 8.43 3.42 3.43 8.42 3.37 | 5.33 8.46 3.50 3.22 8.42 3.46 | 15.4 18.4 18.6 18.6 |
| 5 s, other HEAD runs (s) | 0.2\* 3.5 **13.4** 3.4; 0.5\* 0.0\* 0.0\* 0.0\* 3.1 8.8 3.1 3.6 | | |

\* not stopped: lent a place and finished on it.

What the queue held at the re-queue, and how long the probe then waited:

| Timeout | Aged hard callers ahead → wait |
|---|---|
| 5 s | 0 → 3.0–5.1 s (6 samples); 1 → 3.0 s; 4 → 8.1–8.2 s (4 samples) |
| 1 s | 0 → 0.34–0.39 s; 5 → 1.4–1.6 s; 8 → 2.4–2.6 s |

- **Bimodal.** A newcomer that is never stopped is served in 0.0–0.5 s. One that gives way waits 0.6–2.7 hard checks, the pre-SG34 wait: SG34's gain does not reach it.
- **Against the budgets.** 4 of 13 stopped samples at 5 s exceed AUDIT-30 #1's 1.5 hard checks (8.4, 8.4, 8.8 and 13.4 s). 13.4 s is 45 % of the 30 s budget for a whole 10-step trace, spent on one 150 ms check.
- **Who hits it.** As in AUDIT-34 #1: a new task's first check of 50–200 ms while a short caller waits.

### Repro

`test_a_newcomer_that_gave_way_is_served_before_the_slot_frees`, deterministic (fake clock, events), about 1 s. Setup: 1 slot, its check stuck. A newcomer is lent the place, runs 2 slices, and gives way to a short caller, whose check ends at once. The test asserts the newcomer is served within 20 slices while the slot's check still runs. Observed: still waiting, `(held, lent, stuck) = (1, 0, 1)`, so the lent place is idle. It is served only after the slot frees.

### Suggested fix

- Keep `lend=True` for a newcomer that gave way, but stop it again only at `_HARD_SLICES`, never again for a short caller. It then takes the lent place after the short callers. The latest-first order and `_LENT_AGING` bound how many short callers go first (4 lent grants with 2 slots). Slot aging is untouched, because lent places are not slot grants.
- Or hand the place back directly: when the short caller that took it releases it, grant it to the newcomer that gave way before anyone else.

---

## 2. Medium: the CPU bound is still broken, and the trade-off in ADR 0009 is not forced

### Re-measured (`audit_load_10 bound`, `audit_load_9 waste`; 2 + 2 places; 1 check per fresh task; 0.4 s; 8 s windows)

Running Z3 processes are counted from `/proc` (state R, sampled every 2 ms), not as calls in flight.

| Load | HEAD | Pre-SG34, same session | SIGSTOP prototype, same session |
|---|---|---|---|
| 8 runs | 34–39 re-runs, 8.8–9.3 s Z3 wasted, 43–44 restarts; running mean 5.10–5.24, peak 6, **> 4 in 85–86 %** | 0 re-runs, 1 restart, mean 3.81, peak 4 | mean 3.89–3.90, > 4 in 11–17 %; re-runs and waste as HEAD (32–41, 7.4–8.8 s), 56 restarts |
| 12 runs | 34–43 re-runs, 8.9–10.1 s wasted, 46–50 restarts; mean 5.24–5.63, **> 4 in 91–96 %** | 0, 3, mean 3.82 | mean 3.92–4.03, > 4 in 11–14 %; 41–44 re-runs, 9.7–9.8 s, 63–65 restarts |
| Easy newcomer beside 8 / 12 / 16 such runs (hard checks; AUDIT-30 #1 load) | 0.1–0.4 / 0.1–1.6 / 0.1–2.5; running > 4 in 60–79 % | not measured | 0.1–0.3 / 0.1–1.4 / 0.0–2.2; > 4 in 5–16 % |

The wasted Z3 time is 21–23 % of all Z3 time, as in round 9. The "done" counts look as good as before (72–83 per 8 s) only because these checks time out on wall time. With 5.2–5.6 processes on 4 CPUs, each gets about 0.71–0.77 CPU-seconds per wall-second of its budget.

### Judging the trade-off

The SG35 amendment says three things cannot all hold:
- the bound (Z3 checks ≤ `workers + slow_workers`);
- AUDIT-30 #1 (newcomers served at once beside hard runs);
- SG30's aging bound.

Its reason is that "holding them to `workers + slow_workers` gives a newcomer only a slot". That premise does not hold. A lent place needs a CPU, not a new one: the stuck slot that lends it can pause its own worker while the place is out.

**Prototype.** `audit_load_10 proto` makes 6 edits to a copy of `solver.py`.
- When a caller is lent a place, it sends SIGSTOP to the worker of one stuck slot's check.
- It sends SIGCONT when the place is given back, or when the lent check moves to a slow place.
- Ranks, aging and the stop rule are unchanged.

**Results.** The running mean falls from 5.1–5.6 to 3.9–4.0, and newcomer waits stay within HEAD's spread (table above). AUDIT-38 #2's repro passes 3 of 3 runs with it and fails on HEAD (49–51 %).
- The remaining samples above 4 (5–17 %) are worker boots and the moments around a pause.
- SG30 aging is untouched, because lent places are not slot grants.

**Costs found (`audit_load_10 pause`).**
- **Budget.** A paused worker uses 0 CPU and resumes correctly. php(8) decides in 0.72 s with a 0.5 s pause, against 0.24 s alone. But Z3's timeout is wall-clock: a runs-to-timeout check at 1.0 s paused for 0.5 s still returns at 1.00 s, so the paused check loses that CPU. It loses at most `_HARD_SLICES` per newcomer it lends to. HEAD already takes the same CPU from every running check by oversubscribing, as shown above. A full fix could extend the paused check's pipe deadline and use a resource limit (`rlimit`) rather than the wall-clock timeout.
- **Waste.** The prototype does not remove the kill-and-re-run waste. Pausing the stopped newcomer's worker and resuming it when it gets its slot, instead of killing it, would remove the re-runs and restarts as well. Z3 keeps its state across SIGSTOP. That part is not prototyped.

**Verdict.** A design that meets all three exists in outline. The amendment should not close the question as an owner's trade-off.

### Repro

`test_z3_processes_running_at_once_never_outnumber_the_places` (`perf`, about 2 s). Setup: a 1-CPU host's defaults (1 slot, 0 slow), a 1.5 s hard check in the slot, and 3 hard newcomers. It asserts that more than one Z3 process runs in at most 5 % of samples. Observed 49–51 %.

It counts running processes, not calls in flight, so a pausing design passes. The AUDIT-34 #2 repros count calls in flight, so they would still fail for such a fix. A fix of that kind should retire them in favour of this one.

---

## 3. Low: a fork between a worker's start and its registration kills that worker at the child's exit

**What happens.**
- `Workers._start` builds a `Worker`, whose `Process.start()` adds the process to `multiprocessing.process._children`, and only then adds it to `_live` under the lock.
- `Workers._adopt` (SG35) discards only `_live` workers from the child's `_children`.
- A fork in between leaves the worker in the child's `_children`. The child's normal exit (`sys.exit`, the end of `__main__`) then has multiprocessing's atexit hook SIGTERM it.
- The child also prints `AssertionError: can only join a child process` from that hook.

**Who hits it.** A prefork master forks while `SolverExecutor`'s prestart thread is starting workers, which takes about 1 s after `Verifier()`. If the owner's worker is in use when it is killed, its check comes back UNGROUNDED `worker_died`. That is AUDIT-35 #1's effect, through a narrower door.

**Measurements (`audit_load_10 fork`, scratch `fork_race`).**
- With the window widened by a 0.5 s sleep after `Worker.__init__`, the owner's worker exited with -15 in 3 of 3 runs.
- Natural forks during prestart hit it 0 times in 255 (180 at 20 ms gaps, 75 back to back). The window is a few bytecodes, so this is rare.

**Repro.** `test_a_child_forked_while_a_worker_starts_spares_it`, a subprocess, about 1.5 s. It asserts the owner's worker survives. Observed exit code -15.

**Suggested fix.** In the child hook, discard every `_children` entry whose process the child did not start: `p._parent_pid != os.getpid()`. Do not key on `_live`. Alternatively, register the worker in `_live` before `start()`.

---

## Questions asked of SG35, and answers

**Can a repeatedly stopped check jump ahead indefinitely?** No.
- After its first stop a caller has `lend=False` and so is never lent again.
- `_yields` applies only on a lent place, so a call to `run` is stopped at most once.
- After the re-run the task is recorded short or hard, so its next check is not a newcomer.
- Its kept arrival puts it ahead only of callers that arrived after it first did.

**Can a non-lendable newcomer starve?** No.
- It ranks 1 for slots and ages by `passed` like any waiter.
- Its wait is bounded by the aged hard callers that arrived before it (snapshot table in #1). That wait is #1's cost, not starvation.
- Its `passed` and `lent_past` restart at 0 on the re-queue. That is harmless, because it was not waiting while it ran.

**Can a check run forever if its worker keeps restarting?** No.
- `check_entailment` never retries: a worker that fails `ready()` within `_BOOT_S` (60 s) returns `failed to start`, and one that dies returns `worker_died`.
- A run makes at most two calls, so its worst case is 2 × (60 s + budget + grace).
- **Side effect.** A newcomer on a lent place is not stopped while its worker boots, so a pathological boot holds a lent place for up to 60 s.
- **Measured.** Under the fresh-task load, 52 boot waits had p50 0 ms, p90 29 ms and max 112 ms. Two were over one slice.

**Unsynchronised pause bookkeeping.**
- `_Interrupt.ran_s()` reads `_paused` before `_overhead_s` without the lock.
- A `resume()` between the two reads subtracts one pause twice, which underestimates the run time and delays a stop by at most a slice.
- It never overestimates, so it cannot cause a false stop or a false "stuck".

**A late stop.**
- If `request()` lands between `detach()` and the pool thread's `interrupt.requested()` check, the result is kept, as SG35 intends.
- The healthy worker is still discarded and restarted. The window is microseconds, so this is not a finding.

**Event-loop cost of the arrival-ordered queue (`scratch slots_cost`).**

| Waiters | Re-queue walking to the front | One slot grant (`sorted` per grant, which is older) |
|---|---|---|
| 1 000 | 0.09 ms | 0.31 ms |
| 10 000 | 1.35 ms | 5.34 ms |

## CLAUDE.md budgets

- **10-step trace in < 30 s.**
  - A single 150 ms first check that gives way can still take 8.4–8.8 s at the default timeout beside 16 hard runs, and once took 13.4 s (#1).
  - This is no worse than before SG34. The round-9 regression to about 20 s is gone.
- **50 ms p95 per step.** Not re-measured: SG35 did not touch the per-step loop work.

## Bottom line

- **What SG35 fixed.**
  - The round-9 high (AUDIT-34 #1): a stopped newcomer's wait is back to pre-SG34's, sample for sample (5.3 / 8.4 / 3.4 s against 5.3 / 8.5 / 3.5 s), from 15–20 s.
  - Start-up no longer counts toward a stop.
  - None of the new requeue rules starves anyone.
- **What remains.**
  - A newcomer that gives way gets none of SG34's gain: its lent place idles while it waits for a hard check to end (#1).
  - The CPU bound is still broken, at 5.2–5.6 Z3 processes on 4 CPUs with a fifth of Z3 time wasted (#2). The ADR presents this as an unavoidable trade-off. A pause-the-lender prototype keeps the bound without giving up newcomer service or aging, so the owner's choice is not forced.
  - The fork exit fix has a narrow race (#3).
  - Still open from round 9: AUDIT-34 #4 (a forked child's cold first check; its test still xfails), and AUDIT-34 #3's old-strength test, which xfails as an expression of #2.

## How this was measured

- **Harness.** `scripts/audit_load_10.py` reuses `audit_load_9`'s `stopped_newcomer_wait_s`, `fresh_task_load`, `php` and `UNDECIDABLE`, `audit_load_8`'s `newcomer_beside_fresh_task_runs_s`, and `audit_load_7`'s `burners` and `run_child`.
  - `RunningSampler` reads the state of every live worker from `/proc/<pid>/stat` every 2 ms.
  - `requeue` patches `_Slots.acquire` on SG35 trees to snapshot the queue at each re-queue: the aged callers ahead (rank 0) and the wait.
  - `proto` applies `PROTO_EDITS` to a copy of `src/`.
- **Calibration.**
  - Waits are given in units of one warm hard check measured in the same run, or as shares of the run's own samples.
  - The scheduling repro uses a fake clock and events, and the fork repro a sleep that widens the window.
- **Scratch.**
  - Scratch scripts in a private folder covered the fork race over 255 natural forks, boot waits, `_Slots` cost, and the first prototype comparison. Their numbers are quoted above.
  - No burner processes were started. The prototype and archive trees lived in that folder, which was deleted.

## Verification

- `make lint typecheck`: ruff clean (165 files formatted), mypy "Success: no issues found in 149 source files".
- `uv run pytest tests/audit/test_audit_38.py -q -p no:randomly` gives 3 xfailed in 6.3 s.
- With `--runxfail`, all 3 fail on their assertions.
- With `PYTHONPATH=<proto>/src:.`, #2's test passes (3 of 3).
