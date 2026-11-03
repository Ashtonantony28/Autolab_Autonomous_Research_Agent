# AUDIT-42: Performance under load, round 11

Auditor: independent. I wrote none of the audited code or the earlier audits. Base: `origin/claude/sg36-pause-lender` @ `3d2d7b4` (SG36 on top of round 10).
Machine: 4 CPUs, Linux, Python 3.11, z3 4.16.0, no cgroup CPU quota, so the defaults are 2 slots and 2 slow places. Other agents shared the machine: load average 0.5 at the start and 4–8 for most measurements, so compared trees were always measured back to back.
All runs use Z3 checks or stubs. Nothing touches the network. No source file or existing test was changed.

- **Repros:** `tests/audit/test_audit_42.py` has 4 tests, each `xfail(strict=True, reason="AUDIT-42 #n")` and asserting the correct behaviour; the two timing tests also carry `perf`. With `--runxfail -p no:randomly` all 4 fail on their assertions (2 of 2 runs, 6.5 s). Without it, 4 xfailed (6.4 s). On the pre-SG36 tree #1, #2 and #4 pass and #3 fails, so #1, #2 and #4 are SG36's.
- **Measurements:** `uv run python -m scripts.audit_load_11 [--burn N] [section ...]`, sections `lender`, `giveway`, `held`, `watchdog`, `vocab`, `drift`. AUDIT-34 #2 and AUDIT-38 #1 were re-measured with `scripts.audit_load_10 bound requeue`.
- **Versions:** "pre-SG36" is `git archive 9792264` (the merge SG36 branched from), run with `PYTHONPATH=<archive>/src:.` from the same venv.

| # | Severity | Title |
|---|---|---|
| 1 | High | A lending check stays frozen as long as short callers keep its lent place busy. Its budget excludes the pause, so its wall time is unbounded: a 1.0 s check was not done after 10 s beside one short loop (1.00 s before SG36), and 16 fresh hard checks beside 2 short loops took 59.4 s (4.2 s before). |
| 2 | High | A newcomer paused after it shows it is hard keeps its worker process and pool thread, so a burst of new runs holds one process each: 64 fresh tasks peaked at 48–50 worker processes and 1.2–1.3 GB of worker PSS, and 128 at 96–100 processes and 2.3–2.5 GB, against 6 processes and 0.2 GB before SG36. |
| 3 | Medium | A newcomer that gave way gives way again to every short caller that queues, even after running no time at all. Beside a stream of short callers a 150 ms check waits 2.3–7.5 s and gives way 110–400 times (1 slot, 2 short loops). SG36 claims 0.17 s. |
| 4 | Medium | The watchdog starts and joins one thread per check. That adds 0.7–1.1 ms to an easy check when idle and 5.6–9.6 ms under 4 CPU burners (median 14–20 ms against 9–12 ms without the thread). This is the likely cause of `test_hard_runs_do_not_starve_an_easy_verify_on_one_worker`'s 0.507 s miss. |
| 5 | Low | `test_audit_30`'s vocabulary-merge xfail XPASSes under CPU burners because its "alone" sample is inflated by preemption. Not SG36's: same spread on both trees, and no code on its path changed. |

In total: two high findings, two medium and one low. None is critical.

**What SG36 delivers.** The bound now holds in steady state: 2.98–3.81 Z3 processes run on average, over 4 places in 1 % of samples (AUDIT-34 #2 / AUDIT-38 #2). The stopped newcomer of AUDIT-38 #1 is served in 0.16 s at 5 s (6 of 6). No check is re-run, and the pause-aware clock does not drift. **What it costs** is #1 and #2: pausing moves the oversubscription into wall time and memory, which nothing bounds.

---

## 1. High: a frozen lender is never thawed while short callers stream

**Mechanism.**
- `_Slots._balance` freezes `lenders[0 : lent]` after every grant and release, the longest stuck first.
- A short caller's release (`lent -= 1`) and the next short caller's grant (`lent += 1`) happen inside the same `_wake` call. `_balance` runs only after both, so the lender never sees `lent == 0` while any short caller waits.
- The lender's budget excludes paused time on both sides: `Worker._poll` in the parent, and `_Watchdog` / `_now` in the worker. Nothing else bounds the call: no `wait_for` exists in `src/`, and `Verifier.verify` waits on it.
- So the check stays frozen, and keeps its slot, for as long as the stream lasts. Pre-SG36 it ran beside the lent check (oversubscribed) and ended at its wall-clock timeout.

**Knock-on effects.**
- The frozen check keeps its slot. Every caller queued for a slot, including SG30's aged hard callers, waits behind it, so the whole hard queue stops, not just one check.
- The trace-level budget (CLAUDE.md: a 10-step trace in < 30 s) and SG2's "times out to UNGROUNDED inside `solver_timeout_s + 1 s`" no longer hold whenever easy traffic is present.

### Measurements (`audit_load_11 lender`, `held`)

Hard checks are runs-to-timeout checks, each in a new task, made once the slots are full and stuck. Short loops make back-to-back easy checks. The cap is 10 s.

| Load | HEAD | pre-SG36, same session |
|---|---|---|
| 1 slot, 1 hard at 1.0 s, 0 short loops | 1.00 s | 1.00 s |
| 1 slot, 1 hard at 1.0 s, 1 / 2 / 4 short loops | **not done in 10 s** (2 of 2 runs at each loop count) | 1.00 / 1.00 / 1.01 s |
| 2+2, 4 hard at 1.0 s, 2 short loops | 1.02 1.02 8.15 s, 1 not done (another run: 5.93, 8.40 s) | 1.01–1.07 s |
| 2+2, 4 hard at 1.0 s, 4 / 8 short loops | 2 of 4 not done in 10 s (2 of 2 runs) | 1.01–1.06 s |
| 2+2, 16 fresh hard at 1.0 s, 2 short loops, all done in | **28.6 s, 59.4 s** | 4.2–4.3 s |
| 2+2, 64 fresh hard at 1.0 s, 2 short loops, all done in | 20.1 s, **80.9 s** | 16.7–17.0 s |

The easy throughput gained is small. A single slot served 1 421–1 449 easy checks in 10 s while its hard check was frozen, about 142/s, against 144–191/s on pre-SG36 while that check still ran.

### Repro

`test_a_lending_checks_wall_stays_bounded_while_short_callers_stream` (`perf`, about 3.5 s).
- Setup: 1 slot, no slow place. A 0.5 s runs-to-timeout check is timed alone, then made again and left to become stuck, and then 2 short loops start.
- It asserts that the check ends within 4× its own wall alone. Observed: still running after 2.01–2.02 s (alone 0.50 s), 2 of 2 runs; 3.21 s (alone 0.80 s) in an earlier version that did not warm a worker first.
- It passes on pre-SG36.

### Suggested fix

Bound the lender's frozen share, not only the number of running checks.
- **Time-share.** While a place is lent, freeze the lent check and the lender alternately, one slice each. The lender's wall stays ≤ 2× its budget, and the bound still holds.
- **Or a ceiling.** Thaw a lender once it has been frozen for its own remaining budget, and stop lending its place until it ends. The bound is broken only briefly.

Either way, the budget should then exclude only the frozen time the scheduler accounts for, so wall time is bounded by a multiple of the budget.

---

## 2. High: every paused newcomer keeps a worker process

**Mechanism.**
- In a burst of new tasks, each newcomer is lent a place (latest first) and runs `_HARD_SLICES`.
- It is then frozen with its worker, not stopped (SG36), and queued again as hard.
- The next newcomer gets the lent place. `Workers.acquire` finds no idle worker, because the frozen ones are attached, and starts a new process. `SolverExecutor.run` also keeps a `_pool.resize(1)` thread for each lent check while it lasts.
- So the number of worker processes and threads grows with the burst, not with the places.
- Pre-SG36, the newcomer's worker was killed and replaced. That wasted CPU, but kept at most `_IDLE_WORKERS` processes.

### Measurements (`audit_load_11 held`; 2 slots + 2 slow places; N new tasks, one runs-to-timeout check each at 1.0 s; peaks sampled every 20 ms from `Workers._live` and `/proc`)

| N | HEAD: worker processes (stopped), threads, worker PSS | pre-SG36: same |
|---|---|---|
| 16 | 14 (10), 16–17, 380–425 MB | 6 (0), 8–9, 181–207 MB |
| 64 | 48–50 (44–46), 51–52, 1 194–1 298 MB | 6 (0), 9–10, 191–215 MB |
| 128 | 96–100 (92–96), 98–103, 2 277–2 516 MB | 6 (0), 9, 217 MB |

- **Memory.** About 25 MB of PSS per paused worker, linear in N. On this 16 GB machine about 600 new tasks at once would exhaust memory.
- **CPU bound.** Each paused newcomer needs a new process, so boots scale with the burst. Running processes peaked at 8–11 during the 16- and 64-task bursts with short loops (6 on pre-SG36, whose oversubscription is AUDIT-34 #2).
- **Wall time.** Without short callers the burst's total stays near ideal: 17.5 s against 16.0 s for 64 tasks, and 35.5 s against 32.0 s for 128. With short callers it is #1's numbers.

### Repro

`test_a_burst_of_new_runs_holds_worker_processes_bounded_by_the_places` (about 2 s).
- Setup: 2+2, 32 new tasks, each making one runs-to-timeout check at 1.0 s.
- It asserts that live worker processes never exceed `_IDLE_WORKERS` plus the places (10 here). It stops as soon as they do. Observed: 11, the first sample over the bound.
- It passes on pre-SG36, whose peak is 6.

### Suggested fix

Cap the number of paused newcomers, for example at `workers`, or at the idle workers available. Past the cap, either stop the newcomer and discard its worker as SG35 did, or do not lend it a place when no idle worker exists. A newcomer that shows it is hard gains little from keeping its state: it then waits as hard, typically for a whole hard check.

---

## 3. Medium: a newcomer that gave way gives way again to every short caller

**Mechanism.**
- In `SolverExecutor.run`, once `lent and gave_way`, the loop skips its slice wait whenever `slots.short_waiting()`.
- `_yields(..., gave_way=True)` then gives way at once, whatever the check has run since it was thawed.
- Lent places rank short callers before newcomers. A newcomer that gave way gets one again only after `_LENT_AGING * workers` grants past it, and its `lent_past` restarts at 0 on each re-queue.
- So beside a stream of short callers the newcomer runs only in the gaps between their checks. Each gap costs a SIGSTOP, a SIGCONT and two event-loop wake-ups.
- With one slot, #1 also keeps the slot from freeing, so the newcomer never gets a slot instead.

### Measurements (`audit_load_11 giveway`; probe = a new task's one 150 ms check, 0.15–0.16 s alone; hard runs at 5 s; give-ways counted at `SolverExecutor._again`)

| Load | HEAD: wait (give-ways) | pre-SG36, same session |
|---|---|---|
| 1 slot, 1 hard, 1 short loop | 1.05–3.80 s (115–535) | 4.56–5.16 s (stopped, re-run) |
| 1 slot, 1 hard, 2 short loops | 2.29–7.45 s (110–397) | 4.56–5.18 s |
| 1 slot, 1 hard, 4 short loops | 2.30–6.10 s (216–400), once > 8 s | 4.55–5.19 s |
| 2+2, 4 hard, 2 short loops | 0.65–2.97 s (40–282) | 0.17–5.02 s |
| 2+2, 16 hard, 4 / 8 short loops | 0.19–7.48 s (0–3 148) | 0.18 s to > 8 s, > 8 s in 5 of 12 |

- **Against the claim.** HEAD is better than pre-SG36 on average, but the claimed 0.17–0.18 s holds only where short callers rarely queue: `audit_load_10 requeue` at 5 s gave 0.16 s, 6 of 6.
- **Early probes.** `requeue` at 0.4 s and 1.0 s gave a first probe of 1.39 s and 2.53 s (3.4 and 2.5 hard checks), then 0.21–0.23 s.

### Repro

`test_a_newcomer_that_gave_way_is_served_beside_a_stream_of_short_callers` (`perf`, about 2 s).
- Setup: 1 slot, its check stuck at 5 s, and 2 short loops.
- It asserts that a new task's 150 ms check ends within 4× its wall alone. Observed: still waiting at 0.61–0.62 s (2 of 2 runs).
- It fails on pre-SG36 too (AUDIT-38 #1's slot wait), so it is not a regression.

### Suggested fix

Give way again only after running a slice since the last thaw, that is, use `ran_s()` since `thaw`, not `gave_way`. Or hand a released lent place straight back to the newcomer that gave way for it (AUDIT-38 #1's second suggestion).

---

## 4. Medium: one watchdog thread per check

**Mechanism.** `_solve` wraps every check in `_Watchdog`, which builds, starts and joins a `threading.Thread`.
- `Thread.start()` blocks until the new thread has run.
- `__exit__` sets an event and joins.
- Each is a hand-off between two threads, and under CPU contention each waits for the scheduler.

### Measurements (`audit_load_11 [--burn 4] watchdog`; `_solve` against the same work without `_Watchdog`, interleaved 400 times in one process, so load falls on both alike)

| | with watchdog, median | without, median | mean extra per check |
|---|---|---|---|
| idle (5 runs) | 4.65–5.85 ms | 4.03–5.15 ms | 0.68–1.08 ms |
| 4 CPU burners (3 + 3 runs) | 14.2–19.6 ms | 8.6–12.0 ms | 5.6–9.6 ms |

- **Against the ADR.** The amendment quotes +0.27 ms on `bench_verifier`'s step p50, an idle figure. Under contention the extra is 60–70 % of an easy check.
- **The perf miss.** `test_hard_runs_do_not_starve_an_easy_verify_on_one_worker` asserts an absolute 0.5 s for a 10-step easy verify beside 2 hard runs. Under 4 burners (plus other agents) it measured:
  - HEAD: 0.23–2.15 s, median about 0.77 s (16 runs).
  - pre-SG36: 0.19–0.79 s, median about 0.37 s (16 runs, alternated).
  - The verify alone took 0.25–2.26 s on HEAD and 0.11–0.80 s on pre-SG36.
- **Attribution.** The test pre-dates SG36 and uses an absolute bound, so it is load-sensitive on any tree. The added per-check hand-offs explain why a near miss (0.507 s) became likely: a 10-step verify makes 10 checks, so about +56–96 ms under burners. Nothing else in the slot path the test exercises changed: with 1 slot and 2 slow places, its hard runs go slow and nothing is lent.

### Repro

`test_checks_share_one_watchdog_thread` (deterministic, about 0.2 s).
- It counts `z3-watchdog` threads started while `_solve` decides 20 easy checks, and asserts at most 1.
- Observed: 20. It passes on pre-SG36, which has none.

### Suggested fix

Start one watchdog thread per worker in `_serve` and arm it per check (a deadline under a `Condition`). Or keep Z3's own `timeout` for checks that are never paused, and arm the watchdog only once the parent first pauses the worker.

---

## 5. Low: AUDIT-30 #3's strict xfail XPASSes under burners (not SG36's)

**The test.** `test_merging_a_reply_does_not_scale_with_the_vocabulary_it_joins` is `xfail(strict=True)`. It fails as expected when `loaded > 2 × alone`, so a "perf miss" there is an XPASS. Its `alone` is the best of 3 merges of about 35 ms.

**Under 4 burners (`audit_load_11 --burn 4 vocab`, 10 pairs each, back to back).**
- `alone` was inflated by preemption on some samples, up to 227 ms against about 35 ms idle.
- The ratio spread was 3.0–16.1 on HEAD and 2.5–20.8 on pre-SG36. Values down to 2.5 make an XPASS likely in one run of five.
- Idle, the ratio was 3.9–6.9 on both trees.

**Attribution.** No file on its path (`vocabulary`, `formalizer`, `vericot`, `audit_load_8`) changed in SG36, and no Z3 worker is involved.

**Suggested fix.** Take `alone` as the minimum over more samples, interleaved with `loaded`, or compare against a unit of work measured in the same loop. No repro test: this is the test's calibration, not the product's behaviour.

---

## Questions asked of SG36, and answers

**Can a newcomer that gives way be starved by a stream of short callers?**
- Yes in effect: #3. It is not starved forever, because it accumulates run time in the gaps between short checks, but it gives way hundreds to thousands of times.
- With a slot whose lender is frozen (#1), no slot frees to rescue it.

**Can a check stay paused indefinitely, so its wall time is unbounded?**
- Yes: #1.
- Cancelling still ends it: `request()` resumes, then kills.
- `shutdown()` and process exit resume and kill too (`_close_all` resumes every live worker first).

**Paused checks holding a worker process and a pool thread.**
- Unbounded in a burst: #2.
- The threads are cheap (about 1 per paused check, 98–103 at N=128) but grow without limit too.

**Watchdog thread count, CPU and timer drift.**
- At most one watchdog per worker at a time, always joined, so it leaks nothing.
- Past the deadline it interrupts every 10 ms until `_solve` returns, which is negligible.
- **No drift** (`audit_load_11 drift`, pausing through `Worker.pause`/`resume`). A 1.0 s check:
  - paused 0.5 s once: returns at 1.505 s;
  - paused 50 × 10 ms: returns at 1.519 s, 0.996 s unpaused;
  - paused 400 × 2 ms: returns at 1.869 s, 0.970 s unpaused.
  - Each was ended by the watchdog ("solver returned unknown"), never killed.
- **Cost:** #4.

**AUDIT-34 #2 / AUDIT-38 #2 re-measured (`audit_load_10 bound`).**

| Load | Running Z3 processes: mean, peak, share of samples over 4 |
|---|---|
| 8 runs | 2.98, 5, 1 % |
| 12 runs | 3.81, 8, 1 % |
| easy newcomer beside 8 / 12 / 16 fresh-task runs | 2.93–3.48, 5–8, 1 % |

- No re-runs, no wasted Z3 time, and 0–13 restarts.
- This matches the claimed 3.0–3.8. The peaks are booting workers, which #2 multiplies in a burst (8–11).
- The easy newcomer waited 0.1–0.7 hard checks, once 9.9 (beside 16).

**AUDIT-38 #1 re-measured (`audit_load_10 requeue`).** 0.16 s at 5 s (6 of 6, 1 call each). At 0.4 s and 1.0 s the first probe took 1.39 s and 2.53 s, then 0.21–0.23 s. The claim holds at the default timeout when short callers rarely queue; see #3 for when they do.

**The two `make test` misses under 4 burners.**
- `test_hard_runs_do_not_starve_an_easy_verify_on_one_worker` (0.507 s against 0.5 s): an absolute bound that SG36 made easier to miss through #4.
- `test_audit_30`'s ratio: an XPASS of a strict xfail, not SG36's (#5).

## CLAUDE.md budgets

- **10-step trace in < 30 s.** A run with an undecidable step cannot meet it while easy traffic is present (#1): its check is frozen for as long as the traffic lasts.
- **50 ms p95 per step.** Idle, the watchdog adds about 0.7 ms per check. Under contention it adds 5.6–9.6 ms (#4).

## Bottom line

- **Delivered.** SG36 delivers what it measured in steady state: the CPU bound (mean 3.0–3.8 running, 1 % over 4), no re-run waste, a pause-aware clock that does not drift, and AUDIT-38 #1's 0.16 s at the default timeout.
- **Two unbounded resources.** The pause trades CPU oversubscription for two resources nothing bounds:
  - wall time of a frozen lender, unbounded under any stream of short callers (#1, high);
  - worker processes and memory held by paused newcomers, linear in burst size (#2, high, 2.3–2.5 GB at 128 tasks).
- **Not fully fixed.** A newcomer that gave way still waits seconds beside a short stream (#3).
- **New cost under load.** The per-check watchdog thread is SG36's main per-check cost under load (#4), and the likely cause of one of the loaded `make test` misses. The other miss is not SG36's (#5).

## How this was measured

- **Harness.** `scripts/audit_load_11.py` reuses `audit_load_9`'s `UNDECIDABLE`, `audit_load_7`'s `EASY` and `burners`, and `audit_load_8`'s `merge_into_vocab_s`.
  - `ShortStream` runs back-to-back easy checks in their own tasks.
  - `GiveWays` counts SG36 re-queues by wrapping `SolverExecutor._again`.
  - `HeldSampler` reads `Workers._live`, `/proc/<pid>/stat` states, `smaps_rollup` PSS and `threading.active_count()`.
  - `watchdog_cost` interleaves `_solve` with the same work minus `_Watchdog`.
  - `paused_through_worker` pauses via `Worker.pause`/`resume`, so the shared paused total is written as SG36 writes it. `audit_load_10 pause` sends raw signals, so its "1.00 s" does not exercise SG36's clock.
- **Calibration.**
  - Timing repros compare against the same check's wall alone on the same executor.
  - #2 compares against the pool's own `_IDLE_WORKERS` plus places.
  - #4 counts threads.
  - Version comparisons were run back to back with the pre-SG36 archive.
- **Scratch.** A private scratch folder held the pre-SG36 archive, the first versions of these harnesses and a burner wrapper. All burners were killed by the wrapper's exit trap; no burner or Z3 worker process was left running (checked with `pgrep`). The folder was deleted.

## Verification

- `make lint typecheck`: ruff "All checks passed!", 171 files formatted; mypy "Success: no issues found in 154 source files".
- `uv run pytest tests/audit/test_audit_42.py -q` gives 4 xfailed in 6.4 s.
- With `--runxfail -p no:randomly`, all 4 fail on their assertions (2 of 2 runs).
- With `PYTHONPATH=<pre-SG36>/src`, #1, #2 and #4 pass and #3 fails.
