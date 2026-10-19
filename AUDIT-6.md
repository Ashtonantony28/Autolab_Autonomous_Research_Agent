# AUDIT-6: Performance under load, round 2

Auditor: independent (wrote none of the audited code or the earlier audits). Base: `origin/claude/sg9-entity-identity` @ `8a1c1de` (SG7–SG10 merged).
Machine: 4 CPUs, Linux, Python 3.11, z3 4.16, SQLAlchemy 2.0, aiosqlite. Default solver workers = 8.
All runs use `FakeLLMClient`, stubs, or exceptions built on an in-process `httpx` request. Nothing touches the network and no real LLM is used.

- Repros: `tests/audit/test_audit_6.py`. Each finding has one test marked `xfail(strict=True, reason="AUDIT-6 #n")` that asserts the correct behaviour. The slowest takes 5.6 s, and the file takes about 18 s.
- Measurements: `uv run python -m scripts.audit_load_2 [section ...]` (sections: segv, loops, shutdown, races, threads, judges, payload, store, retry, memory, budget). Round-1 numbers were re-measured with `uv run python -m scripts.audit_load --default-timeout`.
- `make lint typecheck test`: lint and mypy clean; 616 passed, 12 skipped, 9 xfailed (8 new, plus 1 left from earlier audits).

| # | Severity | Title |
|---|---|---|
| 1 | **Critical** | Verifies on two threads segfault the interpreter: the formalizer parses on Z3's global context |
| 2 | Medium | SQLite store fails runs with `database is locked` under concurrent writers |
| 3 | Medium | `retry-after` overrides the jitter, so rate-limited clients retry in lockstep |
| 4 | Medium | Faithfulness judges fan out without a bound (100 in flight for one 100-step verify) |
| 5 | Low | A failed judge is noticed only after every step is formalized |
| 6 | Low | SG7's premise checks pool the whole trace, and the bench does not count them |
| 7 | Low | `SolverExecutor`'s bound and shutdown are per event loop |
| 8 | Low | A reused compiled graph grows by about 66 KB per completed run |

No high findings.

---

## 1. Critical: verifies on two threads segfault the interpreter

**Impact.** `Formalizer.formalize` checks every LLM formalization with `z3.parse_smt2_string(...)` with no `ctx=`, so the check runs on Z3's process-global main context. That context is not thread-safe. Two event loops on two threads that verify at the same time corrupt it, and the process dies. No Python exception is raised, so nothing can catch it.

| Threads (one event loop each) × verifies of a 20-step trace | Interpreters that crashed |
|---|---|
| 1 × 40 | 0 / 10 |
| 2 × 10 | 7 / 8 (SIGSEGV) |
| 2 × 20 | 10 / 10 in one run; 7 / 8 in another (6 SIGSEGV, 1 hung and was killed at 30 s) |
| 4 × 5 | 8 / 8 (SIGSEGV) |
| 4 × 10 | 10 / 10 (SIGSEGV or SIGABRT) |
| 2 × 20 and 4 × 10, with the parse given a fresh `z3.Context()` | **0 / 20** |

The faulthandler trace shows two threads inside `Z3_parse_smtlib2_string` called from `formalizer.py:104`.

Precondition: no entry point in this repository runs verifies on two threads today (the CLI runs one loop). But `SolverExecutor` keeps one semaphore per event loop precisely so that it can serve several loops (`solver.py:244-250`). Any threaded host, such as FastAPI sync handlers, a Streamlit callback that verifies, or a worker pool that calls `asyncio.run` per job, crashes the whole process and every run in it. The solver side is safe: `Z3Solver` already uses a fresh `z3.Context` per check.

**Repro.** `test_verifies_on_several_threads_do_not_crash_the_interpreter` runs `verify_in_threads(4, 5)` in two child interpreters and asserts both exit with 0. Today both exit with -11. Script: `audit_load_2 segv`.

**Root cause.** `src/autolab/verification/formalizer.py:104`: `z3.parse_smt2_string(f"{full_decls}\n(assert {out.assertion})")` with no `ctx`. (`tests/fixtures/golden_traces.py:71` does the same, but only in tests.)

**Suggested fix.** Pass `ctx=z3.Context()` to the parse (measured: 0 crashes in 20 runs). A per-thread context also works. Add a test that verifies on two threads in a subprocess. Also consider a lint rule against `z3.*` calls without `ctx=` under `src/`.

---

## 2. Medium: SQLite store fails runs with `database is locked` under concurrent writers

**Impact.** Each `StateStore.append` is its own transaction on an engine built with driver defaults: a 5 s busy timeout, the rollback journal, and no retry. The tracer writes each new entity in its own transaction (about 12 per run). When writers queue for more than 5 s, the write raises `OperationalError: database is locked`. That error propagates out of `_observed` and aborts the graph run. It is neither escalated nor retried.

| Load (full traced graph runs, one store file) | Runs failed | Wall |
|---|---|---|
| 1 store engine, 100 concurrent runs, shared checkpoint file | 1 / 100 | 26.8 s |
| 4 engines (≈ 4 processes), 100 runs, separate checkpoint files | 23 / 100 | 23.1 s |
| 4 engines, 100 runs, shared checkpoint file | 31 / 100 (all from the store) | 22.1 s |
| 8 engines, 200 runs, separate checkpoint files | 138 / 200 | 29.8 s |
| 8 engines, 200 runs, shared checkpoint file | 156 / 200 (137 store, 19 checkpointer) | 32.1 s |

- Store only (no graph), with 8 engines × 120 writers × 8 appends: 6, 11 and 15 failures in three runs. Every failure came after 5.14–5.27 s. Below the threshold the tail is already near the limit: 4 engines × 40 writers gave a max append latency of 4.17 s, and 8 × 80 gave 4.64 s with 0 errors.
- `DATABASE_URL` defaults to `sqlite+aiosqlite:///./autolab.db`. So two or more `autolab run` processes, or one process with many runs, can hit this. Postgres (the production target) was not measured.
- Round 1's race is still fixed: 200 concurrent upserts of one key from 4 engines gave 0 errors and 1 row.

**Repro.** `test_store_append_waits_out_a_concurrent_writer`: another connection holds `BEGIN IMMEDIATE` for 5.5 s, and `append` raises `OperationalError` after 5 s. The test asserts that the append succeeds. Script: `audit_load_2 store`.

**Root cause.** `src/autolab/state/store.py:157` (`create_async_engine(url, echo=echo, future=True)`: no `connect_args={"timeout": ...}`, no WAL pragma, no write serialization). `store.py:241-243` uses one session and commit per entity. `src/autolab/monitoring/tracer.py:32-41` writes the run row, each entity and the event sequentially. `orchestrator/recovery.py:18-22` does not classify `OperationalError` (locked) as transient, so the run aborts.

**Suggested fix.** For SQLite, set `connect_args={"timeout": 30}` and `PRAGMA journal_mode=WAL` on connect. Serialize writes per engine with an `asyncio.Lock`, or write each `record_node` batch in one transaction. Retry `OperationalError` "database is locked" a bounded number of times. Measure Postgres separately.

---

## 3. Medium: `retry-after` overrides the jitter, so rate-limited clients retry in lockstep

**Impact.** SG8 added full jitter, `sleep(max(uniform(0, delay), retry_after))`. A 429 normally carries `retry-after`, and whenever `retry_after ≥ delay` every client sleeps exactly `retry_after`. 50 clients rate-limited together:

| Server header | Attempt 2: earliest, spread | Attempt 3: earliest, spread |
|---|---|---|
| none (jitter only) | 0.009 s, 490.5 ms | 0.071 s, 1337.4 ms |
| `retry-after: 1` (default 0.5 s backoff) | 1.003 s, **7.3 ms** | 2.006 s, **6.2 ms** |
| `retry-after: 1`, 2 s backoff | 1.003 s, 912.6 ms | 2.004 s, 3845.2 ms |

So the case that jitter exists for, many clients hitting one rate limit, produces a synchronized herd at exactly `t + retry-after`. With finding 4's fan-out, that is hundreds of simultaneous requests.

**Repro.** `test_retry_after_waits_are_jittered`: 20 clients with `retry-after: 0.2` retry within 1.6 ms of each other. The test asserts a spread of at least 20 ms. Script: `audit_load_2 retry`.

**Root cause.** `src/autolab/orchestrator/recovery.py:86`: `max(random.uniform(0, delay), retry_after_seconds(exc) or 0.0)`.

**Suggested fix.** Treat `retry-after` as a floor and add jitter on top of it, for example `retry_after + uniform(0, delay)` (or `uniform(retry_after, retry_after + delay)`), still capped by `MAX_RETRY_AFTER_S`.

---

## 4. Medium: faithfulness judges fan out without a bound

**Impact.** SG10 runs each step's judge as a task while later steps are formalized, but nothing limits how many tasks are in flight. When formalization is faster than judging, which also happens when judges are slowed by 429 retries, almost every judge of a long trace is in flight at once.

| Trace / latencies | Peak concurrent judge calls |
|---|---|
| 100 steps, formalize 10 ms, judge 500 ms | 46 |
| 100 steps, formalize 10 ms, judge 2 s | **100** (one per step) |
| 100 steps, formalize 50 ms, judge 50 ms | 1 |
| 20 concurrent 100-step verifies, judge 500 ms | **915** |

Against a rate-limited API this turns one verify into a burst of up to N requests. With finding 3, the retries of that burst land together. Cancellation works: cancelling a verify with 44 judges in flight settled in 0.7 ms with 0 judges and 0 tasks left over.

**Repro.** `test_judges_in_flight_per_verify_are_bounded`: a 100-step trace with a 0.5 s judge reaches 100 in flight. The test asserts at most 10. Script: `audit_load_2 judges`.

**Root cause.** `src/autolab/verification/vericot.py:161`: `judged.append(asyncio.create_task(self._judge(step, formula)))` for every step, with no semaphore.

**Suggested fix.** Wrap `_judge` in a per-verifier `asyncio.Semaphore` sized from a setting (for example 4–8), or keep a sliding window of pending judges. A process-wide LLM concurrency limit in `RetryingLLMClient` would also cover agents and formalization.

---

## 5. Low: a failed judge is noticed only after every step is formalized

**Impact.** A judge that raises (for example a transient error after retries are exhausted) is only observed by the final `gather`, so formalization of every remaining step continues first.

| Trace | Step-0 judge fails at | `verify` raises at | Formalize calls made after the failure |
|---|---|---|---|
| 20 steps, 50 ms per call | 0.10 s | 1.02 s | 17 of 20 |
| 100 steps, 50 ms per call | 0.10 s | 5.11 s | **97 of 100** |

Each wasted call is a full formalize prompt, about 85 K prompt characters in total for 100 steps (round 1), spent on a run that has already failed.

**Repro.** `test_failed_judge_stops_formalization`: on a 30-step trace, 27 formalize calls are made after the failure. The test asserts at most 2.

**Root cause.** `src/autolab/verification/vericot.py:156-162`: the loop never looks at finished judges; `asyncio.gather(*judged)` runs only after the last step is formalized.

**Suggested fix.** Run judges in an `asyncio.TaskGroup` (the first failure cancels the formalization loop), or check `task.done() and task.exception()` for finished judges before each formalize call.

---

## 6. Low: SG7's premise checks pool the whole trace, and the bench does not count them

**Impact.** Since SG7, every `premise` step runs a consistency check against **every** prior VALID step plus the facts, whatever the step cites. The check is reported with reason `assumed`, and `bench_verifier` drops `assumed` from its samples.

- Golden corpus (5 runs): the bench reports 420 samples, but 945 checks ran. Premise checks are 525 of them and 2762 of 5156 solver-ms (54%). Per-step p95 is about the same (8.83 ms reported, 8.19 ms over all checks), but the bench's per-trace "total" understates solver time by more than half.
- Cited chains lost their linear cost: total time at 400 steps went from 1.3 s (round 1) to 3.97 s, and at 800 steps from 2.5 s to 10.9 s. Premise checks are 58% of solver time at 100 steps and 70% at 400. At 400 steps the premise checks have p95 22.0 ms and max **65.0 ms**, over the 50 ms budget. Inference steps have p95 8.4 ms.
- Golden corpus sequentially: 585 ms (round 1) → 1313 ms. A full four-agent graph run makes 24 solver checks and takes 150–190 ms of CPU. 10 and 30 concurrent graph runs on one SQLite checkpoint file went from 0.99 / 2.28 s (round 1) to 2.61 / 8.08 s.
- At the sizes in the brief this is within budget. For bound (payload) traces of 50 and 100 steps, SG7 adds 1.5× the checks (78 vs 51, 153 vs 101) or 2.5× with one circular premise (125 vs 50, 250 vs 100). Wall time per step is 10.0–21.1 ms, and solver p95 over all steps is ≤ 12.9 ms (max 20.4 ms).

**Repro.** `test_bench_times_every_solver_check`: `bench_verifier.run(1)` reports 84 samples while 189 checks ran. Script: `audit_load_2 payload`; `audit_load long`.

**Root cause.** `src/autolab/verification/vericot.py:194` (`pool = [*formalized.values(), *extra_premises, formula]`) and `scripts/bench_verifier.py:25` (`_UNSOLVED` includes `"assumed"`).

**Suggested fix.** Count premise checks in the bench, or give them their own row. For the pool, check a new premise incrementally: keep one `z3.Solver` per trace with the VALID steps asserted and `push`/`check`/`pop` each new premise, instead of re-parsing every prior premise per check. That makes consistency O(1) parses per premise.

---

## 7. Low: `SolverExecutor`'s bound and shutdown are per event loop

**Impact.**
- **Bound.** The docstring says "at most `workers` checks are submitted at once". The semaphore is per loop, so k loops admit k × `workers` checks. With 8 workers and 16 slow checks per loop, the peak pool queue was 4 with 1 loop, 10 with 2 loops and **25** with 4 loops. Those checks wait inside the `ThreadPoolExecutor`, invisible to the semaphore.
- **Shutdown.** `shutdown()` gives three different outcomes. With 4 running and 8 waiting verifies, the 4 running checks *returned* UNGROUNDED/`timeout` ("solver returned unknown"), a verdict indistinguishable from a real timeout. The 8 waiting raised `RuntimeError` (cannot schedule new futures). With two loops, the check queued in the pool from the other loop raised **`CancelledError`** in a task that nobody cancelled. A graph node seeing that treats its run as cancelled.

**Repro.** `test_shutdown_does_not_cancel_callers_on_other_loops`: on a 1-worker executor, two loops each start one slow check, and `shutdown()` gives `['CancelledError', 'timeout']`. The test asserts no `CancelledError`. Script: `audit_load_2 loops shutdown`.

**Root cause.** `src/autolab/verification/solver.py:244-250` (one `asyncio.Semaphore` per loop) and `solver.py:236-242` (`shutdown(cancel_futures=True)` cancels queued pool futures, which `asyncio.wrap_future` turns into `CancelledError` for the waiter). `solver.py:95-97, 142-146` report an interrupted check as `timeout`.

**Suggested fix.** Use one process-wide admission bound (a `threading.BoundedSemaphore` acquired via the pool, or a counter guarded by the existing lock) instead of one per loop. On shutdown, fail queued and interrupted calls with one dedicated exception (for example `SolverShutdown(RuntimeError)`) instead of a cancelled future or a `timeout` verdict.

---

## 8. Low: a reused compiled graph grows by about 66 KB per completed run

**Impact.** `build_graph()` without a checkpointer uses the in-process `MemorySaver`, which keeps every checkpoint of every thread for the life of the graph. Over 200 sequential runs on one compiled graph, the traced heap grew 3.3 / 6.6 / 9.6 / **13.0 MB** at 50/100/150/200 runs, linearly at about 66 KB per run. With a fresh graph per run it stayed at 0.2 MB. A shared `RunTracer` likewise keeps 12 ids per run forever (2400 after 200 runs, small). The CLI builds a graph per run with the SQLite saver, so it is not affected. A long-lived host that reuses one graph (`test_compiled_graph_is_reusable_across_threads` shows reuse is supported) grows without bound.

**Repro.** `test_reused_graph_memory_does_not_grow_per_run`: 15 runs grow the heap by 1.03 MB. The test asserts under 256 KB. Script: `audit_load_2 memory`.

**Root cause.** `src/autolab/orchestrator/graph.py:111` (`checkpointer or new_in_process_saver()`) and `orchestrator/checkpoints.py:144-146` (an unbounded `MemorySaver`). `monitoring/tracer.py:29-30` (`_seen` and `_runs` are never pruned).

**Suggested fix.** Delete a thread's checkpoints once its run reaches END (`checkpointer.delete_thread(thread_id)`), or document that the in-process saver is for single-run use. Drop a run's keys from `RunTracer._seen` when its run ends.

---

## Round-1 resolutions, re-measured

| AUDIT-2 # | Round 1 → resolution claim | Re-measured now | Status |
|---|---|---|---|
| 1 | `getaddrinfo` during 50 slow verifies 3.08 s → 0.01 s; `solver_ms` max at 50 × 1 s and 50 × 5 s ≈ 1011 / 5011 ms | `getaddrinfo` 0.03 s (idle 0.4 ms); `solver_ms` max 1076 ms (50 × 1 s) and 5029 ms (50 × 5 s); wall time for 50 × 5 s is still 35.3 s (queueing; open as stated) | Holds |
| 2 | 0 `IntegrityError`s | 20 concurrent appends of one id: 0 errors; 10 `ensure_run`: 0; 200 upserts of one key from 4 engines: 0 errors, 1 row; 10 and 30 concurrent traced runs sharing a store: 0 errors | Holds (finding 2 is a separate lock-timeout failure) |
| 3 | `(run_id, id)` keys | Covered by the flipped AUDIT-2 test, which passes | Holds |
| 4 | ≤ 3 HTTP attempts, jitter, `retry-after` honoured | Production path (`ClaudeLLMClient` builds its own client) ≤ 3: its test passes. `audit_load retry` still prints 9 because that script injects its own `AsyncAnthropic` with the SDK default `max_retries=2`, a script artifact that SG8 noted as open. Jitter without `retry-after`: spread 164 / 396 ms (20 clients). `retry-after` honoured (attempt 2 at 1.003 s), but it cancels the jitter (finding 3) | Partly holds |
| 5 | 10 steps at 100/200/400 ms per call: 1.17/2.27/4.47 s | 1.17 / 2.27 / 4.48 s (0.56–0.58 × the sum of call latencies) | Holds |
| 6 | next pool job after cancelling 8 verifies 2.83 s → 0.00 s | 0.00 s; 2000 randomly timed cancellations: 1569 cancelled mid-flight, 0 errors, 0 interrupt handles left, next verify all VALID | Holds |
| 7 | declaration lines scanned for 100 / 800 uncited steps: 5 100 / 320 800 | 5 100 / 320 800. 800-step uncited total 18.0 s with max step **71.0 ms**, which is over the 50 ms budget (SG10 recorded 52.1 ms; still open as stated) | Holds, with the remaining superlinearity noted |

## What held up

- **CLAUDE.md "10-step trace in < 30 s".** With faithfulness on, a 10-step trace costs about 11 LLM call latencies: 1.18 s at 100 ms per call, 5.57 s at 500 ms, 11.08 s at 1 s and 22.08 s at 2 s. The requirement holds while the mean structured call takes **≤ 2.7 s** (round 1: ≤ 1.5 s). A 10-step bound payload trace takes 93 ms at zero latency. One formalize call rate-limited twice with `retry-after: 1` adds about 2 s (2.07 s total). Live latency was not measured (no API key; the brief rules it out).
- **Thread leaks.** 1000 `Verifier()` instances on the default executor left 2–3 `z3` threads (process threads 1 → 3). 50 `Verifier`s, each with its own never-shut-down `SolverExecutor(4)`, left 2 `z3` threads 0.5 s after GC. Idle workers exit when their pool is collected. In a separate check, 20 such executors left 0 alive and 0 threads.
- **Per-loop semaphore cleanup.** After 200 `asyncio.run` loops used one executor, 0 semaphore entries were left (weak keys work).
- **Interrupt races.** See the AUDIT-2 #6 row above: 2000 random cancellations, no errors, no leaked handles, correct verdicts afterwards.
- **Judge cancellation.** Cancelling a verify with 44 judges in flight settled in 0.7 ms with 0 judges in flight and 0 leftover tasks.
- **SG7 cost at 50 and 100 steps.** Bound traces of 50–101 steps: wall time per step 10.0–21.1 ms, solver p95 ≤ 12.9 ms, max 20.4 ms. That is within the 50 ms per-step budget (finding 6 covers 400+ steps and the bench).
- **Bench.** `bench_verifier --runs 5`: step p95 8.74 ms, max 15.87 ms (within the 50 ms budget, though without premise checks; finding 6).
- **z3 contexts under one loop.** 20 and 50 concurrent verifies of the golden corpus produced 0 verdict mismatches against the sequential run (724 ms for 50 concurrent, 1313 ms for 60 sequential).
- **Memory of the verifier.** RSS oscillated between 388 and 430 MB over 5000 verifies (347 MB at start), with no monotonic growth.
- **Event-loop stalls.** Verifying 10, 100 and 400 steps stalled the loop for at most 1.6, 8.1 and 7.7 ms. A full graph run with the SQLite checkpointer stalled it for at most 13.8 ms.
- **Concurrent graph runs on one SQLite checkpoint file** (no store): 10 and 30 runs, 0 errors.
