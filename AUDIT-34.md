# AUDIT-34: Performance under load, round 9

Auditor: independent. I wrote none of the audited code or the earlier audits. Base: `origin/claude/sg34-together-fork` @ `10ae977` (SG34 on top of round 8).
Machine: 4 CPUs, Linux, Python 3.11.17, z3 4.16.0, no cgroup CPU quota, so the defaults are 2 slots and 2 slow places. Other agents shared the machine: load average was 4.6–8.7 during every measurement below, so both trees were measured back to back in the same conditions.
All runs use Z3 checks, stubs or `FakeLLMClient`. Nothing touches the network. No source file or existing test was changed.

- **Repros:** `tests/audit/test_audit_34.py` has 6 tests. Each is `xfail(strict=True, reason="AUDIT-34 #n")` and asserts the correct behaviour. The two load tests also carry `perf`.
  - With `--runxfail -p no:randomly`, all 6 failed on their assertions in 3 of 3 quiet runs (17.0–17.1 s) and under 4 CPU burners (23.9 s).
  - Without `--runxfail`, all 6 xfail (16.9 s).
- **Measurements:** `uv run python -m scripts.audit_load_9 [--burn N] [section ...]`. The sections are `requeue`, `waste`, `stream`, `decided` and `fork`. SG34's own claims were re-measured with `scripts.audit_load_8`.
- **Version comparison:** the same scripts ran against `git archive dcef66c` (round 8 merged, before SG34), with `PYTHONPATH=<archive>/src`. That tree is called "before" below.

| # | Severity | Title |
|---|---|---|
| 1 | **High** | A newcomer that gives its lent place to a short caller is queued again at the hard rank, at the back. A 150 ms check waited 18.7–20.5 s at the default 5 s timeout (before SG34: 5.4–8.5 s). The stop rule also counts worker start-up, which SG34's own kills make common. |
| 2 | Medium | Lent places now run hard checks beside every slot and slow place. Under a LangGraph-like load, 5.1–5.4 Z3 checks run at once on 4 CPUs (before: 3.8–3.9), about a fifth of Z3 time goes to checks that are killed and run again, and 5–6 workers restart per second. |
| 3 | Medium | SG34's retuned `test_slow_checks_are_bounded_by_slow_workers` cannot fail any more. Its `peak <= 3` and `threads <= 3` hold for any 3 callers. At its old strength it fails (peak 3), which is #2. |
| 4 | Low | A forked child adopts the pool empty and starts nothing, so its first check starts a forkserver and a worker: 0.30–1.01 s against 6–30 ms warm, recorded nowhere. The parent's pre-started pool (6 processes, about 44 MB PSS) is never used in a prefork master. |

In total: one high, two medium and one low finding. None is critical.

---

## 1. High: a newcomer that gives way is queued behind every hard run

**What SG34 does.** A newcomer (a task with no completed check) is lent a place by a stuck slot. Its check is stopped in two cases:
- it has run `_HARD_SLICES` (0.2 s), so it is hard;
- it has run one slice (50 ms) while a short caller waits.

In the second case the check is not counted as hard, and the ADR says it is "queued again for a slot only".

**What actually happens.** `SolverExecutor.run` then sets `short = new = False` and calls `slots.acquire(False, False)`. That gives the caller:
- rank 2, the hard rank, below every newcomer and short caller;
- a new arrival number, so it also loses its place among the hard waiters.

So a check that has shown it is not hard waits behind every queued hard check. That breaks SG32's guarantee (AUDIT-26 #1): a newcomer's first check is served before hard runs. With N hard waiters and 2 slots, the wait is about N/2 hard checks. SG30 aging does not help, because only later arrivals count toward `passed`.

**Second trigger: start-up counts as run time.** `_yields` reads `interrupt.ran_s()`, which runs from `_call`'s start. That includes acquiring a worker and waiting for it to boot (`Worker.ready`), and the target attaches before `ready()`. `_call` subtracts `_worker.overhead_s` only after the call ends. So hardness and `solver_ms` exclude start-up (SG32), but the stop rule does not.

SG34's own kills drain the idle pool: 6.7 worker starts per second under the fresh-task load, against 0 before. Under `audit_load_9 waste`, checks waited on 58 boots in 8 s (p90 35 ms, max 125 ms, two over one slice). In `stream`, one easy (1 ms) newcomer out of 19 was stopped and re-run.

### Measurements (`audit_load_9 requeue`)

Setup: 2 slots and 2 slow places. Hard runs make back-to-back runs-to-timeout checks in long-lived tasks, so they are known hard. Short runs make back-to-back easy checks. The probe is one check in a new task that runs 150 ms (three slices, not hard). Waits are in units of one warm hard check (0.40 s). "Started 2" means the probe's check was stopped and run again.

| Load | HEAD | Before SG34 |
|---|---|---|
| 12 hard, 3 short, 150 ms probe | 3.0 0.8 3.2 3.5 3.6 (started 2 1 2 2 2); another run 3.5 4.0 3.2 | 1.5 1.8 1.3 2.2 0.7; another run 0.7 1.8 1.0 |
| 16 hard, 3 short, 150 ms probe | 5.3 4.1 0.6 4.4 4.6 (started 2 2 1 2 2) | 2.4 2.1 1.5 2.2 1.1 |
| 12 hard, no short runs, 150 ms probe | 0.4 0.5 0.4 0.4 0.4 (never stopped) | 0.5 0.9 1.0 0.7 0.8 |
| 12 hard, 3 short, 10 ms probe | 0.5 0.2 0.3 0.6 0.2 | 0.4 1.8 0.8 0.9 0.7 |
| **Default 5 s timeout**, 16 hard, 4 short, 150 ms probe | **20.5 s, 18.7 s** (stopped), 0.2 s and 0.3 s (not stopped) | 5.4 8.5 8.4 8.5 s |

- **Bimodal.** A probe that is not stopped is served at once, which SG34 improved. A probe that is stopped waits about as long as the whole hard queue: 2.4× the worst wait before SG34.
- **Against the budget.** At the default timeout, 20.5 s for one 150 ms check is 68 % of the 30 s budget for a whole 10-step trace. It grows linearly with the number of hard runs.
- **Who hits it.** Any first check of 50–200 ms in a new task when a short caller waits. In LangGraph every node and retry is a new task. A short caller is any run whose previous check in the same task was easy, for example every step after the first of an easy verify. Checks of that length are ordinary: a real-arithmetic pool, a quantified step, or a 1 ms check that waits for a worker to boot.

### Repros

- `test_a_newcomer_that_gives_way_to_a_short_caller_is_served_before_hard_runs`. It is deterministic (fake clock, events) and takes 0.1 s. Setup: 1 slot, its check stuck, 2 known-hard runs queued. A newcomer is lent the place, runs 2 slices, and gives way to a short caller. The test asserts it is served next. Observed order: `new, short, h1, h2, new`.
- `test_a_newcomer_is_not_stopped_while_its_worker_starts`. The newcomer's worker takes 3 slices to start (`Worker.ready` delayed; the measured worst was 2.5 slices) while a short caller waits. The test asserts the 1 ms check runs once and finishes before the slot frees. Observed: started twice, still waiting at 1 s.

### Root cause

`src/autolab/verification/solver.py`:
- `SolverExecutor.run`: `short = new = False  # stopped: queued again for a slot only`, then a fresh `slots.acquire`, which means a new `_Waiter` with a new `arrival` at rank 2.
- `SolverExecutor._yields`: uses `interrupt.ran_s()`, which includes `_worker.overhead_s` while the call runs.

### Suggested fix

- Re-queue a check stopped for a short caller as a newcomer that may not be lent. That means rank 1 for slots, ideally with its original arrival number. Only a check stopped at `_HARD_SLICES` should drop to rank 2.
- Exclude start-up from `_yields`. One way is to attach the target after `ready()`. Another is to publish the running overhead and subtract it, as `_call` does at the end.
- Reconsider the one-slice rule. Under load, an easy check's own wall here was 33–85 ms (p50 33–49 ms), so one slice does not separate easy from medium.

---

## 2. Medium: lent places run hard checks beside every slot and slow place

**The invariant.** AUDIT-18 #5 sized the pool so that slots and slow checks together do not outnumber the CPUs: 2 + 2 on 4 CPUs, and 1 + 0 on 1 CPU. Before SG34, a lent place went only to a short caller, whose check is brief.

**What SG34 adds.** Now every newcomer's check may run on a lent place for up to `_HARD_SLICES`. Then it is killed, its worker is restarted on the pool thread, and it is run again from scratch. Lent places number up to `workers`, so up to 6 CPU-bound Z3 processes run on 4 CPUs. Under LangGraph's shape (every node a new task), every hard run's first check per node takes that path.

### Measurements (`audit_load_9 waste` and `decided`, 8 s windows)

| Load (2 + 2, fresh task per node) | HEAD | Before SG34 |
|---|---|---|
| 8 runs × 1 check per task, 0.4 s | 35–38 re-runs, **8.5–8.9 s Z3 wasted**, 43–44 restarts, Z3 at once mean 5.07–5.19, peak 6 | 0 re-runs, 0–2 restarts, mean 3.83–3.87, peak 4 |
| 12 runs × 1, 0.4 s | 35–43 re-runs, 9.7–10.3 s wasted, 52 restarts, mean 5.26–5.38 | 0, 0–3, mean 3.84 |
| 12 runs × 3, 0.4 s | 11–13 re-runs, 2.6–3.3 s wasted, 11–16 restarts, mean 4.22–4.26 | 0, 3, mean 3.74–3.83 |
| 12 runs × 1, 1.0 s | 16–18 re-runs, 3.9–4.2 s wasted, 17–20 restarts, mean 4.54–4.58 | 0, 0, mean 3.92 |
| Decidable pigeonhole php(8) (0.28–0.32 s alone), 6–10 runs, 0.45–1.0 s | 29–41 re-runs, 6.7–9.2 s wasted, 31–44 restarts, mean 4.84–5.15 | 0, 0, mean 3.86–3.91 |

- **Waste.** With 1 check per task at 0.4 s, 22–25 % of all Z3 time is spent on checks that are killed and run again. A decidable check that needs a little over 0.2 s, like php(8), is killed at 0.2 s and decided from scratch, once per node.
- **Hard runs' tail latency.** The longest per-check latency rose: 1.40 / 2.56 / 1.91 / 5.02 s against 0.93 / 1.39 / 1.79 / 3.17 s before. The median fell: 0.71 / 0.99 / 1.33 / 2.14 s against 0.85 / 1.29 / 1.37 / 3.10 s.
- **A 1-CPU host** (1 slot, 0 slow). Four hard newcomers ran 2 Z3 checks at once (before: 1), made 6–7 calls (before: 4) and caused 2–3 restarts. The wall was the same, 4.04–4.16× the timeout.
- **Verdicts.** Whether the extra processes flip near-timeout verdicts could not be shown on this shared machine. php(8) at 0.45 s decided 14/74 and 3/70 checks on HEAD, against 7/70 and 6/71 before, which is noise.
- **At the default 5 s timeout** the waste per node is at most 0.2 s plus one restart, about 4 % of a hard check, so the share shrinks. The oversubscription does not: it lasts whenever both slots are stuck.

A stopped check whose result arrives before the kill is also thrown away (`if not stopped or self._closed: return result`), so a check that finished in time is run again.

### Repros

- `test_z3_checks_never_outnumber_the_slots_and_slow_places`. It uses a 1-CPU host's defaults and 3 hard checks in new tasks. It asserts at most 1 Z3 check at a time. Observed 2, with 2 restarts. It takes about 1.5 s.
- `test_runs_that_check_in_fresh_tasks_waste_little_z3_time` (`perf`). It runs 8 runs × 1 check per task for 4 s and asserts at most 5 % of the load's own Z3 time is wasted. Observed 23.6 % quiet and 22.5 % under 4 burners.

### Root cause

- `_Slots.acquire` and `_Slots._wake` call `_grant(short or new)`, so a stuck slot lends to newcomers.
- `SolverExecutor.run` resizes the pool by 1 for a lent place. Nothing counts lent places against `slow_workers` or the CPUs.
- A stop kills the worker (`Workers.discard` → `stop` + `_start`), so the next check boots one.

### Suggested fix

- Lend a newcomer a place only while slots, slow checks and lent places together are fewer than the CPUs. Otherwise rank it 1 for a slot, as SG32 did.
- Or keep a stopped check's worker: move the check to the next free slot or slow place instead of killing it and running it again. Z3 checks are pure, but their CPU is not free.
- Keep a result that arrived before the stop took effect.

---

## 3. Medium: the retuned tests no longer guard what they are named for

| Test | SG34 change | What it guards now |
|---|---|---|
| `test_slow_checks_are_bounded_by_slow_workers` (1 slot, 1 slow, 3 callers) | `peak == 2` → `peak <= 3`; `threads <= 2` → `<= 3`; added `calls in (3, 4)` | **Nothing about the bound.** Three callers can never exceed peak 3 or need more than 3 threads. `calls in (3, 4)` accepts the kill and re-run. It is the only test of the slow cap on hard checks. |
| `test_solver_ms_excludes_queueing` (1 slot, 0 slow, four 0.2 s checks) | wall `>= 4×` → `>= 2×` the timeout | Its `solver_ms` bounds still guard its name. The wall bound now admits two checks at once on a 1-slot executor, which is #2's behaviour. Measured on HEAD, the wall is still 4.04–4.16× with 6 calls and 2 restarts, so no observed run needed the loosening. The 0.2 s check sits exactly on `_HARD_SLICES`, so whether it is stopped is a race. |

- **Repro.** `test_slow_checks_are_bounded_by_slow_workers_as_before_sg34` restores the pre-SG34 assertions (`peak, calls == 2, 3`). Observed `(3, 3)`. It takes about 1 s.
- **Suggested fix.** Assert the bound with more callers than places, or assert that known-hard callers never exceed `workers + slow_workers`. State the newcomer allowance as an explicit number, with a re-run count, instead of a bound that cannot fail.

---

## 4. Low: a forked child starts cold, and the parent's pool idles

**What SG34 fixed.** A child forked after `Verifier()` now checks VALID. AUDIT-30 #2 and AUDIT-29 #1 are fixed.

**What it costs.** `_adopt` leaves the child with no workers and no forkserver, and nothing pre-starts them. A prefork worker's first verify therefore pays a forkserver start, with z3 and the solver preloaded, plus a boot. That wait is not in `solver_ms` (AUDIT-30 #4).

### Measurements (`audit_load_9 fork`, 4 children)

| | Value |
|---|---|
| Child's first check, at once after fork | 475 / 749 / 866 / 677 ms (another run 653 / 1 012 / 975 / 639) |
| Child's first check after idling 1 s | 295–678 ms; later checks 6–75 ms |
| Each child's own forkserver and worker | about 50 MB PSS |
| Parent's pre-started pool, never used by a prefork master | 6 processes, 42–45 MB PSS |
| fds | Child: 14 at fork (the parent's 18, less the 4 idle pipes closed by `_adopt`), 18 after 3 checks. Parent: 18 before and after. No growth. |
| Leftover processes after children exit (`os._exit` or `exit`) | 2–4 zombies at 1 s, none at 5 s (reaped by PID 1) |

### Repro

`test_a_forked_childs_first_check_costs_about_a_warm_one` (`perf`, about 2 s). The child idles 1 s, as a prefork worker does before its first request, then checks. The test asserts the first check costs at most 5× a later one. Observed 295–678 ms against 15–30 ms.

### Suggested fix

In the at-fork child hook, start a prestart thread for each adopted executor, as `SolverExecutor.__init__` does. Record the start-up wait in the verdict (AUDIT-30 #4).

---

## SG34 claims, re-measured (`audit_load_8`, load 5–7)

| Claim | Now | Before SG34, same session | Status |
|---|---|---|---|
| A newcomer beside 8 / 12 / 16 hard runs that started together waits 0.1–0.6 hard checks | 0.3 / 1.9 / 0.1 | (round 8: 1.8 / 2.9 / 3.9) | Mostly holds; one sample of 1.9 |
| A 10-step easy verify beside 16 at 5 s takes 0.7 s (was 20.3 s) | 0.88 s | — | Holds |
| Steady state, 1 check per fresh task, 8 / 12 / 16 runs | 0.4 1.1 0.3 / 0.8 0.8 1.4 / 2.3 2.6 6.4 | 1.2 1.8 1.8 / 2.4 3.0 2.8 / 3.4 3.7 3.7 | Better median; worse tail at 16 runs |
| 3 or 10 checks per fresh task, 12 runs | 0.2–0.4 / 0.1–0.3 | 0.3–1.9 / 0.2–2.6 | Holds |
| SG32 harness | 0.1–0.2 | — | Holds |
| Fork: child checks VALID | VALID in 8 of 8 children | (round 8: raised) | Holds. Cold start is #4 |
| Cost: "a hard run's first check wastes up to `_HARD_SLICES` slices and one restart" | True per check, but under fresh tasks that is 35–43 re-runs and 43–52 restarts per 8 s, a fifth of Z3 time, with 6 Z3 processes on 4 CPUs | — | Understated (#2) |

## Checked and not a finding

**Latest-first lending does not starve early newcomers.** `_LENT_AGING × workers` = 4 lent grants bound the overtaking. In `audit_load_9 stream` (8 hard runs plus a stream of new tasks, half hard and half easy), easy newcomers waited in hard checks:

| Stream rate | HEAD, p50 / p95 / max | Before SG34, p50 / p95 / max |
|---|---|---|
| 5/s | 0.13–0.21 / 0.54–0.68 / 1.02–3.72 | 1.06–1.40 / 1.35–1.71 / 1.57–1.81 |
| 10/s | 0.20–0.27 / 0.49–2.95 / 0.55–7.10 | 3.18–3.60 / 3.56–4.03 / 3.64–4.18 |
| 20/s (above capacity) | 3.12–4.70 / 4.72–5.76 / 6.59–10.65 | 5.32–5.55 / 7.72–8.03 / 8.14–8.39 |

- The medians are far better.
- The worst samples vary between runs. The 3.72 maximum came with an easy newcomer that was stopped and re-run, which is #1, not the lending order.

**Hard runs under a stream of newcomers (`audit_load_8 starve`, same load).**

| Load | Hard checks done, HEAD vs before | Longest wait in units, HEAD vs before |
|---|---|---|
| 4 hard runs, 100 easy/s | 20 vs 22 | 6.3 vs 4.1 |
| 8 hard runs, 100 easy/s | 27 vs 32 | 7.2 vs 5.0 |
| 5 hard/s | 45 / 40 vs 37 / 39 | 1.7 / 4.7 vs 1.9 / 3.5 |

These are within this machine's noise at load 7, but the 100/s tail is worth re-measuring on a quiet machine.

**Fork adoption.**
- No fd growth.
- Children are not left as zombies past PID 1's reaping (none at 5 s).
- The parent's workers are untouched, and its fd count is unchanged.
- The at-fork hooks also run in every worker the forkserver forks, where they find nothing to adopt.

## CLAUDE.md budgets

- **10-step trace in < 30 s.** One medium first check can take 20.5 s at the default timeout beside 16 hard runs and a few easy ones (#1).
- **50 ms p95 per step.** Not affected by SG34. The loop work is unchanged, so AUDIT-30 #3 still applies.

## Bottom line

- **What SG34 fixed.**
  - The round-8 high: truly easy newcomers in runs that start together are now served in a fraction of a hard check.
  - The fork regression: children check VALID.
- **What it costs.**
  - A newcomer whose check takes more than a slice, or whose worker is still booting, is now queued behind every hard run. That is worse than before SG34 in the tail, about 20 s at the default timeout (#1).
  - Lending to newcomers breaks AUDIT-18 #5's CPU bound and spends about a fifth of Z3 time on kills and re-runs under LangGraph's task shape (#2).
  - The retuned test that guarded the slow cap now cannot fail (#3).

## How this was measured

- **Harness.** `scripts/audit_load_9.py`, which reuses `audit_load_7`'s `run_child`, `burners` and `hard_check_s`, and `audit_load_2`'s `slow_check`.
  - `metered()` patches `Worker.call` to count Z3 checks in workers and their time-weighted concurrency, and patches `Workers.discard` to count restarts.
  - `fresh_task_load` times every call of the check function, so a run whose function was called twice was stopped and re-run. Its earlier calls are counted as wasted.
- **Version comparison.** `git archive dcef66c` was extracted into a private scratch folder and run with `PYTHONPATH=<archive>/src` from the same venv, back to back with HEAD.
- **Calibration.**
  - The load tests compare against the machine's own measurements: one warm hard check, the load's own Z3 seconds, and the child's own warm checks.
  - The scheduling repros use a fake clock and events. One of them delays `Worker.ready` by 3 slices to stand in for a slow boot.
- **Scratch.**
  - Scratch scripts measured worker boot waits inside checks, the 1-slot cases and the fresh-load probe re-runs. Their numbers are quoted above.
  - Burners were killed in a `finally` block, and `pgrep` showed none left.
  - The scratch folder was deleted.

## Verification

- `make lint typecheck`: ruff clean (160 files formatted), mypy "no issues found in 145 source files". `mypy scripts/audit_load_9.py` is clean.
- `uv run pytest tests/audit/test_audit_34.py -q -p no:randomly` gives 6 xfailed in 16.9 s.
- With `--runxfail`, 6 failed on their assertions in 3 of 3 runs, and under 4 CPU burners.
