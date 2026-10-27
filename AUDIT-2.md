# AUDIT-2: Performance under load

Auditor: independent (wrote none of the audited code). Base: `origin/claude/sg6-persistence` @ `c1c4d15`.
Machine: 4 CPUs, 16 GB RAM, Linux, Python 3.11, z3 4.16.0, SQLAlchemy 2.0.49, anthropic 0.102.0.
All runs use `FakeLLMClient` (the retry repros use an in-process `httpx.MockTransport`; nothing touches the network).

- Repros: `tests/audit/test_audit_2.py`. Each finding has one or more tests marked `xfail(strict=True, reason="AUDIT-2 #n")` that assert the correct behaviour. Each test takes under 3.1 s, and the file takes about 14 s.
- Measurements: `uv run python -m scripts.audit_load [section ...] [--default-timeout]`. The numbers below come from one full run of that script.

| # | Severity | Title |
|---|---|---|
| 1 | **High** | Z3 checks saturate asyncio's shared default executor |
| 2 | Medium | `StateStore` upserts race: concurrent writes of one id raise `IntegrityError` |
| 3 | Medium | Store primary keys ignore `run_id`: a repeated entity id silently moves rows between runs |
| 4 | Medium | Retry layers multiply (9 HTTP attempts), have no jitter and ignore `retry-after` |
| 5 | Medium | The verifier's LLM calls are fully sequential, so the 10-step < 30 s budget holds only at ≤ 1.5 s per call |
| 6 | Low | Cancelling `verify()` does not stop the Z3 check it started |
| 7 | Low | Each premise carries the whole vocabulary: declaration work is O(N²)–O(N³) per trace |

No critical findings.

---

## 1. High: Z3 checks saturate asyncio's shared default executor

**Impact.** `asyncio.to_thread` runs on the loop's default `ThreadPoolExecutor`, which has `min(32, cpu + 4)` = **8 workers** here. The same pool serves every other `to_thread` and `run_in_executor(None, …)` caller in the process. That includes `loop.getaddrinfo`, which httpx/anyio use to resolve DNS for every Anthropic request.

| Load (pathological NIA check, always runs to its timeout) | Wall time | `solver_ms` min / max |
|---|---|---|
| 8 concurrent verifies, timeout 1 s | 1.03 s | 1016 / 1024 ms |
| 20 concurrent, timeout 1 s | 3.04 s | 1007 / 3016 ms |
| 50 concurrent, timeout 1 s | 7.07 s | 1008 / 6987 ms |
| 50 concurrent, **default timeout 5 s** | **35.06 s** | 5020 / **34931 ms** |

- While 50 slow verifies run, a `getaddrinfo("localhost")` takes **6.00 s**. It takes 0.5 ms when the pool is idle. A no-op `to_thread` job waits 6.03 s. So one run's hard formula stalls the LLM calls and DNS lookups of every other run in the process.
- SG2's guarantee ("UNGROUNDED within `solver_timeout_s` + 1 s") does not hold under load. The deadline starts when a worker picks the check up, not when `verify` asks for it.
- `StepVerdict.solver_ms` is measured around the whole `await asyncio.to_thread(...)`, so it includes time spent waiting in the queue. It reported 34.9 s for a check bounded at 5 s. Bench numbers, metrics and the dashboard would blame Z3 for queueing delay.
- Z3 itself is not the bottleneck. With 50 dedicated threads, 50 one-second checks finish in 1.69 s wall (each check at most 1.12 s), all `timeout`.

**Repro.** `test_slow_checks_do_not_starve_the_default_executor` (no-op `to_thread` waited 0.97 s; asserts < 0.25 s) and `test_timeout_bounds_each_check_under_concurrency` (worst `solver_ms` 1469 ms for a 0.5 s timeout; asserts < 1000 ms). Script: `audit_load pool --default-timeout`, `audit_load threads`.

**Root cause.** `src/autolab/verification/vericot.py:314`: `await asyncio.to_thread(self._solver.check_entailment, ...)` uses the shared default executor. `solver_ms` is timed around the await (`vericot.py:313-315`). The deadline is computed inside the worker (`solver.py:80`).

**Suggested fix.** Give Z3 its own bounded `ThreadPoolExecutor` (sized from a new `Settings.solver_workers`) and call it with `loop.run_in_executor(pool, ...)`. Add an `asyncio.Semaphore` so queued checks wait as coroutines and do not pile up in the pool. Measure `solver_ms` inside the worker, for example by having `check_entailment` return its own elapsed time. Report queue wait separately. Optionally count the deadline from submission time, so the end-to-end bound holds.

---

## 2. Medium: `StateStore` upserts race; concurrent writes of one id raise `IntegrityError`

**Impact.** `append` claims to be idempotent ("rows are upserted by id"), but `session.merge` is a SELECT followed by an INSERT, with no lock or conflict clause.

- 20 concurrent `append(run, same_hypothesis)` calls: **14 raise `IntegrityError`** (UNIQUE constraint failed: hypotheses.id).
- `ensure_run` has the same check-then-insert pattern: 10 concurrent `ensure_run(same_id)` calls give **9 `IntegrityError`s**.
- End to end, concurrent traced graph runs sharing one `StateStore` whose entities share ids (the shared fixtures do) crashed **5 of 10** and **3 of 30** runs with `IntegrityError`. The tracer's exception propagates out of the `_observed` node and the graph aborts.
- The same race applies to two processes resuming or re-recording the same run, which is the case the upsert was added for.

**Repro.** `test_concurrent_upserts_of_one_entity_are_idempotent`, `test_concurrent_ensure_run_is_idempotent`. Script: `audit_load store graph`.

**Root cause.** `src/autolab/state/store.py:204` (`await session.merge(row_cls(**kwargs))`) and `store.py:151-157` (`ensure_run`: `session.get`, then a separate `create_run` in a new session).

**Suggested fix.** Use a real upsert: `sqlalchemy.dialects.sqlite.insert(...).on_conflict_do_update(index_elements=[...])`, with the `postgresql.insert` equivalent chosen by dialect. Use `on_conflict_do_nothing` for `ensure_run`. Alternatively, catch `IntegrityError` and re-read. Add one test per dialect.

---

## 3. Medium: store primary keys ignore `run_id`; a repeated entity id silently moves rows between runs

**Impact.** Every entity table is keyed by the entity's `id` alone. When a second run records an entity with an id already stored (deterministic fixtures, replays, or an LLM that fills the `id` field of the schema it is shown), `merge` *updates* the existing row's `run_id`. Measured: after `append(run_a, H)` and then `append(run_b, H)`, `load_hypotheses(run_a)` returns **`[]`**. Run A's audit trail loses the entity without any error. Under concurrency the same collision produces finding 2's crash instead. `Hypothesis.id` is part of the JSON the Research agent's LLM is asked to produce (`Hypothesis.model_validate_json(raw)`), so this does not need a bug elsewhere to happen. I did not measure how often a live model actually repeats an id.

**Repro.** `test_same_entity_id_in_two_runs_stays_in_both`.

**Root cause.** `src/autolab/state/store.py:59` (`id` is the sole primary key of `_EntityRow`) together with the `merge` at `store.py:204`.

**Suggested fix.** Make the primary key `(run_id, id)`, or add a surrogate key with a unique constraint on `(run_id, id)`, and upsert on that pair. Separately, consider stripping or regenerating LLM-supplied ids at the agent boundary.

---

## 4. Medium: retry layers multiply, have no jitter and ignore `retry-after`

**Impact.**
- **Multiplied attempts.** `ClaudeLLMClient` builds `AsyncAnthropic(api_key=...)` with the SDK's default `max_retries=2`, and `RetryingLLMClient` wraps it with 3 more attempts. Against a server that always answers 429, **one logical call made 9 HTTP requests**. Every verifier formalization and faithfulness call goes through this stack, so a rate-limit episode hits the API 3× harder than either layer intends.
- **No jitter.** 20 clients that fail together retry together: attempt 2 at 0.201 s with a **0.20 ms** spread, attempt 3 at 0.602 s with a 0.18 ms spread. That is a synchronized thundering herd against a rate limiter.
- **`retry-after` ignored.** For a `RateLimitError` carrying `retry-after: 1`, the outer layer retried after its own 0.05 s backoff. With the defaults it waits 0.5 s and then 1.0 s, whatever the server asked for. The whole outer budget (about 1.5 s) is shorter than typical rate-limit windows, so the error escapes the graph (as SG4 noted) after hammering the API.

**Repro.** `test_one_logical_call_makes_at_most_three_http_attempts` (9 > 3), `test_concurrent_retries_are_jittered` (spread about 0.16 ms; asserts ≥ 10 ms), `test_rate_limit_retry_after_is_honoured` (waited 0.05 s; asserts ≥ 1 s). Script: `audit_load retry`.

**Root cause.** `src/autolab/verification/llm.py:187` (SDK default `max_retries`), `src/autolab/orchestrator/recovery.py:44-58` (`with_retries`: fixed `delay *= backoff_multiplier`, no jitter, no `retry-after`), and `recovery.py:88-93`.

**Suggested fix.** Keep one retry layer. Either build `AsyncAnthropic(max_retries=0)` and let `RetryingLLMClient` own retries, or drop the outer layer for live clients. Use full jitter (`sleep(random.uniform(0, delay))`). For `anthropic.APIStatusError`, honour `retry-after` / `retry-after-ms`, capped at a configured maximum. Make the attempt count and cap settings.

---

## 5. Medium: the verifier's LLM calls are fully sequential; the 10-step < 30 s budget holds only at ≤ 1.5 s per call

**Impact.** With zero LLM latency a 10-step trace verifies in **23 ms**, far under CLAUDE.md's 30 s. With the faithfulness check (on by default for a live client), each step makes two LLM calls, formalize and then judge, and nothing overlaps.

| Simulated latency per LLM call | 10-step wall time | Calls | Wall ÷ sum of call latencies |
|---|---|---|---|
| 100 ms | 2.03 s | 20 | 1.02 |
| 200 ms | 4.04 s | 20 | 1.01 |
| 400 ms | 8.05 s | 20 | 1.01 |

Wall time is exactly 20 × per-call latency, so the 30 s requirement holds only if the mean structured-output call takes **≤ 1.5 s**. The formalize prompt also grows with the vocabulary (finding 7), so later calls are longer. I did not measure live latency (no API key, and the brief rules it out). This finding is the budget arithmetic plus the measured lack of overlap.

**Repro.** `test_ten_step_trace_overlaps_its_llm_calls`: 3.05 s for 20 × 0.15 s; it asserts < 0.6 × the sum. Script: `audit_load latency`.

**Root cause.** `src/autolab/verification/vericot.py:264-271`: steps are awaited one at a time. `vericot.py:296-299`: the faithfulness judge is awaited before the step's solver check and before the next step is formalized. The judge does not gate the vocabulary, because `Formalizer.formalize` commits the vocabulary before returning (`formalizer.py:104`). Its result only decides whether the formula joins `formalized`.

**Suggested fix.** Pipeline the work. Formalize steps sequentially, since the vocabulary requires it, but start each step's faithfulness judge as a task and await it only when a later step needs that formula as a premise. Run independent solver checks concurrently. Formalization must stay sequential, so this gives about N + 1 latency rounds instead of 2N, which allows about 2.7 s per call at 10 steps. Add a latency regression test that uses a delayed fake.

---

## 6. Low: cancelling `verify()` does not stop the Z3 check it started

**Impact.** I started 8 verifies (one per pool worker) with a 3 s solver timeout and cancelled them at 0.2 s. The next `to_thread` job still waited **2.80 s**. The worker threads keep solving until Z3's own timeout, so a cancelled or abandoned run (client disconnect, `asyncio.wait_for`, shutdown) holds pool capacity for up to `solver_timeout_s` and makes finding 1 worse.

**Repro.** `test_cancelled_verify_releases_its_worker_thread` (waited about 2.8 s; asserts < 0.5 s). Script: `audit_load cancel`.

**Root cause.** `src/autolab/verification/vericot.py:314`: cancelling the awaiting task does not reach the thread. `solver.py:81-113` keeps the `z3.Context` local, so nothing can call `ctx.interrupt()`.

**Suggested fix.** Have `check_entailment` register its `z3.Context` in a handle the caller holds. On `CancelledError` in `_verify_step`, call `ctx.interrupt()` (Z3 supports this from another thread), then re-raise.

---

## 7. Low: each premise carries the whole vocabulary, so declaration work is O(N²)–O(N³) per trace

**Impact.** `SmtFormula.declarations` holds the *cumulative* vocabulary at the time the step was formalized. A step's solver document is therefore assembled by rescanning every premise's full declaration block.

| Steps | Uncited: total / max step / decl lines scanned | Cited: total / max step / decl lines scanned | Formalize prompt chars (sum) |
|---|---|---|---|
| 10 | 50 ms / 16.9 / 64 | 25 ms / 5.6 / 38 | 2.9 K |
| 50 | 167 ms / 8.6 / 5.8 K | 151 ms / 7.6 / 948 | 27 K |
| 100 | 356 ms / 9.3 / 44 K | 302 ms / 10.1 / 3.8 K | 85 K |
| 200 | 1.0 s / 19.9 / 343 K | 0.59 s / 11.3 / 15 K | 295 K |
| 400 | 3.2 s / 37.1 / 2.7 M | 1.3 s / 16.4 / 60 K | 1.1 M |
| 800 | 11.7 s / **78.5 ms** / 21.5 M | 2.5 s / 13.1 / 240 K | 4.3 M |

At the sizes in the brief (50 and 100 steps) this is harmless: 0.17 s and 0.36 s, with every step under the 50 ms budget. The growth is real, though. Uncited traces scale about ×3.5 per doubling of steps, the scanned lines grow cubically, and the last steps of an 800-step trace exceed the 50 ms per-step budget. The formalize prompt resends the whole vocabulary on every step, so prompt tokens grow quadratically. With a live LLM, 100 steps send about 85 K characters (roughly 21 K tokens) of prompt for formalization alone.

**Repro.** `test_solver_document_work_is_linear_in_vocabulary`: the last check of a 100-step uncited trace scans 2549 declaration lines for 51 symbols; it asserts ≤ 4 × symbols. Script: `audit_load long`.

**Root cause.** `src/autolab/verification/formalizer.py:98,105` (each formula stores `candidate.smt()`, the full vocabulary), `solver.py:146-152` (`_build_smt2` rescans every chunk's declarations) and `premise_grounder.py:46-51` (dedup keys hash those large strings).

**Suggested fix.** Store only the step's *own* new declarations on `SmtFormula`, and pass the trace's vocabulary once to `check_entailment` (or emit only the symbols the premises and the claim actually use). For long traces, send the formalizer only the symbols relevant to the step, or a compact symbol table.

---

## What held up

- **z3.Context safety under concurrency.** 20 and 50 concurrent verifies of the 60 golden traces produced verdicts and reasons identical to the sequential run (0 mismatches). 50 concurrent verifies finished in 354 ms, against 585 ms for all 60 sequentially. In 30 rounds of 50 concurrent verifies during the probes, no Z3 errors occurred.
- **Memory.** RSS rose from 258 MB to 318 MB over the first 1000 verifies (allocator warm-up), then stayed flat: 319 MB at 2000, 3000, 4000 and 5000 verifies. 20 000 bare solver checks and 20 000 main-context parses showed no growth beyond warm-up. No leak found.
- **Shared solver deadline (SG2's open item).** With the consistency check slowed to 0.6 s and a 1 s budget, the entailment check got about 0.4 s and the whole call returned `timeout` in 1.00 s. With a 2 s budget and 0.5 s used, the second check got 1.49 s. Regression test: `test_both_checks_share_one_deadline` (passes).
- **Z3 timeout under oversubscription.** 50 one-second checks on 50 threads (4 CPUs) all returned `timeout`, with each check taking at most 1.12 s.
- **Event-loop blocking.** The longest loop stall while verifying 10-, 100- and 400-step traces was 0.8, 2.1 and 8.1 ms. A full traced graph run with the SQLite checkpointer stalled the loop for at most 22.9 ms. The formalizer's on-loop parse check cost 24 ms in total over a 100-step trace (about 0.24 ms per step). Checkpoint serialization (also on the loop) took 1.4 ms for 4 × 100-step traces (131 KB) and 7.9 ms for 40 traces (1.3 MB).
- **Concurrent graph runs on one SQLite checkpoint file.** 10 and 30 concurrent full runs, each with its own `AsyncSqliteSaver` on the same file (WAL mode), completed with 0 errors in 0.99 s and 2.28 s. 30 runs sharing one saver also had 0 errors. Store failures appear only when a shared `StateStore` is added (findings 2 and 3).
- **Store throughput.** A sequential `append` takes 5.4 ms (one transaction per entity), which is fine at a few entities per node.
- **10-step requirement with fakes.** 23 ms for a 10-step trace and 0.36 s for a 100-step one. Finding 5 covers the live-latency risk.
