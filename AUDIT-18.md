# AUDIT-18: Performance under load, round 5

Auditor: independent (wrote none of the audited code or the earlier audits). Base: `origin/claude/sg22-solver-fairness` @ `603e452` (SG20–SG23 merged).
Machine: 4 CPUs, 16 GB, Linux, Python 3.11.17, z3 4.16.0. Defaults here: `SOLVER_WORKERS` = 2, `SOLVER_SLOW_WORKERS` = 8.
All runs use `FakeLLMClient`, subclasses of it, or stubs. Nothing touches the network and no real LLM is used. No production code was changed.

**Load caveat.** Other agents' suites ran on this machine for the first half of the audit (load average 2.3–10 on 4 CPUs); the second half was quieter (0.9–2.9). `uptime` was read before each timing run and the load is given where it matters. Finding 2 depends on load in its natural form (it is a race), so its repro forces the interleaving instead of waiting for it.

- Repros: `tests/audit/test_audit_18.py`, 11 tests. Each is marked `xfail(strict=True, reason="AUDIT-18 #n")` and asserts the correct behaviour; wall-clock ones carry `perf` and take 0.8–6.3 s. With `--runxfail` all 11 failed on their assertion in 10 of 10 runs at load 0.9–2.0 (37–39 s per run); an earlier set of 10 runs of the first 9 at load 2.3–3.5 also failed 10 of 10. Ordering uses events and waits on conditions, not sleeps.
- Measurements: `uv run python -m scripts.audit_load_5 [section ...]`. Sections: `handoff`, `cap`, `oversub`, `cancel`, `threads`, `phases`, `guard`, `real`, `probe`, `tokens`, `figures`, `store`, `loop`, `memory`. Earlier rounds were re-run with `scripts.bench_verifier --runs 20`, `scripts.audit_load_4 overrun starve interrupt pool loop memory bench`, `scripts.audit_load pool cancel latency long --default-timeout`, `scripts.audit_load_3 concurrent threads`, `scripts.audit_load_2 segv store`.
- `make lint typecheck test`: see the end of this file.

| # | Severity | Title |
|---|---|---|
| 1 | **Critical** | SG20's applied-builtin probe is exponential in a declaration's arity and runs on the event loop: one 16-argument function blocks the process for 21 s, 20 arguments build 0.8 GB of probe strings, 26 cannot finish |
| 2 | **High** | A stale `ThreadPoolExecutor` idle permit stops SG22's pool from growing: checks that gave up their slot still hold the only thread, and an easy 10-step verify waits 150 s (5× the 30 s budget) |
| 3 | **High** | Real-valued premise pools ignore the solver timeout (up to 20×) and cancels (worker held 9.4 s), and cost 9–11× their Int twins |
| 4 | Medium | Above `slow_workers`, hard checks keep their slots: 16 hard runs make an easy 10-step verify take 45 s (1.5× budget) |
| 5 | Medium | The slow lane runs CPU-bound checks far beyond the CPUs, so wall-clock timeouts change verdicts under load (10 of 10 decidable checks time out on a 1-CPU container) |
| 6 | Medium | SG23 hardened the store but not the LangGraph checkpointer: a checkpoint write fails with `database is locked` after 5 s |
| 7 | Low | `_SETUP_SHARE` returns `timeout` for premise checks whose entailment step needs milliseconds (Real pools of 1200–1300 premises at the default timeout) |
| 8 | Low | SG21's figure scan is quadratic in the number of tokens: 1.2 s for an 8 000-number protocol |
| 9 | Low | SG20's earlier-use rule re-tokenizes the whole vocabulary for every step (63–238× the text; 56% of formalize time at 1600 steps) |

One critical, two high, three medium and three low findings.

---

## 1. Critical: the applied-builtin probe is exponential and blocks the event loop

**Impact.** SG20 added `_applied_builtin`, which parses a declared function applied to its argument sorts without its declaration, to see whether Z3 already resolves it. Each `Int` or `Real` argument is tried both ways, so a function with k numeric arguments is probed under all 2^k sort combinations, and the list of 2^k probe strings is built before the first parse. It runs inside `Vocabulary.merged`, called by `Formalizer.formalize` on the event loop, so nothing else in the process runs meanwhile. A cancel or `asyncio.wait_for` cannot fire, and pytest-timeout's signal method cannot either. The input is one LLM reply: `(declare-fun f (Int Int ... Int) Bool)` is about 6 characters per argument.

`audit_load_5 probe`, `probemem` (one declaration, uncached):

| Int arguments | Parses | Time on the calling thread | Peak RSS before the first applied parse |
|---|---|---|---|
| 2 | 6 | 14 ms | |
| 8 | 258 | 56–75 ms | |
| 12 | 4 098 | 0.93–1.06 s (event-loop stall measured: 1 013 ms) | 108 MB |
| 14 | 16 386 | 4.2–4.6 s | |
| 16 | 65 538 | 21.2 s | 142 MB |
| 18 | | ≈ 85 s (extrapolated) | 264 MB, 2.1 s just to build the list |
| 20 | | ≈ 6 min | 803 MB, 10.3 s to build |
| 26 | | ≈ 6 h | ≈ 50 GB (4× per 2 arguments): the process is killed first |

- A run hangs (16–20 arguments) or is OOM-killed (≥ 24 on this 16 GB machine), taking every other run in the process with it. That is a hang or a crash on a reachable path: the formalizer's input is untrusted LLM output (CLAUDE.md §4), and nothing bounds a declaration's arity.
- The cache does not help a live run: the key includes every `declare-sort` line in the vocabulary, so adding one sort re-runs the probe (12 arguments with one sort declared: 1.19 s again).

**Repro.** `test_formalizing_a_wide_function_makes_a_bounded_number_of_parses` formalizes one step whose reply declares a 16-argument Int function and counts `Z3_parse_smtlib2_string` calls, stopping at 1 000. It asserts at most 1 000 parses; today the count reaches the cap (65 538 needed). 0.9 s. Script: `audit_load_5 probe`.

**Root cause.** `src/autolab/verification/formalizer.py:279-282` (`itertools.product(*(_COERCIBLE.get(a, (a,)) for a in args))`, materialized into `uses` before any parse), called from `formalizer.py:75` inside `Vocabulary.merged`, which `Formalizer.formalize` runs synchronously on the event loop (`formalizer.py:127`). The `lru_cache` key at `formalizer.py:259-260` includes `sort_decls`.

**Suggested fix.**
- Probe linearly: one probe with every numeric argument `Int`, one with every one `Real`, and one per position with only that argument flipped (2k + 2 parses), or reject declarations with more than a small number of arguments (for example 8) before probing. Generate probes lazily.
- Cap the arity in `_decl_name`/`merged` regardless (fail closed with `FormalizationError`), since no realistic step needs a 16-argument function.
- Key the cache on the declaration and only the sorts it mentions.
- Longer term, run `formalize`'s Z3 parses off the event loop (the solver executor), so a slow parse can be timed out.

---

## 2. High: a stale idle permit stops SG22's pool from growing

**Impact.** SG22 hands a slot over by raising the private `ThreadPoolExecutor._max_workers` by one, expecting the next `submit` to start a thread. CPython's `_adjust_thread_count` first does `_idle_semaphore.acquire(timeout=0)` and, if that succeeds, starts no thread. A worker releases that semaphore after every job, but a job it takes from the queue never acquires it. So whenever a job is queued while every thread is busy and the pool is at `_max_workers`, the semaphore ends up one above the real number of idle threads. That happens in the normal flow: when a check finishes, its future is resolved before the worker marks itself idle, and the event loop can submit the caller's next check in between; it also happens whenever `_max_workers` has just dropped back after a slow check ended.

With such a stale permit, the next `submit` after `_to_slow` takes the permit instead of a thread. The check is queued behind the slow check on the only thread, holding the slot, and since it never attaches a Z3 context it can never go slow itself (`_to_slow` requires `interrupt.attached()`). When the slow check's thread finishes, it releases the permit again before taking the queued job, so the stale permit survives and the pool stays at one thread: every check of every run then runs one after another, which is the pre-SG22 starvation of AUDIT-14 #2, only worse.

10-step easy verify beside hard runs whose checks run to the timeout (`audit_load_5 handoff`, `cap` and the trial scripts behind them; the pool was warmed by one verify):

| Workers | Hard runs | Check timeout | 10-step verify | Load |
|---|---|---|---|---|
| 1 | 3 | 0.5 s | **15.0 s in 7 of 8 trials** (0.36 s in 1) | 2.8–5.0 |
| 1 | 3 | **5 s (default)** | **150.0 s in 3 of 3 trials** (5× budget) | 4.4–5.4 |
| 1 | 7 | 1 / 2 / 5 s | 70 / 140 / **350 s** (11.7× budget) | 1–10 |
| 1 | 8 | 5 s | **400 s** (13× budget) | 10 |
| 2 | 4 | 5 s | 5.1–5.2 s in 2 of 3 trials (one full hard check) | 4.4–5.4 |
| 1 | 3 | 0.5 s / 5 s | 0.16–0.18 s (stale permit seen in 1 of 10 trials) | 1.3 |
| 2 | 2 | 0.5 s | 0.05–0.07 s (stale permit in 4 of 10, harmless: a thread was idle) | 1.3 |

- In every slow trial the pool had a work item queued and `_threads` stayed at 1 (or 3 with `_max_workers` 4): the pool refused to grow. Each 10-step verify then took (hard runs) × 10 × timeout: 3 × 10 × 5 s = 150 s.
- How often the race is won depends on scheduling: under load 3–5 it happened in most trials, on a quieter host (load 1.3) rarely. A 1-CPU container is the worst case, and it is where SG22 was most needed.
- **The SG22 regression test is flaky.** `test_audit_14.py::test_hard_runs_do_not_starve_an_easy_verify` (2 workers, 2 hard runs, bound 0.5 s) failed 5 of 10 runs at load 3.9–4.4 (1.05 s once), and 4 of 10 trials of the same scenario took 0.47–1.93 s. Every slow trial had a check queued in the pool with 3 threads against `_max_workers` 4. It passed in the quiet `audit_load_4 starve` run (0.06–0.1 s). So `make test` can fail under load on a test that SG22 flipped.
- Forced, the effect is exact: one stale permit, then a 3 s hard check that gives up its slot. The easy check took 2.96–2.97 s (5 of 5) with 1 pool thread; without the stale permit 0.017 s with 2 threads.

**Repro.** `test_an_easy_check_never_queues_behind_a_check_that_gave_up_its_slot` forces the interleaving the event loop produces on its own: a wrapper around the pool's `_idle_semaphore` (`scripts/audit_load_5.py`, `_GatedIdle`) holds the worker's "idle" release after one trivial job until the next job's `submit` has returned. Then a 3 s hard check takes the slot and goes slow, and the test asserts that an easy check finishes in under 1 s. Today it takes 2.96 s. 3.0 s. Script: `audit_load_5 handoff cap`.

**Root cause.** `src/autolab/verification/solver.py:324-332` (`_to_slow` raises `self._pool._max_workers` and assumes the next `submit` starts a thread) and `solver.py:316-320` (`_max_workers` drops back while the pool keeps every thread it ever started); CPython `concurrent/futures/thread.py` `_adjust_thread_count` (`if self._idle_semaphore.acquire(timeout=0): return`) and `_worker` (releases the semaphore after each job, never acquires it). `solver.py:327` (`not interrupt.attached()`): a queued check can never go slow.

**Suggested fix.**
- Do not resize a `ThreadPoolExecutor`. Run slow checks on threads the executor owns: for example a fixed pool of `workers + slow_workers` threads with admission done only by SolverExecutor's own counters (a check that goes slow releases its slot; the pool never needs to grow), or a dedicated thread per slow check.
- If `ThreadPoolExecutor` stays, size it once at `workers + slow_workers`, so a spare thread always exists when a slot is handed over.
- Re-run AUDIT-14 #2's repro 10 times under a parallel load as part of the fix's verification.

---

## 3. High: Real-valued premise pools ignore the timeout and cancels

**Impact.** A step's premise pool is every prior VALID step (all of them for a `premise` step, `vericot.py:194`; every uncited one for other steps). On Real-valued chains of a few hundred premises or more, Z3's consistency check becomes superlinear and runs in a phase that polls neither its timeout nor `Context.interrupt()`. Int and Boolean pools of the same shape stay bounded. Real values are what the Design and Analysis facts are made of (`design_facts`, `measurement_facts`).

`audit_load_5 phases`, consistency check of `real_chain(n)` (n − 1 premises `r_i > r_{i+1} + 1.0`) against `lia_chain(n)` and a Boolean chain:

| Premises | Real | Int | Bool |
|---|---|---|---|
| 200 | 14 ms | 10–12 ms | 0.8 ms |
| 400 | 80–92 ms | 31–39 ms | 1.2 ms |
| 800 | 0.75–1.16 s | 0.11–0.15 s | 1.9 ms |
| 1000 | 2.1–2.4 s | 0.18–0.23 s (9.4–12×) | |
| 1600 | **8.9–10.4 s** | 0.43–0.50 s | 3.4 ms |

Timeouts and cancels (`audit_load_5 cancel`, load 1.3):

| Pool | Timeout | Result |
|---|---|---|
| Real 1600 | 0.5 s | `timeout` after **10.27 s (20.6×)** |
| Real 1600 | 1.0 s | `timeout` after 10.29 s (10.3×) |
| Real 1600 | 5.0 s (default) | `timeout` after 9.80 s (2.0×); a 1600-step Real trace can assume no late premise |
| Real 1000 | 0.5 s | `timeout` after 2.31–2.36 s (4.7×) |
| Int 1600 | 0.5 / 1.0 s | `timeout` after 0.51 / 1.03 s (AUDIT-14 #1 holds) |
| Real 1600, cancelled at 0.4 s | 30 s | worker free after **9.41 s**, 1 785 interrupts sent |
| Real 1000, cancelled at 0.3 s | 30 s | worker free after 1.86 s, 351 interrupts |
| Int / Bool 1600, cancelled at 0.4 s | 30 s | free after 0.01 / 0.00 s |

- Verifier-level, uncited Real trace (`audit_load_5 real`): step p95 7.5 / 13.2 / 28.1 / **88.4 ms** at 50 / 100 / 200 / 400 steps (Int: 15.8 / 14.1 / 22.5 / 44.9 ms); 125 of 400 steps over 50 ms. At 800 steps the premise checks take about 1 s each (20× the step budget).
- It is not SG19's single `from_string` load: asserting item by item, `SimpleSolver` and `SolverFor("QF_LRA")` all overrun the same way (2.18 / 1.33 / 0.92 s against 0.5 s on 1000 premises). Added one at a time to one incremental solver, with a check after each, 800 Real premises took 0.46 s in total and 0.6 ms for the last check (Int: 0.31 s and 0.4 ms; scratch measurement, not in the script).
- SG22's slow lane keeps an overrunning check from blocking other runs, but `shutdown()` joins it (9 s), and with more than `slow_workers` of them finding 4 applies.
- Realism: trace length is bounded by one LLM reply (`max_tokens = 4096`), so the agents produce tens of steps today, not hundreds. The pools also include `context.facts`, and the timeout and cancel failures are the AUDIT-2 #6 / AUDIT-14 #1 guarantees, which no longer hold on this input class.

**Repro.**
- `test_a_real_premise_check_returns_within_its_timeout`: 1000-premise Real pool, 0.5 s timeout, asserts wall ≤ 1.5× the timeout. Today 4.6–4.7×. 2.3 s.
- `test_cancelling_a_real_premise_check_frees_its_worker_promptly`: same pool, 30 s timeout, cancelled 0.3 s in, asserts the worker is free within 0.5 s. Today 1.86 s. 2.2 s.
- `test_a_real_premise_pool_costs_no_more_than_its_int_twin`: the same check over Real against Int, best of two, interleaved; asserts at most 4×. Today 9.4–12×. 5.4 s.

Script: `audit_load_5 phases cancel real`.

**Root cause.** `src/autolab/verification/vericot.py:192-195` and `:200-202` (every check re-asserts the whole pool from scratch in a fresh context; AUDIT-6 #6 noted the pooling), together with Z3's arithmetic core on chained strict Real bounds, which does not poll the resource limit that both `timeout` and `interrupt()` set. `solver.py:142-144` sets the timeout correctly; `solver.py:265-269` (the 5 ms re-send) cannot help while Z3 is not polling.

**Suggested fix.**
- Check a trace incrementally: one solver per verify, premises asserted as they become VALID, each step checked under `push`/`pop`. Measured above, this turns 1 s checks into sub-millisecond ones for Real and Int alike, and it is also the fix AUDIT-6 #6 and AUDIT-2 #7 pointed at.
- Bound what Z3 cannot: run checks in worker processes that can be killed at `timeout + grace` (AUDIT-10 #1's alternative), or cap a pool's size and return `timeout` for larger ones.
- Add Real pools to `bench_verifier` and the latency tests; today they cover Boolean chains and Int pools only.

---

## 4. Medium: above `slow_workers`, hard checks keep their slots

**Impact.** SG22 lets at most `slow_workers` checks run beside the slots (PLAN, SG22 Open). Beyond that, a hard check keeps its slot for its whole timeout, and callers wait in FIFO order behind the hard runs, which re-queue as soon as their check ends. An easy verify waits roughly (queued hard checks) × (time to the next slow-check end) per step.

`audit_load_5 cap` (5 s timeout, 10-step easy verify, quiet machine):

| Workers / slow_workers | Hard runs | 10-step verify |
|---|---|---|
| 2 / 8 (4 CPUs) | 8 | 0.36 s |
| 2 / 8 | 10 | 10.3 s |
| 2 / 8 | 12 | 25.1–25.3 s |
| 2 / 8 | 16 | **45.4–46.1 s (1.5× budget)** |
| 1 / 5 (1 CPU) | 6 | 15.1–15.2 s |

Under 2× the budget at these sizes, so Medium; it grows linearly with the number of hard runs. Some of these trials also had a stale permit (finding 2: pool queue 1–2), which makes it worse.

**Repro.** `test_hard_runs_above_the_slow_cap_do_not_starve_an_easy_verify`: 1 worker, `slow_workers = 1`, 3 hard runs at 0.5 s; asserts the easy verify takes under 0.5 s. Today 5.2 s. 5.5 s. Script: `audit_load_5 cap`.

**Root cause.** `src/autolab/verification/solver.py:307-311` (a check at the cap polls and keeps its slot) and `solver.py:342-348` (one FIFO semaphore per loop, with no notion of runs).

**Suggested fix.** Admit per run, not per check: give each verify at most one slot while others wait (round robin), or reserve one slot for checks that have not yet run longer than `_SLICE_S`. Either keeps easy runs moving no matter how many hard runs there are.

---

## 5. Medium: the slow lane runs CPU-bound checks far beyond the CPUs

**Impact.** A slow check waits on Z3, not on the GIL (SG22), but it still needs a CPU. `SOLVER_SLOW_WORKERS = min(32, os.cpu_count() + 4)` sizes the lane like an I/O pool, and `os.cpu_count()` reports the host's CPUs, not the container's quota or the process's affinity. Up to `workers + slow_workers` checks then share the CPUs, every one of them slows down by that factor, and since timeouts are wall-clock, checks that decide alone time out together.

`audit_load_5 oversub`: a child pinned to one CPU, `lia_chain(2000)` checks (alone: `not_entailed` in 1.3–1.6 s).

| `os.cpu_count()` | Pool | Concurrent checks, timeout | Result |
|---|---|---|---|
| 4 (1-CPU container on a 4-CPU host) | 2 + 8 slow (default) | 10, 5 s | **`timeout` 10 of 10** in 5.5 s |
| 4 | 2 + 0 slow | 10, 5 s | `not_entailed` 10 of 10 in 12.7 s |
| 1 | 1 + 5 slow (default) | 4, 3 s | `not_entailed` 4 of 4 in 4.9 s (4 × the alone time: the checks most likely ran one at a time, finding 2) |

- So whether a step is VALID depends on how many other runs share the machine. A run that would pass alone retries or escalates to a human under load. Before SG22 the same load queued (finding 4 in AUDIT-14) but kept its verdicts.
- On this 4-CPU host without pinning, 10 concurrent checks run on 4 CPUs (2.5× slowdown). The pool keeps the extra threads (`audit_load_5 threads`: 10 pool threads after a burst with 8 slow, 20 with 32 slow; they never exit, but idle threads cost no measurable RSS).

**Repro.** `test_concurrent_checks_reach_the_verdict_they_reach_alone`: the child above with `os.cpu_count()` = 4 and 10 `lia_chain(1600)` checks at 3 s; asserts all 10 reach the verdict one reaches alone (`not_entailed`). Today all 10 return `timeout`. 6.3 s. Script: `audit_load_5 oversub`.

**Root cause.** `src/autolab/verification/solver.py:46` (`SOLVER_SLOW_WORKERS` from `os.cpu_count()`), `solver.py:327-331` (a slow check is admitted whenever the count allows), and `solver.py:142`/`:160` (wall-clock `timeout`).

**Suggested fix.** Bound checks with Z3's deterministic resource limit (`rlimit`) and keep the wall-clock timeout only as a backstop well above it, so CPU sharing changes latency but not verdicts. Size the lane from `len(os.sched_getaffinity(0))` (and the cgroup quota where available), not `os.cpu_count()`.

---

## 6. Medium: the LangGraph checkpointer gives up on a lock after 5 s

**Impact.** SG23 gave the store's SQLite engine a 30 s busy timeout and WAL (`SQLITE_BUSY_TIMEOUT_S`). The checkpoint file is opened separately by `sqlite_saver` with `aiosqlite.connect(path)`: the default 5 s timeout and the rollback journal. Concurrent CLI runs share `autolab_checkpoints.db` by default (`cli.py:19`). A checkpoint write that waits more than 5 s for another writer raises `OperationalError: database is locked` and fails the run (it can be resumed from its previous checkpoint, so nothing is lost, as in AUDIT-6 #2).

- One writer holding the checkpoint file's lock: the next `aput` fails after 5.01–5.04 s (`audit_load_5 store`). The store would have waited 30 s.
- `audit_load_2 store` (100 concurrent traced runs, 1 store engine, one shared checkpoint file): 2 of 100 runs failed, both in the checkpointer (load 6.7); 0 store failures in all five configurations (up to 8 engines × 25 runs), against 156 of 200 failures before SG23.

**Repro.** `test_a_checkpoint_write_waits_out_a_lock_like_the_store_does` holds `BEGIN EXCLUSIVE` on the checkpoint file from a second connection until the write returns (at most 8 s) and asserts the write succeeds. Today it fails after 5.04 s. 5.1 s. Script: `audit_load_5 store`.

**Root cause.** `src/autolab/state/checkpointer.py:82` (`aiosqlite.connect(str(path))`: 5 s timeout, no WAL), against `src/autolab/state/store.py:50`, `:154`.

**Suggested fix.** Open the checkpointer with `aiosqlite.connect(path, timeout=SQLITE_BUSY_TIMEOUT_S)` and `PRAGMA journal_mode=WAL`, and add the concurrent-runs test for the checkpointer that SG23 added for the store.

---

## 7. Low: `_SETUP_SHARE` skips entailment checks that need milliseconds

**Impact.** SG22 does not start the entailment check when less than half of the consistency check's time is left. For a premise step the entailment step is `claim! = (not true)`, which Z3 decides in 0.3–8.7 ms on every pool measured (`audit_load_5 phases`, up to 1600 premises), yet the guard returns UNGROUNDED/`timeout` whenever the consistency check used more than about two thirds of the timeout. The premise is then not assumed and drops out of every later step's pool.

- `audit_load_5 guard`, Real premise pools at the default 5 s: 1100 premises VALID after 2.65 s; **1200 and 1300 premises UNGROUNDED** ("too little time left for the entailment check") after 4.24 / 4.68 s, while the same checks with 30 s are VALID and their entailment step takes ~8 ms.
- Under CPU sharing (finding 5) consistency checks stretch, so the window is hit more often.
- The guard does what it was built for: with the deadline a quarter of the way into the entailment check, overruns were 0.91–1.14× on Boolean, Int and Real pools of 50–1600 premises.

**Repro.** `test_a_premise_check_finishes_when_its_entailment_step_needs_no_time` uses a fake clock in `solver.py`: the consistency check of a 10-premise Boolean pool "takes" 7 of 10 s. It asserts the premise check is VALID; today it is UNGROUNDED. No wall-clock dependence. Script: `audit_load_5 guard phases`.

**Root cause.** `src/autolab/verification/solver.py:153-155` (the guard scales with the consistency check's time, not with the work the entailment step adds).

**Suggested fix.** Skip the guard when the claim is `true` (or a literal), or base it on the pool's size rather than on elapsed time; better, remove the need for it with finding 3's incremental checking.

---

## 8. Low: the figure scan is quadratic

**Impact.** `protocol_figures` slices the rest of the token list for every number it meets (`toks[i + 1 :]`), so a protocol with many numbers costs O(numbers × tokens), on the event loop in `DesignAgent.__call__`.

| Numbers in the protocol | Characters | Scan |
|---|---|---|
| 1 000 | 9 000 | 11 ms |
| 2 000 | 18 000 | 38–43 ms |
| 4 000 | 36 000 | 218 ms |
| 8 000 | 72 000 | 1.19–1.28 s |
| 16 000 bare numbers | 46 000 | 1.8 s |
| 64 000 bare numbers | 185 000 | 64 s |

At the default `max_tokens = 4096` a protocol is at most about 16 KB (well under 0.1 s), so this is Low. A larger `max_tokens` or a long parameter list raises it. 1 000 parameters through `design_figures` stay cheap (each text is scanned on its own).

**Repro.** `test_the_figure_scan_is_linear_in_the_protocol` asserts that 4× the numbers costs at most 8×; today 27–31×. 4.4 s. Script: `audit_load_5 figures`.

**Root cause.** `src/autolab/agents/common.py:206-213` (`_words_after` iterates `toks[i + 1 :]`, a copy of the rest of the list), called from `common.py:187`.

**Suggested fix.** Iterate indices (`for j in range(i + 1, len(toks))`) or `itertools.islice`; the loop already stops after `_WINDOW` words.

---

## 9. Low: the earlier-use rule re-tokenizes the vocabulary for every step

**Impact.** `Formalizer.formalize` builds `used = self._used | _tokens(self.vocabulary.smt())`, so every step re-tokenizes every declaration so far. The tokens of the vocabulary only grow, so they could be kept like `_used`.

- Characters tokenized per formalized chain (`audit_load_5 tokens`): 50 steps 16× the trace's SMT text, 200 steps 63×, 800 steps 238×.
- `cProfile` of a 1600-step numeric trace: `_tokens` takes 4.9 of 8.7 s of `formalize` (56%), all on the event loop; formalize totals 4.9–5.1 s without profiling (0.34 s at 400 steps). Each step stays at ≤ 22 ms, so no single stall is large.
- Memory is not the problem: `_used` holds 1 603 tokens at 1600 steps and lives for one `verify`.

**Repro.** `test_the_earlier_use_rule_tokenizes_each_declaration_once` counts characters passed to `_tokens` while formalizing a 200-step chain and asserts at most 4× the final vocabulary plus assertions; today 63×. 0.8 s. Script: `audit_load_5 tokens`.

**Root cause.** `src/autolab/verification/formalizer.py:128` (`_tokens(self.vocabulary.smt())` per call). The full-vocabulary `parse_smt2_string` at `formalizer.py:136` is also per step (2.1 s of the 8.7 s), as it was before SG20.

**Suggested fix.** Keep a `self._declared_tokens` set and extend it with the tokens of each accepted step's new declarations only.

---

## Earlier fixes, re-measured

Quiet machine (load 1.2–2.9) unless stated.

| Fix | Claim | Re-measured now | Status |
|---|---|---|---|
| AUDIT-2 #1 (SG10) | Z3 off asyncio's default pool; `getaddrinfo` during 50 slow verifies stays fast; `solver_ms` excludes queueing | `getaddrinfo` 0.00 s during 50 slow verifies (idle 0.4 ms). 50 × 1 s: 5.60 s wall, `solver_ms` 1001–1017 ms; 50 × 5 s: **25.7 s** (125.5 s in AUDIT-14, 35 s on 8 workers), `solver_ms` 5000–5017 ms | Holds (better through SG22's slow lane) |
| AUDIT-2 #2, #3 (SG9) | atomic upserts, `(run_id, id)` keys | Repros pass in `make test`; 200 concurrent upserts of one key from 4 engines: 0 errors, 1 row, 1.71 s | Holds |
| AUDIT-2 #4 (SG8) | ≤ 3 HTTP attempts, jitter, `retry-after` | Repros pass in `make test`; the AUDIT-6 #3 `retry-after` herd is still an open xfail | Partly holds (unchanged) |
| AUDIT-2 #5 (SG10) | 10 steps at 100/200/400 ms per call ≈ N + 1 rounds | 1.17 / 2.28 / 4.48 s (0.56–0.58× the sum) | Holds |
| AUDIT-2 #6 (SG10, SG19, SG22) | a cancel frees the worker | Next job 0.00 s after cancelling 8 verifies. 100 / 400 cancels of running checks: peak 2 re-send threads, 0 left, worst release 6.0 / 16.2 ms. Int 1600 cancelled at 0.4 s: free after 0.01 s. **Real 1000 / 1600: 1.86 / 9.41 s** | Holds except Real pools (finding 3) |
| AUDIT-2 #7 (SG10) | 5 100 / 320 800 declaration lines at 100 / 800 uncited steps | 5 100 / 320 800; 800 uncited steps 8.0 s, max step 20.1 ms | Holds |
| AUDIT-6 #1 (SG11) | no crash with verifies on several threads | `audit_load_2 segv` 0/30 crashed; `audit_load_3 threads` 0/20 (2 × 20 … 16 × 5) | Holds |
| AUDIT-10 #1 (SG19, SG22) | 10 × 100-step p95 ≈ 6–9 ms on 2 workers | 10 × 100: 3.53–3.61 s, p95 8.1 ms; 50 × 100: 18.4 s, p95 8.1 ms; 30 × 100: 11.0 s, p95 8.2 ms; 10 × 200 p95 8.4 ms; 4 × 400 p95 10.0 ms; 2 × 800 p95 12.1 ms (max 112 ms, one step). All VALID | Holds |
| AUDIT-10 #5 (SG19) | no worker enters z3py's printer | its two tests pass in `make test` | Holds |
| AUDIT-14 #1 (SG22) | the entailment check honours its timeout and cancels | `lia_chain` 800–2000 at 0.5 / 1 / 5 s: 0.1–1.03× the timeout, verdicts as on the add path. Cancel at 0.8 s into 1600 / 2000: free after 0.04 / 0.33 s. Boolean, Int and Real pools with the deadline a quarter into the entailment check: 0.91–1.14× | Holds for Int and Boolean; Real pools overrun in the consistency check (finding 3) |
| AUDIT-14 #2 (SG22) | hard runs do not starve an easy verify | Quiet: 0.06–0.1 s beside 1–2 hard runs at 1 s and 5 s. Under load the stale-permit race serializes the pool: 150 s (1 worker, 3 hard runs, 5 s), and the flipped repro failed 5 of 10 runs at load ~4 (finding 2). Above the cap 25–46 s (finding 4) | **Does not hold under load** |

## CLAUDE.md budgets

- **10-step trace in < 30 s.** 0.06–0.07 s at zero LLM latency; with faithfulness on, 11.08 s at 1 s per call and 22.12 s at 2 s per call. It holds for one run alone while the mean structured call takes ≤ 2.7 s. It **does not hold** when hard runs share the pool and the pool loses its spare thread (150–400 s; finding 2), above `slow_workers` (45 s at 16 hard runs; finding 4), or while a formalization declares a wide function (21 s for one 16-argument declaration, unbounded beyond; finding 1).
- **50 ms p95 per step (`scripts/bench_verifier.py --runs 20`).** Step p95 9.59–9.85 ms, max 29.8 ms (1680 samples). It holds for the golden corpus and for concurrent Boolean chains up to 800 steps (p95 ≤ 12.1 ms) and Int traces up to 400 steps (44.9 ms). It does not hold for Real-valued traces of 400 steps or more (88.4 ms at 400, about 1 s per premise check at 800; finding 3).

## What held up

- **SG22's slow lane, when its pool can grow.** 50 runs-to-timeout verifies take 25.7 s at 5 s (5× faster than AUDIT-14). Thread counts are bounded: after bursts of 12 and 40 slow checks the pool kept 10 and 20 threads (`workers + slow_workers` at most), `_max_workers` returned to 2 and `_slow` to 0. No interrupt threads were left behind.
- **SG22's add path and `_SETUP_SHARE`.** No Int or Boolean overrun above 1.14× in any measurement here.
- **SG20's earlier-use rule, memory.** `_used` is per `verify` (1 603 tokens at 1600 steps) and is freed with the `Formalizer`; the caches are `lru_cache(maxsize=4096)`.
- **SG23's store.** 0 store failures in 100–200 concurrent traced runs with 1, 4 or 8 engines on one file; 200 concurrent upserts of one key in 1.71 s.
- **Event loop.** Max stall 2.7 / 13.2 / 19.3 ms with 1 / 10 / 50 concurrent 60-step numeric verifies; 104 ms once in the 2 × 800 and 50 × 100 loads. Formalize steps of a 1600-step trace stay ≤ 22 ms each. The exceptions are findings 1 and 8.
- **Memory.** RSS 357 → 361 MB over 1000 verifies (100 at a time), flat after the first batch; 747–783 MB and flat over 300 verifies beside 4 hard runs on the slow lane (after the Real-pool sections had run in the same process).
- **Concurrency correctness.** 10, 30 and 50 concurrent 100-step verifies: 0 non-VALID steps.

## Verification

`make lint typecheck test`: lint and mypy clean (135 files formatted; mypy: no issues in 124 source files). `make test` (load 1.6): 1025 passed, 13 skipped, 38 xfailed (11 new) in 205 s. Under load ~4 it can fail on `test_audit_14.py::test_hard_runs_do_not_starve_an_easy_verify` (finding 2; 5 of 10 runs alone). `uv run pytest tests/audit/test_audit_18.py --runxfail -p no:randomly`: 11 failed on their assertions, 10 of 10 runs; without `--runxfail`: 11 xfailed.
