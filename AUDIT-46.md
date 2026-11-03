# AUDIT-46: Performance under load, round 12 (SG37)

Auditor: independent. I wrote none of the audited code or the earlier audits. Base: `origin/claude/sg37-bound-pauses` @ `2a6ee8f` (SG37).
Machine: 4 CPUs, Linux, Python 3.11, no cgroup quota, so the defaults are 2 slots and 2 slow places. Load average was 0.4–3.9 during the measurements, with other agents on the machine, so I measured compared trees back to back.
Every run uses Z3 checks or stubs. Nothing touches the network. I changed no source file and no existing test.

- **Repro:** `tests/audit/test_audit_46.py` has 1 test, `xfail(strict=True, reason="AUDIT-46 #1")` plus `perf`. It asserts the correct behaviour.
- **Versions:** "SG36" means `git archive 7a3debe` (the tree SG37 branched from). "pre-SG36" means `git archive 9792264`. Both were run with `PYTHONPATH=<archive>/src:.` from the same venv.

| # | Severity | Title |
|---|---|---|
| 1 | Medium | A check on a lent place is frozen for its lender's whole run-on, up to one full timeout. An easy check waited 0.89–0.96 s beside 1.0 s checks (2.51 s beside 3.0 s ones; 2–3 ms alone; SG36 0.03 s). AUDIT-38 #1's 150 ms newcomer waited 1.77–3.44 s in 2 of 8 probes at 5 s (SG36: all ≤ 0.19 s). AUDIT-30 #1 at 16 fresh-task runs had 4 and 6 of 16 probes over 1.5 hard checks (SG36: 0 of 16, max 0.8). |
| 2 | Low | Past the paused cap, bursts re-run work. 64 fresh tasks make 107–120 calls (43–54 stops), and the burst takes 19.0–19.4 s against 16.2 s before SG36 (+18 %). |
| 3 | Low | Beside streaming short callers, hard bursts take about 2× the place bound: 16 tasks in 8.7 s (4.2 s pre-SG36), 64 in 33.6–34.5 s (16.5 s). The CPUs stay busy (3.45 of 4 Z3 processes running on average), and the time goes to easy checks (5.6–10 k against 2.3 k). |

None is critical or high: 0 critical, 0 high, 1 medium, 2 low.

## What SG37's claims measured (re-measured)

| Claim | Claimed | Measured here |
|---|---|---|
| AUDIT-42 #1: lender wall beside short loops (`audit_load_11 lender`, 1.0 s checks) | ~2.5 timeouts | 1 slot with 1/2/4 loops: 2.49, 2.50, 2.50 s. 2+2 with 4 hard and 2/4/8 loops: 2.54–2.56 s for the lenders, 1.00–1.01 s for the slow ones. All done. |
| AUDIT-42 #2: bursts of 16/64/128 fresh tasks (`audit_load_11 held`) | peak 8 processes, 270–307 MB | Peak 8 worker processes (at most 4 stopped), 10–11 threads, 242–308 MB PSS in every run. SG36 in the same session: 18 and 50 processes, 479 MB and 1.27 GB. |
| SG36's CPU bound (`audit_load_10 bound`; probe) | mean ≤ places | Mean Z3 processes running: 3.77–3.83 (8/12 runs) and 3.02–3.31 (newcomer probes). More than 4 in ≤ 1 % of samples. 64-task burst: 3.91. AUDIT-38 #2's /proc test passes. |
| AUDIT-30 #1 newcomer wait (`audit_load_8 fresh`, `together`) | 0.0–1.5 hard checks | 8 and 12 runs: 0.0–0.3. 16 runs: see #1. |
| Lender ending while lent, overlapping pauses | no leak, no negative `solver_ms` | 4 rounds of 40 mixed 0.3/0.6/1.0 s checks beside 2 short loops, 5 cancelled mid-flight. After each round: slots, lenders, lent checks, holders, handed, paused, slow and running all 0. Threads, workers (6 = `_IDLE_WORKERS`), fds and stopped processes stay flat. 0 negative `solver_ms`, and 0 over 1.6× the timeout. |
| One watchdog thread per worker process | | `test_checks_share_one_watchdog_thread` passes. |

---

## 1. Medium: a lent check is frozen for its lender's whole run-on

**Mechanism.**
- `_Slots._balance` orders checks as non-due lenders first, then `_lent_checks`, then due lenders, and freezes the first `lent` of them.
- Once a lender has been frozen for `_FROZEN_SHARE × budget_s`, the check on its lent place is frozen (SIGSTOP) until the lender ends. That is the lender's whole remaining budget, almost one timeout if it was stuck early.
- Every short caller queued behind that place waits as well. The place stays lent, `lent < stuck` fails, and with no slow place no other CPU is free.
- A newcomer that gives way from a frozen lent place hands it to a waiting short caller. `_balance` then freezes that caller's check at once.
- PLAN's open item says "a short caller's lent check may stay frozen for up to a lender's remaining share". These measurements put a size on it. At the default `SOLVER_TIMEOUT_S` (5 s), an easy step can stall about 5 s, roughly 2 000× its wall.

**Measurements.** Probes are scratch scripts that reuse `audit_load_11` helpers.
- **Easy latency, 8 s windows: a hard loop beside short loops.**
  - 1+0 at 1.0 s: max 0.89 / 0.96 s (1 / 2 short loops), 3–6 waits over 0.5 s.
  - 1+0 at 3.0 s: max 2.51 s.
  - 2+0 (a 2-CPU host's defaults), 2 hard loops at 1.0 s: max 0.95 s, 6 waits over 0.5 s.
  - 2+2, 4 hard loops at 1.0 s: max 0.14–0.44 s. Slow places absorb most of it there.
  - SG36, same loads: max 0.03 s.
  - Easy alone: 2.3–3.2 ms.
- **AUDIT-38 #1 (`stopped_newcomer_wait_s(16, 4, 5.0, 0.15, …)`), 150 ms newcomer.**
  - HEAD: 3.38 s (1 of 6), then 3.44 and 1.77 s (2 of 8), then 3.40 and 2.01 s (2 of 8). The rest were 0.17–0.19 s.
  - SG36: 8 of 8 at 0.17–0.19 s.
- **AUDIT-30 #1 (`newcomer_beside_fresh_task_runs_s(16, 0.4)`), 4 × 4 probes, waits in hard checks.**
  - HEAD: max 2.1 and 2.0; 4 and 6 of 16 probes over 1.5.
  - SG36: max 0.8; 0 of 16 over 1.5.
  - At 8 and 12 runs both trees stay at 0.0–0.3.

**Repro.** `test_an_easy_check_beside_a_due_lender_is_not_frozen_for_its_run_on` (`perf`, about 5 s).
- Setup: 1 slot, no slow place. A loop of 0.5 s runs-to-timeout checks runs beside 1 short loop for 6 hard walls.
- It asserts that no easy check waits more than 0.25 × one hard check's wall alone.
- With `--runxfail`, 3 of 3 runs failed: 0.42, 0.43 s, and 0.44 s under 4 CPU burners (0.84–0.87 hard checks).
- It passes on SG36.

**Suggested fix.** Once a lender is due, alternate it and the lent check slice by slice, instead of freezing the lent check for the rest of the lender's run.
- The lender's wall stays bounded: about 1.5 T plus 2× its remaining time.
- A lent check then waits at most one slice. The cost is longer walls for lent newcomers in that phase only.
- Or, once a lender is due, stop granting that place to newcomers and let short checks through between slices.

## 2. Low: re-runs past the paused cap

`probe_burst`: 2+2 slots, N fresh tasks, one runs-to-timeout check each at 1.0 s; stops counted at `SolverExecutor._pause` returning False.
- 16 tasks: 8 stops, 24 calls. 64 tasks: 43–54 stops, 107–120 calls.
- Each stopped call has run `_HARD_SLICES` (0.2 s) and is killed and run again. With no short loops, 64 tasks take 19.0–19.4 s against 16.2 s pre-SG36 (+18 %).
- 43 stops × 0.2 s is about 8.6 CPU-s, or about 2.2 s of wall on 4 places, which is most of the gap.
- This is the cost SG37 accepted in exchange for bounding processes (ADR 0009). It is reported as a number, so there is no xfail.
- A possible fix: cap lent places to newcomers at `workers` while `_paused` is full, so a newcomer past the cap queues for a slot without running first.

## 3. Low: hard throughput beside short streams

- 16 fresh 1.0 s tasks beside 2 short loops: 8.7 s (place bound 4.0 s; pre-SG36 4.2 s; SG36 94.8 s).
- 64 tasks: 33.6–34.5 s (16.0; pre-SG36 16.5; SG36 184.0).
- While a lent place is busy, each lender gets the CPU about 1.0 T in every 2.5 T, and short loops keep the lent places busy.
- That is policy, not idle waste: 3.45 Z3 processes run on average, and easy checks run 5 626–10 002 against 2 262.
- The longest wait in the burst was 34.5 s for a 1.0 s check. Infinite short streams are a worst case, so there is no xfail.

## Starvation, waste and leaks: nothing else found

- No caller went unserved in any run, and every burst completed.
- No negative or inflated `solver_ms`.
- No leaked threads, workers, fds, stopped processes or slot counters (see the table above).
- With SIGSTOP available, no newcomer was paused beyond `workers`.

## How this was measured

- **Harnesses:**
  - `uv run python -m scripts.audit_load_11 lender held`
  - `uv run python -m scripts.audit_load_10 bound requeue`
  - `uv run python -m scripts.audit_load_8 fresh together`
- **Scratch probes:** probe_burst, probe_easy, probe_fresh and probe_leak. These are short scripts that reuse `ShortStream`, `HeldSampler`, `_state`, `newcomer_beside_fresh_task_runs_s` and `stopped_newcomer_wait_s`, plus monkeypatched counters on `SolverExecutor._pause` and `_call`. They were deleted afterwards.
- Each comparison ran HEAD and SG36 (or pre-SG36) back to back.
- **CPU burners:** 4 × `python -c "while True: pass"`, killed afterwards.
- **Repro command:** `uv run pytest -p no:randomly --runxfail tests/audit/test_audit_46.py` (fails as expected); without `--runxfail` it reports 1 xfailed.

## Bottom line

SG37 delivers both highs of round 11:
- Lenders end in 2.5 timeouts beside any number of short loops.
- Bursts of up to 128 tasks hold 8 worker processes and ≤ 308 MB.
- The CPU bound (mean ≤ 3.9 of 4) holds, and nothing leaks.

The price is #1. A lender's run-on freezes the check on its lent place for up to a full timeout. That brings back multi-second waits for easy work on hosts with no slow place, and in AUDIT-38 #1 and AUDIT-30 #1 at 16 runs. It is bounded, but larger than PLAN's open item suggests.
