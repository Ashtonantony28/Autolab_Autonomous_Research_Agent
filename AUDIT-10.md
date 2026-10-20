# AUDIT-10: Performance under load, round 3

Auditor: independent (wrote none of the audited code or the earlier audits). Base: `origin/claude/sg14-timing-tests` @ `7b8c452` (SG11–SG14 merged).
Machine: 4 CPUs, 16 GB RAM, Linux, Python 3.11, z3 4.16.0. Default solver workers = 8.
All runs use `FakeLLMClient`, subclasses of it, or stubs. Nothing touches the network and no real LLM is used.

**Load caveat.** For most of the audit this machine was shared with other agents' test and mutation runs (load average 4 to 9 on 4 CPUs). Absolute wall times are noisy. Each repro therefore compares two measurements taken in the same process, or counts something, instead of using a fixed time bound where it can. Where a number varied between runs, the range is given. Finding 1 is much *worse* on a quiet machine (load average 1.4–2.7), because more worker threads then run at the same time; its headline numbers come from those quiet runs.

- Repros: `tests/audit/test_audit_10.py`. Each finding has one test marked `xfail(strict=True, reason="AUDIT-10 #n")` that asserts the correct behaviour. All five fail on their assertion under `--runxfail`. The file takes about 19–22 s, and its slowest test about 8 s.
- Measurements: `uv run python -m scripts.audit_load_3 [section ...]`. Sections: `pool`, `scaling`, `contexts`, `threads`, `churn`, `long`, `design`, `hitl`, `printer`, `symbols`, `pytest_timeout`, `bench`, `concurrent`. The earlier rounds were re-run with `uv run python -m scripts.audit_load --default-timeout` and `uv run python -m scripts.audit_load_2`.

| # | Severity | Title |
|---|---|---|
| 1 | **High** | The 8-thread solver pool convoys on the GIL: 10 concurrent verifies run 6× slower than on one worker, and step p95 reaches 805–908 ms (budget 50 ms) |
| 2 | Medium | `pytest-timeout` cannot stop a Z3 call on the main thread, so AUDIT-8 H1's hang (mutant S08) still hangs the suite |
| 3 | Medium | SG11 keeps a ~16.5 MB `z3.Context` alive for every thread that has ever formalized |
| 4 | Low | Every solver check creates and frees a fresh 16.5 MB `z3.Context`, which costs more than the check itself |
| 5 | Low | Every INVALID check formats a counter-model through z3's thread-unsafe global printer, and the verifier then discards it |

No critical findings. One high finding (#1).

---

## 1. High: the solver pool convoys on the GIL under concurrent verifies

**Impact.** `SolverExecutor` runs checks on `SOLVER_WORKERS = min(32, cpu + 4)` = 8 threads. A step check is a long chain of short ctypes calls into Z3 (context creation, parse, AstVector indexing, one `add` per assertion, `inc_ref`/`dec_ref` for every AST wrapper, two `check` calls). z3py releases the GIL around every one of those calls and has to win it back after each, so threads cannot run checks in parallel. With several workers, the GIL becomes a convoy: each hand-off is a futex wake and a context switch. Throughput collapses, and every check's `solver_ms` (wall time on the worker) grows by up to two orders of magnitude.

Concurrent cited-chain verifies on one loop, by pool size, on a quiet machine (`audit_load_3 pool concurrent`, load average 1.4–2.7):

| Load | Workers | Wall | CPU / wall | `solver_ms` p50 / p95 / max |
|---|---|---|---|---|
| 10 × 40-step | 1 | 2.11 s | 1.37 | 4.3 / 6.2 / 13.2 ms |
| | 2 | **1.22 s** | 4.74 | 4.6 / 10.3 / 15.9 ms |
| | 4 | 3.20 s | 3.04 | 13.7 / 111.3 / 139.3 ms |
| | **8 (default)** | **6.25 s** | 2.24 | 53.4 / **358.6** / 429.7 ms |
| 30 × 40-step | 1 | 5.79 s | 1.83 | 3.9 / 5.8 / 9.5 ms |
| | 2 | **4.32 s** | 2.19 | 4.8 / 13.8 / 70.0 ms |
| | 4 | 10.70 s | 1.97 | 11.8 / 124.5 / 158.8 ms |
| | **8 (default)** | **18.47 s** | 1.80 | 54.0 / **342.7** / 440.8 ms |
| 10 × 100-step | 1 | 5.24–5.58 s | | 4.1–4.5 / 7.0–7.4 ms |
| | **8 (default)** | **31.7–35.2 s** | | 54.6–56.4 / **805–908** ms |
| 50 × 100-step | **8 (default)** | **166.9 s** | | p95 870.4 ms |

- **Budget.** At 10 concurrent 100-step verifies the default pool's step p95 is 805–908 ms, **16–18×** the 50 ms budget (`STEP_BUDGET_MS`). At 30 × 40 it is 343 ms (6.9×). On one worker the same loads stay at p95 5.8–7.4 ms.
- **Throughput.** 10 concurrent 100-step verifies take 31.7–35.2 s on the default pool, against 5.2–5.6 s on one worker (about 6× slower) and 0.66 s for one verify alone. Two workers are the fastest setting measured; four are already slower than one.
- **Where the time goes.** For 10 × 100 on 8 workers, user time was 13.3–17.1 s and **system time 36.3–45.1 s** within 31.7–35.2 s of wall time. `strace -f -c` on a 10 × 60 run counted 2.4 M `futex` calls (97.6% of syscall time). In four gdb samples of all threads during the run, 29 of 32 worker-thread stacks were in `PyEval_RestoreThread`, called from ctypes (`_PyObject_MakeTpCall`), waiting to re-acquire the GIL. The other threads were the event loop in `epoll_wait` and Z3's idle `scoped_timer` threads.
- **Load dependence.** On the shared, loaded machine the same loads gave p95 52.6–137.5 ms (1.05–2.75× the budget), because fewer workers ran at the same moment. The quieter the host, the worse the convoy. A dedicated 4-core server is the worst case.
- **Bare checks.** A tight loop of trivial checks scales only slightly with threads (`audit_load_3 scaling`): 1 / 2 / 4 / 8 threads gave 223 / 367 / 341 / 306 checks/s, against 806 checks/s for 4 processes. Even the best thread count stays far below what processes reach.
- **Realism.** AUDIT-2 and AUDIT-6 ran 10 and 30 concurrent graph runs, and each run verifies one trace at a time, so 10 concurrent verifies is a realistic load. `bench_verifier` never sees this, because it verifies one trace at a time.
- **Event loop.** The loop thread shares the GIL with the workers. On the quiet machine the longest stall was 8.8 ms at 10 and 81.5 ms at 50 concurrent 100-step verifies. On the loaded machine it was 40.1 ms and **273 ms**.

**Repro.** `test_the_default_solver_pool_does_not_inflate_solver_ms_under_concurrency` (perf, about 6 s) runs 12 concurrent 30-step verifies, first on a 1-worker pool and then on the default pool. It asserts the default pool's p95 is at most 2 × the 1-worker p95. Today it is 46.1 ms against 4.6 ms on the loaded machine, and 273.6 ms against 5.7 ms on a quieter one. Script: `audit_load_3 pool scaling concurrent`.

**Root cause.**
- `src/autolab/verification/solver.py:38` sizes the pool for I/O-bound work (`cpu + 4`), but the work is GIL-bound. `solver.py:218` builds the pool with that size.
- `solver.py:94-146`: each check makes thousands of short GIL-releasing ctypes calls: the context at `:94`, the parse at `:100`, AstVector indexing and one `solver.add` per assertion at `:112-113`, and the model at `:135`.
- `solver.py:256-258`: `solver_ms` is wall time on the worker, so it includes the GIL convoy (queueing for a worker is still excluded, as AUDIT-2 #1 asked).

**Suggested fix.**
- Default to a small pool, for example `min(2, cpu)` (2 workers were fastest here), and keep `Verifier(solver_executor=...)` for hosts that want more. Add a regression test that compares the default pool against one worker under concurrent verifies (the repro).
- For real parallelism, run checks in a `ProcessPoolExecutor`: 4 processes reached 806–972 checks/s, against at most 375 for any number of threads. The interrupt handle would become a per-process timeout or kill.
- Cut ctypes round trips per check, and assert the parsed document with one call (`solver.from_string(smt)`, with `push`/`pop` or `check(assumption)` for the negated claim) instead of indexing the AstVector in Python.
- Also record `solver_ms` as thread CPU time (`time.thread_time()`), next to wall time, so the budget can tell Z3's own time apart from GIL waits.

---

## 2. Medium: `pytest-timeout` cannot stop a Z3 call on the main thread

**Impact.** SG14 added `pytest-timeout` (`timeout = 60`) so that "a hung solver check fails and names the test" (AUDIT-8 H1, `pyproject.toml:67`). It uses the default `signal` method. The SIGALRM handler is Python code, and it runs only when the main thread returns to the interpreter. A Z3 check on the main thread is one long ctypes call, so the limit fires only after Z3 returns. If Z3 never returns, the limit never fires.

| Probe (child pytest with the repo config) | Result |
|---|---|
| `--timeout=1`, a 6 s Z3 check on the main thread | child took 8.0 s; the test was reported as timed out after it had run 7 s |
| `--timeout=2`, a 12 s check | 14.2 s (12.05 s in the test) |
| Same check awaited on the solver executor, `--timeout=2` | failed at 2.00 s; cancellation interrupted the check (works) |
| **Mutant S08** (`_remaining_ms` returns 0, Z3's "no limit"), `tests/unit/verification` + `test_audit_2.py`, `--timeout=10` | **hung**: killed by the outer limit after 400 s, and again after 200 s with `-v`. The last line was `test_latency.py::test_hard_premise_consistency_check_times_out`, with no failure and no test named. `audit_load_3 pytest_timeout` on that test alone: killed after 30 s (`--timeout=1`) and after 120 s (`--timeout=10`), never named |

So the concrete hang that AUDIT-8 H1 reported (S08) still hangs the suite, and CI would stop only at its job timeout. The sync tests that run Z3 on the main thread include `test_latency.py:91` (`test_hard_premise_consistency_check_times_out`), `test_audit_2.py:291`, `test_audit_4.py:76` and all of `tests/unit/verification/test_solver.py`. Async tests that await the executor are covered.

**Repro.** `test_a_hung_main_thread_solver_check_fails_within_the_per_test_limit` (perf, about 8 s) runs `test_hard_premise_consistency_check_times_out` under S08 (a `-p` plugin written to a temp dir) with `--timeout=1` in a child pytest. It asserts that the child exits on its own within 6 s and names the test. Today it is killed at 8 s. Script: `audit_load_3 pytest_timeout`.

**Root cause.** `pyproject.toml:83` (`timeout = 60`; no `timeout_method`, so `signal`), together with tests that call `Z3Solver.check_entailment` synchronously on the main thread (`tests/unit/verification/test_latency.py:91-99` and the others listed above).

**Suggested fix.** Set `timeout_method = "thread"` in `[tool.pytest.ini_options]`. Thread mode dumps every thread's stack (including the hung test's frame) and exits, so a hang becomes a named failure. As a backstop, have `tests/conftest.py` set a global Z3 limit (`z3.set_param("timeout", 60_000)`), so a check built without `Z3Solver`'s deadline cannot run forever. Verify that this backstop still applies when a solver sets its own `timeout` to 0, as S08 does; if it does not, the thread method is the fix. Optionally run the sync solver tests through the executor so that `signal` mode can cancel them.

---

## 3. Medium: SG11 keeps a ~16.5 MB `z3.Context` alive per thread

**Impact.** A `z3.Context` costs about **16.5 MB RSS** in Z3 4.16 (10 live contexts: +130 MB; 200: +3.3 GB). The raw C API costs the same, and the memory is returned on delete. SG11's fix for AUDIT-6 #1 keeps one context per thread in a `threading.local` (`formalizer._parse_context`) for that thread's whole life. Every thread that has ever formalized a step therefore holds 16.5 MB, whether or not it verifies again.

| Idle threads alive (child interpreter) | RSS, threads only | RSS, after one `formalize` each |
|---|---|---|
| 4 | +0.1 MB | +66 MB |
| 16 | +0.3 MB | **+265 MB** |
| 64 | +1.0 MB | **+1057 MB** |

- After the 64 threads exited, 541 of the 1034 MB stayed resident (glibc per-thread arenas), so even thread churn leaves a high-water mark.
- The fix works for what it was meant to do. There were no crashes (see the re-check table), and contexts are freed at thread exit (400 short-lived thread-verifies left 0 live `z3.Context` objects).
- But memory now scales with the host's thread count. A threaded host such as FastAPI sync handlers (a 40-thread pool), a worker pool that calls `asyncio.run` per job, or Streamlit callbacks pays 16.5 MB per thread forever. In 400 thread-verifies at 16 threads at a time, RSS stayed at 621–654 MB, against 264–395 MB at 4 at a time.
- The single event-loop thread of the CLI pays it once (16.5 MB).

**Repro.** `test_threads_that_have_formalized_do_not_each_keep_a_z3_context` runs 16 idle threads in a child interpreter, with and without one `Formalizer.formalize` each. It asserts the extra RSS is under 2 MB per thread. Today it is 264.6 MB for 16 threads. Script: `audit_load_3 contexts churn`.

**Root cause.** `src/autolab/verification/formalizer.py:117-129`: `_PARSE_CONTEXTS = threading.local()`; `_parse_context()` creates a `z3.Context()` per thread and never releases it.

**Suggested fix.** Bound the number of parse contexts, not the number of threads. Use a small process-wide pool (for example a `queue.LifoQueue` of `SOLVER_WORKERS` contexts) that a parse checks out and returns. That is just as thread-safe, since a context is used by one thread at a time, and memory is fixed. Alternatively, run the formalizer's parse check on the solver executor and share its per-worker contexts (finding 4). If a per-thread context stays, document the 16.5 MB per thread and give a way to drop it (`del _PARSE_CONTEXTS.ctx`) when a worker thread finishes a job.

---

## 4. Low: every check creates and frees a fresh 16.5 MB `z3.Context`

**Impact.** `Z3Solver.check_entailment` builds a new context for every check, so most of a cheap check's time goes to faulting in and freeing about 16 MB.

- Same parse and both checks on one reused context: `check_entailment` takes **2.8–3.0×** as long when the machine is quieter, and 5.4–5.9× under contention (`context_overhead_ratio`, interleaved on one thread).
- Single-thread throughput of the 50-symbol chain check: 223–253 checks/s with a fresh context, **573–691 checks/s** with one context per thread (`audit_load_3 scaling`).
- Inside a verify, the solver worker's `z3.Context()` took p50 2.3–4.1 ms and p95 2.7–7.8 ms. That is 57% of `solver_ms` for a single 40-step verify, and 4–13% at 4–30 concurrent verifies, where `solver_ms` is dominated by the GIL convoy of finding 1.
- 50 checks construct 50 contexts. The golden bench is within budget (p95 7.6 ms), so this is cost, not a broken budget. Reusing contexts does not fix finding 1 on its own: with one context per worker, 10 × 100-step verifies on 8 workers still took 35.2 s (40.9 s system time).

**Repro.** `test_a_trivial_check_is_not_dominated_by_creating_a_context` (perf, about 2 s) asserts the ratio above is under 1.5. Today it is 5.9 (2.8–5.9 across runs). Script: `audit_load_3 contexts`.

**Root cause.** `src/autolab/verification/solver.py:94` (`ctx = z3.Context()` per call).

**Suggested fix.** Give each solver worker thread its own long-lived context (as the formalizer already does). Build a fresh `z3.Solver(ctx=ctx)` per check, and recycle the context every N checks to bound Z3's internal tables. Re-check the interrupt handle on a reused context: `ctx.interrupt()` must not leak into that worker's next check, so reset it or recycle the context after an interrupt.

---

## 5. Low: counter-models go through z3's global printer on every worker, then are discarded

**Impact.**
- `check_entailment` calls `str(solver.model())` for every INVALID result. `Verifier._check` copies only verdict, reason and details into `StepVerdict`, so `counter_model` is never stored, traced or shown. The only reader is one unit test.
- Formatting is not free: 0.7 / 2.7 / 6.8 / 5.7 ms of a 6.8 / 9.1 / 16.9 / 24.4 ms INVALID check with 12 / 50 / 200 / 800 symbols (up to 40%, all of it Python holding the GIL; see finding 1).
- z3's printer keeps its state in module globals (`z3printer._PP`, `_Formatter`). The 8 solver workers enter it concurrently: 16 INVALID checks on 8 workers put **5–8 threads** inside it at once.
- With a short GIL switch interval (1e-5 s), 6 of 320 concurrently built `counter_model` strings differed from the sequential ones (3 of 320 in another run). At the default 5 ms interval, 0 of 320 differed.
- No exception was observed, so today this is wasted work plus a latent race. Any future reader of `counter_model`, such as a dashboard counterexample view, would get occasionally corrupted text.

**Repro.** `test_counter_models_are_never_printed_on_two_threads_at_once` wraps `z3printer.PP.__call__` with a counter (holding each call 20 ms), runs 16 INVALID checks on an 8-worker executor, and asserts at most one thread is ever inside the printer. Today the peak is 5–8. Script: `audit_load_3 printer`.

**Root cause.** `src/autolab/verification/solver.py:134-141` (`model = solver.model()` and `counter_model=str(model)` on the worker thread), `src/autolab/verification/vericot.py:215-222` (the field is dropped), and z3's `z3printer.py:1423-1424` (module-global `_PP` and `_Formatter`).

**Suggested fix.** Stop building `counter_model` in the verifier path: make it opt-in, or drop the field. If it is kept, use `model.sexpr()`, which is formatted in C by `Z3_model_to_string` with no shared Python state, or format under a process-wide lock. Add it to `StepVerdict` only if something will read it.

---

## Earlier fixes, re-measured

| Fix | Claim | Re-measured now | Status |
|---|---|---|---|
| AUDIT-2 #1 (SG10) | `getaddrinfo` during 50 slow verifies 0.01 s; worst `solver_ms` ≈ 1011 / 5011 ms at 50 × 1 s / 50 × 5 s | `getaddrinfo` 0.01 s (idle 0.5 ms); `solver_ms` max 1052 ms (50 × 1 s) and 5100 ms (50 × 5 s); 50 × 5 s still takes 35.3 s wall (queueing, open as stated) | Holds |
| AUDIT-2 #2 (SG9) | no `IntegrityError` | 20 concurrent appends of one id: 0 errors; 10 `ensure_run`: 0; 200 upserts of one key from 4 engines: 0 errors, 1 row. The separate `database is locked` failures (AUDIT-6 #2) remain: 10/100 runs failed with 1 engine, 67/100 with 4, 172/200 with 8 | Holds |
| AUDIT-2 #3 (SG9) | `(run_id, id)` keys | `test_same_entity_id_in_two_runs_stays_in_both` passes | Holds |
| AUDIT-2 #4 (SG8) | ≤ 3 HTTP attempts, jitter, `retry-after` | Production path ≤ 3 (its test passes; `audit_load retry` still prints 9 because the script injects an SDK client with `max_retries=2`). Jitter spread 181 / 482 ms (20 clients). `retry-after` honoured, but 50 clients retry within 10.7 / 9.7 ms of each other (AUDIT-6 #3, open) | Partly holds (unchanged since round 2) |
| AUDIT-2 #5 (SG10) | 10 steps at 100/200/400 ms per call: 1.17 / 2.27 / 4.47 s | 1.16 / 2.27 / 4.47 s (0.56–0.58 × the sum of call latencies) | Holds |
| AUDIT-2 #6 (SG10) | next pool job after cancelling 8 verifies 0.00 s | 0.00 s. 2000 randomly timed cancellations: 1533 mid-flight, 0 errors, 0 interrupt handles left, next verify all VALID. Under pytest-timeout, an executor check is interrupted when its test times out | Holds |
| AUDIT-2 #7 (SG10) | declaration lines scanned for 100 / 800 uncited steps 5 100 / 320 800 | 5 100 / 320 800. 800 uncited steps take 15.7–15.8 s; non-premise max 46 ms; over all checks p95 33.0 ms, max 53.3 ms (2 of 800 steps over 50 ms) | Holds; still superlinear, as noted |
| AUDIT-6 #1 (SG11) | no crash with verifies on several threads | 0 crashes: `audit_load_2 segv` 0/30 (1 × 40, 2 × 20, 4 × 10); `audit_load_3 threads` 0/20 (2 × 20, 4 × 10, 8 × 10, 16 × 5); `test_audit_6` #1 passes. Thread churn: 400 short-lived thread-verifies (4 or 16 at a time) left 0 live contexts and 8–9 threads (the z3 pool) | Holds (memory cost: finding 3) |
| AUDIT-8 H1 (SG14) | a hung test fails and is named | Holds for async tests. Does not hold for a Z3 call on the main thread: S08 still hangs the suite | **Does not hold** (finding 2) |
| AUDIT-6 #2–#8 | not in SG11–SG14 | Still open, re-measured: store `database is locked` (above); `retry-after` herd 10.7 ms; judge fan-out peak 100 per verify and 874 for 20 concurrent; 97 of 100 formalize calls after a failed judge; bench counts 420 of 945 checks, and premise checks are 71% of solver time at 400 cited steps (p95 21.6, max 53.4 ms); 4 loops admit a pool queue of 26 and `shutdown()` gives `CancelledError`; reused graph grows 67 KB per run | Open, as recorded in AUDIT-6 |

## CLAUDE.md budgets

- **10-step trace in < 30 s.** 0.06 s at zero LLM latency. With faithfulness on, 11.07 s at 1 s per call and 23.59 s at 2 s per call (about 11 call latencies). It holds while the mean structured call takes ≤ 2.7 s. A 10-step bound payload trace takes 95 ms at zero latency. Live latency was not measured: there is no API key, and the brief rules it out.
- **50 ms p95 per step (`scripts/bench_verifier.py`).** `bench_verifier --runs 20`: p95 7.59 ms, max 24.77 ms (1680 samples). Counting the premise (`assumed`) checks the bench skips (AUDIT-6 #6): p95 8.96 ms, max 34.2 ms over 945 checks. It holds for one verify at a time. Under 10–30 concurrent verifies it does not (finding 1).

## What held up

- **Long traces (untraced, all checks counted).** Step p95 by trace size: 50 steps ≤ 12 ms, 100 ≤ 15 ms, 200 ≤ 12.6 ms, 400 ≤ 20.1 ms, 800 between 17.7 and 33.0 ms (max 62.8 ms, 0–3 steps over 50 ms). Payload-bound 800-step traces: 1203 checks in 13.1 s (cited), and 2000 checks in 18.7 s (circular, where SG7 re-checks the whole trace). Peak traced heap during an 800-step verify is 10.8 MB.
- **SG12's design facts.** On a 10-step bound Design trace, step p95 was 12.3 / 7.3 / 17.3 / 35.3 / 54.4 ms with 0 / 10 / 100 / 500 / 1000 numeric parameters, about 0.05 ms per fact per check. Realistic designs (≤ 50 parameters) add under 10 ms. Loop stall stayed ≤ 5.6 ms.
- **SG12's HITL policy re-check.** On `approve` of a Design escalation, the re-check is `cost_trigger`, pure Python with no measurable cost. For Research it is the contradiction verify over the hypotheses already in state. In a CLI run, state holds none at that point, so it returns at once with 0 LLM calls. With seeded priors it costs k + 2 LLM calls: 1 prior 0.01 s (0.82 s at 200 ms per call with faithfulness), 10 priors 0.06 s (2.67 s), 100 priors 0.59 s. Checks grow linearly in k (102 for 100 priors).
- **Solver timeouts.** 50 × 1 s and 50 × 5 s pathological checks are bounded as in the table above. Parsing is not covered by the deadline, but `max_tokens=4096` bounds a formalization to about 12–16 KB. A 12 KB nested term parses in 0.01 s; only far larger inputs (120 KB: 0.95 s; 600 KB: 27 s) exceed it. `(= x (^ 2 100000000))` timed out in 1.21 s against a 1 s limit.
- **Event-loop blocking from one verify.** 10 / 100 / 400 steps stalled the loop for at most 5.1 / 6.6 / 16.8 ms. A full graph run stalled it for 14–18 ms, which is `build_graph`'s compile. One 155 ms reading under heavy machine load did not reproduce. Concurrency is covered in finding 1.
- **Memory of the verifier.** RSS went from 359 MB at the start to 401–435 MB over 5000 verifies, with no monotonic growth after the first 1000. Z3 interns every symbol name process-wide, about 70 B per *novel* name: +28 MB per 400 000 novel names, +0.1 MB when names repeat. Live traces reuse a small vocabulary, so this was not filed.
- **Concurrency correctness.** 20 and 50 concurrent golden verifies gave 0 verdict mismatches against the sequential run. 10, 30 and 50 concurrent 100-step verifies had 0 non-VALID steps.
