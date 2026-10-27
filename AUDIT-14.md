# AUDIT-14: Performance under load, round 4

Auditor: independent (wrote none of the audited code or the earlier audits). Base: `origin/claude/sg19-solver-pool` @ `fee50eb` (SG15–SG19 merged).
Machine: 4 CPUs, Linux, Python 3.11, z3 4.16.0. Default solver workers = `min(2, cpu)` = 2.
All runs use `FakeLLMClient`, subclasses of it, or stubs. Nothing touches the network and no real LLM is used.

**Load caveat.** Other agents' suites ran on this machine for most of the audit (load average 3–6 on 4 CPUs; the container also restarted once). Where a number moved between runs, the range is given. Each repro compares against the check's own timeout or uses a wide margin, so it does not depend on a quiet host.

- Repros: `tests/audit/test_audit_14.py`. Each finding has tests marked `xfail(strict=True, reason="AUDIT-14 #n")` and `perf` that assert the correct behaviour. All three fail on their assertion under `--runxfail` (each takes about 5 s; the file takes about 17 s).
- Measurements: `uv run python -m scripts.audit_load_4 [section ...]`. Sections: `assume`, `overrun`, `starve`, `cpus`, `pool`, `interrupt`, `shutdown`, `reset`, `probe`, `second`, `loop`, `memory`, `bench`. Earlier rounds were re-run with `scripts.audit_load pool cancel latency long concurrency --default-timeout`, `scripts.audit_load_2 segv races` and `scripts.audit_load_3 threads`.
- `make lint typecheck test`: lint and mypy clean; 913 passed, 12 skipped, 29 xfailed (3 new). One earlier full run failed only on the flaky AUDIT-6 #7 repro (see the re-check table).
- `scripts.audit_load_4.add_based_checks()` swaps in the pre-SG19 second check (assert the negated claim, then `check()`) inside the measuring process only, so the two paths can be compared on the same input. No production code was changed.

| # | Severity | Title |
|---|---|---|
| 1 | **High** | SG19's assumption-based entailment check runs past its timeout and ignores interrupts on large linear premise pools (up to 10×), and is 2.8–8× slower than the check it replaced |
| 2 | **High** | Two hard runs starve every other run of the 2-worker pool: a 10-step easy verify takes 30–75 s (budget 30 s) |

No critical findings. Two high findings, no medium or low ones.

---

## 1. High: the assumption-based entailment check outlives its timeout and its cancel

**Impact.** SG19 asserts the whole document with one `Solver.from_string` call and then checks the negated claim as an assumption, `solver.check(claim!)`. On linear integer premise pools of a few hundred to a few thousand premises, that second check is much slower than the pre-SG19 one (which asserted the negated claim and called `check()`). Past roughly 1000 premises, Z3 enters a phase that checks neither its timeout nor `Context.interrupt()`. The problem is `lia_chain(n)`: `n − 1` premises `b_i > b_{i+1} + 1` and the claim `b0 > 0`. This is the shape of an uncited step's pool in a trace of numeric steps, where every prior VALID step is a premise.

Per-check wall time, same input on one thread (`audit_load_4 assume`):

| Premises | As built (assumption) | Pre-SG19 (add) | Ratio |
|---|---|---|---|
| 100 | 21.7 ms | 20.0 ms | 1.1× |
| 400 | 95.8 ms | 58.6 ms | 1.6× |
| 800 | 455 ms | 164 ms | 2.8× |
| 1200 | 1684 ms | 356 ms | 4.7× |
| 1600 | 5282 ms | 642 ms | 8.2× |

**Timeouts are not honoured** (`audit_load_4 overrun`):

| Premises | Timeout | As built | Pre-SG19 |
|---|---|---|---|
| 1000 | 0.5 s | `timeout` after 0.90 s (1.8×) | `not_entailed` 0.28 s |
| 1400 | 0.5 s | `timeout` after 2.65 s (5.3×) | `not_entailed` 0.44 s |
| 1600 | 0.5 s | `timeout` after 4.14 s (**8.3×**; 10.5× in another run) | `timeout` 0.56 s (1.05×) |
| 2000 | 1.0 s | `timeout` after 8.39 s (8.4×) | `not_entailed` 0.94 s |
| 2000 | **5.0 s (default)** | `timeout` after **8.48 s (1.7×)** | `not_entailed` 0.87 s |

The answer gets worse as well as later: the pre-SG19 path decides 1000–2000-premise checks (`not_entailed`, the correct verdict) well within the limit, and the current path returns UNGROUNDED/`timeout` for them.

**Cancels are not honoured either.** The same 1600-premise check with a 30 s timeout, cancelled 0.8 s in, held its worker for another **3.2–4.4 s**. During that time SG19's re-send thread called `ctx.interrupt()` 609 times (every 5 ms), and none of the calls took effect. With 2 workers, two such checks block every check in the process, and `shutdown()` blocks for the same time, because it joins the workers.

**In the verifier** (`audit_load_4 assume`, `lia_trace`, uncited numeric premises plus one inference):

| Steps | Step p95 / max, as built | Pre-SG19 |
|---|---|---|
| 200 | 33.0 / 101.2 ms | 15.2 / 28.1 ms |
| 400 | 87–96 / 127–159 ms (1.7–1.9× budget) | 34–45 / 76–81 ms |
| 800 | **454 / 563 ms (9.1× budget)** | 125 / 196 ms |

SG12's design facts are also slower: with 1000 numeric parameters, step p95 is 56.2 ms against 22.4 ms on the pre-SG19 path. Boolean chains (`chain`, the bench corpus) are not affected; their p95 stays at 6.8–17.5 ms (see "What held up").

**Repro.**
- `test_a_check_returns_within_its_timeout` asserts `overrun_ratio(1600, 0.5) <= 1.5`. Today the ratio is 8.3–10.5. On the pre-SG19 path it is 1.05.
- `test_cancelling_a_check_frees_its_worker_promptly` cancels the same check (30 s timeout) after 0.8 s and asserts its worker is free within 0.5 s. Today it is free after 3.2–4.4 s.

Script: `audit_load_4 assume overrun`.

**Root cause.** `src/autolab/verification/solver.py:144` (`solver.check(z3.Bool(_NEGATED_CLAIM, ctx))`). Checking under an assumption sends this LIA problem down a slower path in Z3, and part of that path does not poll the resource limit that both `timeout` and `interrupt()` set. The same instance without the assumption (assert `claim!`, then `check()`) honours the limit. `solver.py:46` and `solver.py:247-251` (the 5 ms re-send) cannot help while Z3 is not polling.

**Suggested fix.**
- Keep the single `from_string` call (that is SG19's GIL win), but for the entailment check assert the negated claim and call `check()`: `solver.add(z3.Bool(_NEGATED_CLAIM, ctx)); solver.check()`. The solver is per check, so nothing needs popping. On the repro this gives 0.56–0.65 s against 4.1–5.3 s, with a timeout ratio of 1.05.
- Add a regression test that bounds `check_entailment` wall time against its timeout on a large linear pool (the repro).
- Longer term, a check that outlives `timeout + grace` cannot be stopped on a thread. Only a process worker (AUDIT-10 #1's alternative) can be killed.

**Resolution (SG22).** The entailment check asserts `claim!` and calls `check()`; the document is still loaded with one `Solver.from_string`. Of the variants measured on `lia_chain(1600)` (30 s timeout), this one took 0.70 s, against 5.5 s for push/add/check, 2.0 s for a second solver on the same document and 6.3 s for the assumption. On the AUDIT-10 #1 Boolean loads it matches the assumption path (10 x 100: 3.7–4.0 s, step p95 7.8–8.8 ms, against 3.7–4.2 s, 8.3–9.6 ms). The add path still has an unpolled setup phase: a deadline that fell early in the entailment check overran by up to 0.3 s (ratio 1.5–1.6 in 3 of 30 runs). So the check is no longer started when less time is left than half of what the consistency check took (`_SETUP_SHARE`); it returns `timeout` at once. `audit_load_4 overrun`, before → after: 1600 premises at 0.5 s 5.34 s (10.7x) → 0.51 s (1.0x); 2000 at 1.0 s 10.99 s → 0.83 s; 2000 at 5.0 s `timeout` after 10.99 s → `not_entailed` after 1.12 s; a cancel 0.8 s into a 1600 / 2000 check freed the worker after 4.54 / 10.11 s → 0.07 / 0.01 s. Both repros pass as plain tests with their assertions unchanged. Their setup changed, because the add path finishes long before 3x the consistency check: the deadline and the cancel now land a quarter of the way into the entailment check, measured on the machine by spying on `Solver.check` (best of two), and the cancel test waits for the second `check` call instead of a call with assumptions. With the SG19 solver both still fail (ratio 3.0; worker freed after 3.5–3.9 s).

---

## 2. High: two hard runs starve every other run of the 2-worker pool

**Impact.** SG19 cut the default pool from 8 workers to `min(2, os.cpu_count())`. That is right for short, GIL-bound checks (AUDIT-10 #1). But a check that runs to its timeout (`SOLVER_TIMEOUT_S` = 5 s) occupies a worker for the full 5 s. Two runs whose traces Z3 cannot decide (nonlinear arithmetic, as in AUDIT-2 #1's load) then hold both workers back to back. On a 1-CPU host (1 worker), one such run is enough. `SolverExecutor` admits checks in FIFO order through a per-loop semaphore, so every check of every other run waits for one hard check to end. A run verifies its trace one check at a time, so an easy 10-step trace pays that wait about ten times.

A 10-step easy cited verify (0.05–0.18 s alone) while hard runs make back-to-back runs-to-timeout checks (`audit_load_4 starve`, `fast_trace_under_slow_runs`):

| Workers | Hard runs | Check timeout | 10-step verify |
|---|---|---|---|
| 2 (default, 2+ CPUs) | 1 | 1 s | 0.10 s |
| 2 | 2 | 1 s | 8.94 s |
| 2 | 2 | **5 s (default)** | **30.0 s** (at the 30 s budget) |
| 2 | 3 | 5 s | **50.6 s** (1.7×) |
| 2 | 4 | 5 s | **74.7 s (2.5× the 30 s budget)** |
| 1 (1-CPU host) | 1 | 5 s | **50.2 s** (1.7×) |
| 8 (pre-SG19) | 2 | 1 s | 0.21 s |
| 8 (pre-SG19) | 4 | 5 s | 0.2 s |

- **Budget.** CLAUDE.md requires a 10-step trace to verify in under 30 s. With 4 concurrent hard runs on the default pool, an unrelated easy trace takes 74.7 s (2.5×), against 0.2 s on the old 8-worker pool. AUDIT-2 and AUDIT-6 used 10–50 concurrent runs; a handful of them hitting undecidable arithmetic is a realistic load.
- **Throughput of the hard checks themselves.** `audit_load pool --default-timeout`: 50 runs-to-timeout verifies take 125.5 s at 5 s (35 s on 8 workers in AUDIT-10) and 25.3 s at 1 s (7.1 s before). Each check's `solver_ms` is still bounded (max 5109 ms), because queueing is excluded, so `solver_ms` and the bench never show this.
- **The pool size does not track the work.** A check stuck in one long `Z3_solver_check` call releases the GIL for its whole duration. Slow checks therefore do not convoy the way short checks do (AUDIT-10 #1), and a larger pool would cost nothing while they run.
- **CPU count.** With `os.cpu_count` patched in a child interpreter (`audit_load_4 cpus`, 10 × 100-step load), 1 CPU gives 1 worker (4.87 s, p95 5.6 ms), 2 gives 2 (3.02 s, 8.3 ms), 4 gives 2 (3.70 s, 11.7 ms) and 64 gives 2 (4.46 s, 12.6 ms). The short-check load is fine at every size. The starvation above is worst on 1 CPU, where a single hard run blocks the process.

**Repro.** `test_hard_runs_do_not_starve_an_easy_verify` holds both workers of a 2-worker pool with two hard runs whose checks run to a 0.5 s timeout, and asserts that an easy 10-step verify finishes in under one hard check (0.5 s). Today it takes 3.0–4.9 s. On 8 workers it takes 0.06 s. Script: `audit_load_4 starve`.

**Root cause.**
- `src/autolab/verification/solver.py:42`: `SOLVER_WORKERS = min(2, os.cpu_count() or 1)` sizes one pool for both short checks and checks that run for up to `SOLVER_TIMEOUT_S`.
- `solver.py:273-282` and `solver.py:292-298`: a single FIFO admission (a per-loop `asyncio.Semaphore(workers)`) with no notion of fairness between runs or of long checks.
- `vericot.py:215`: every check of every run goes through that one pool.

**Suggested fix.**
- Use two lanes. Run every check first with a short slice on the 2-worker lane (for example `min(remaining, 100 ms)`). A check that returns `unknown` with time left is re-run on a separate "slow" pool sized for blocking work (for example `cpu + 4`), with the rest of its deadline. Short checks keep SG19's GIL behaviour, and hard checks wait on Z3, not on each other.
- Alternatively, grow the pool while all workers have been busy for more than some threshold, or admit checks round-robin per run so that one run cannot hold more than one worker while others wait.
- Add the repro as a regression test, plus a 1-worker variant.

**Resolution (SG22).** `SolverExecutor` keeps `workers` slots, but a Z3 check (one with a context attached) still running after `_SLICE_S` = 50 ms gives its slot to the next caller and runs on as a slow check, while fewer than `slow_workers` (default `SOLVER_SLOW_WORKERS` = `min(32, cpu + 4)`) checks have done so. The pool's thread limit grows by one per slow check and shrinks back when it ends, so short work never gets more than `workers` threads (AUDIT-10 #1 is unchanged). The check is not moved or re-run, so its deadline, its `solver_ms`, cancel and shutdown are unchanged. A two-lane design that re-runs sliced checks on a second pool was rejected: the slice would unwind through callers' own wrappers of `check_entailment`. `audit_load_4 starve`, before → after: 2 workers, 2 hard runs at 1 s 5.95 s → 0.07 s; 1 worker, 1 hard run at 1 s 10.00 s → 0.09 s; 2 hard runs at 5 s 35.0 s → 0.1 s; at 5 s, 4 hard runs on 2 workers 0.27 s, 4 on 1 worker 0.29 s. Above `slow_workers` concurrent hard checks the extra ones keep their slots: 12 hard runs on 2 workers (4 CPUs, 8 slow) still take 20.3 s. The repro passes as a plain test with its assertion unchanged; a 1-worker variant and a bound test for `slow_workers` were added.

---

## Earlier fixes, re-measured

| Fix | Claim | Re-measured now | Status |
|---|---|---|---|
| AUDIT-2 #1 (SG10) | Z3 off asyncio's default pool; `getaddrinfo` during 50 slow verifies 0.01 s; `solver_ms` excludes queueing | `getaddrinfo` 0.00 s (idle 0.6 ms); `solver_ms` max 1006 ms (50 × 1 s) and 5109 ms (50 × 5 s). The end-to-end wall is now 25.3 s and 125.5 s (was 7.1 s and 35 s on 8 workers), because there are only 2 workers (finding 2) | Holds (queueing worse: #2) |
| AUDIT-2 #2, #3 (SG9) | atomic upserts, `(run_id, id)` keys | Their repros pass in the suite (`test_audit_2`). AUDIT-6 #2 (`database is locked`) is still an open xfail | Holds |
| AUDIT-2 #4 (SG8) | ≤ 3 HTTP attempts, jitter, `retry-after` | Repros pass. AUDIT-6 #3 (retry-after herd) is still an open xfail | Partly holds (unchanged) |
| AUDIT-2 #5 (SG10) | 10 steps at 100/200/400 ms per call ≈ N + 1 rounds | 1.18 / 2.27 / 4.46 s (0.56–0.59 × the sum) | Holds |
| AUDIT-2 #6 (SG10, SG19) | a cancel frees the worker | `audit_load cancel` 0.00 s. 2000 random cancels: 1762 mid-flight, 0 errors, 0 handles left, next verify all VALID. 400 cancels of running pathological checks: peak 3 re-send threads, 0 left after, worst release 174 ms, 9.5 ms for one check. **Does not hold** for large linear pools: 3.2–4.4 s after 609 interrupts | Holds except finding 1 |
| AUDIT-2 #7 (SG10) | declaration lines scanned 5 100 / 320 800 at 100 / 800 uncited steps | 5 100 / 320 800; 800 uncited steps 7.4 s, max step 18.7 ms | Holds |
| AUDIT-6 #1 (SG11) | no crash with verifies on several threads | `audit_load_2 segv` 0/30 crashed; `audit_load_3 threads` 0/20 (2 × 20 … 16 × 5); with `reset_params()` racing: 0/20 crashed or wrong (below) | Holds |
| AUDIT-10 #1 (SG19) | 10 × 100-step p95 ≈ 5.8 ms on 2 workers | 10 × 100: 2.98 s, p95 6.8 ms; 30 × 100: 11.6 s, p95 11.8 ms; 10 × 200: 8.8 s, p95 13.0 ms; 4 × 400: p95 14.3 ms; 2 × 800: p95 17.5 ms (all VALID, loaded host). The repro passes | Holds for Boolean checks; LIA pools regressed (#1) |
| AUDIT-10 #5 (SG19) | no worker enters z3py's printer | `test_counter_models_are_never_printed_on_two_threads_at_once` and `test_counter_model_does_not_use_the_shared_printer` pass | Holds |
| AUDIT-6 #7, AUDIT-7 #6 | not fixed | `shutdown()` during 10 / 50 running verifies: every verify raised `RuntimeError` (cannot schedule new futures). `test_audit_6::test_shutdown_does_not_cancel_callers_on_other_loops` XPASSed (strict, so it failed) in 1 of 8 runs alone and in 1 of 2 full-suite runs: when the second loop's check has not reached the pool by the time of the shutdown, it gets `RuntimeError` instead of `CancelledError` | Open; the repro is flaky under load |

## CLAUDE.md budgets

- **10-step trace in < 30 s.** 0.05 s at zero LLM latency. With faithfulness on, 11.08 s at 1 s per call and 22.09 s at 2 s per call. It holds for one run alone while the mean structured call takes ≤ 2.7 s. **It does not hold** when 3 or more hard runs share the default pool (50.6–74.7 s; finding 2).
- **50 ms p95 per step (`scripts/bench_verifier.py`).** `bench_verifier --runs 20`: step p95 9.92 ms, max 17.49 ms (1680 samples, loaded host). It holds for the golden corpus and for concurrent Boolean chains up to 800 steps (p95 ≤ 17.5 ms). It does not hold for numeric uncited traces of 400 or more steps (87–454 ms; finding 1).

## What held up

- **`z3.reset_params()` before each check.** It costs 0.26 µs per call. Races, in child interpreters: 2, 4 and 8 threads, each verifying on its own loop through the shared pool, and 4 threads with 2 more threads calling `z3.set_param` in a loop. That gave 20 interpreters with 0 crashes, 0 hangs and 0 non-VALID steps. A bare stress (4 threads × 2000 `reset_params(); Context()`, and 2 or 8 threads × 300 `check_entailment`) gave 0/15 crashes. As PLAN notes, a global parameter the host sets (for example a backstop `timeout`) is cleared by the next check (`host_param_survives_a_check()` is False).
- **Re-send thread.** At most one thread per running check, so at most `workers` (peak 3 when a thread outlived its check by a few ms) under 400 cancels. 0 were left after. A thread lives only until its check detaches, and costs one GIL acquisition per 5 ms; that was not measurable next to Z3's own CPU in the overrun case (3.11 s of process CPU in 3.21 s, nearly all of it the stuck check).
- **SG15's cached probe.** `_is_z3_builtin` is an `lru_cache(maxsize=4096)`, so the cache is bounded (4096/4096 after 100 000 novel names). An uncached probe costs 243–267 µs on the event loop. RSS grew 12.5–14.5 MB per 100 000 novel names on a fresh interpreter, and 0 MB once Z3's symbol table was warm. That is Z3 interning each name process-wide (AUDIT-10 noted the same cost for parsing). 100 000 failing low-level parses in one context grew RSS by 0.2 MB, so the probe does not leak the AST vector the parse returns.
- **SG16's second pass.** On a payload-bound trace of P independent premises and a conclusion citing them all, checks are 2P + 4 without a circular premise and 3P + 8 with one (one circularity check per VALID premise, then a full re-check). P = 400: 804 checks in 5.28 s, and 1208 checks in 7.76 s. Wall time is linear in P: 1.13 / 2.58 / 5.28 s at P = 100 / 200 / 400.
- **Event loop.** Max stall 7.3 / 9.4 / 20.3 ms with 1 / 10 / 50 concurrent 60-step numeric verifies, and 6.1–36.2 ms across the `pool` loads.
- **Memory.** RSS 179 → 182 → 183 MB over 1000 verifies (100 at a time), flat after the first batch.
- **Concurrency correctness.** 20 and 50 concurrent golden verifies: 0 verdict mismatches against the sequential run. Every concurrent chain verify in `pool` was all VALID.
