# AUDIT-22: Performance under load, round 6

Auditor: independent (wrote none of the audited code or the earlier audits). Base: `origin/claude/sg27-real-timeouts` @ `492980e` (SG24–SG27 merged).
Machine: 4 CPUs, 16 GB, Linux, Python 3.11.17, z3 4.16.0, GIL switch interval 5 ms. Defaults here: `SOLVER_WORKERS` = 2, `SOLVER_SLOW_WORKERS` = 2 (SG26 sizes them from the affinity: `min(2, cpus)` and `cpus - 2`).
All runs use `FakeLLMClient`, subclasses of it, or stubs. Nothing touches the network and no real LLM is used. No source file or existing test was changed.

**Load caveat.** Other agents' suites shared the machine for parts of the audit (load average 0.3–5.5 on 4 CPUs). `uptime` was read before each timing run; the repro runs below were at load 0.5–2.0. Every timing repro compares the machine against itself (a share of the same check's uninterrupted run, the interpreter's GIL switch interval, or a fake LLM's own latency), so a faster CI runner scales both sides.

- Repros: `tests/audit/test_audit_22.py`, 9 tests. Each is marked `xfail(strict=True, reason="AUDIT-22 #n")` and asserts the correct behaviour; wall-clock ones carry `perf`. With `--runxfail -p no:randomly` all 9 failed on their assertions in 10 of 10 runs (24.9–26.9 s per run, load 0.5–2.0). Without `--runxfail`: 9 xfailed.
- Measurements: `uv run python -m scripts.audit_load_6 [section ...]`. Sections: `probe`, `gil`, `concurrent`, `unpolled`, `cancel`, `newcomer`, `cgroup`, `estimate`. The AUDIT-18 load helpers (`scripts/audit_load_{4,5}.py`) are reused.

| # | Severity | Title |
|---|---|---|
| 1 | **Critical** | SG24's bounded probe still makes quadratic work: one 4 KB declaration line blocks the event loop for 11–13 s (52 s at 8 KB) |
| 2 | **High** | SG27 loads every pool of more than 100 premises with one GIL hand-off per premise: 80–116× slower beside Python work, decidable checks time out, and 4 concurrent 400-step Boolean traces reach a step p95 of 152 ms (3× budget, 17× one-call loading) |
| 3 | **High** | Pools of ≤ 100 long premises (or one wide premise in a long pool) still run one unpolled simplex: timeouts overrun 5–20×, cancels are ignored for 2–10 s |
| 4 | **High** | A new run's first check is not "short", and short callers are always served first: it waits until no short caller is left (1.9–3.0 s, unbounded) |
| 5 | Medium | Cancelling a slow check gives back its slow place and thread budget while its thread still runs: the next check queues behind an uninterruptible one |
| 6 | Medium | SG26 sizes the pool from the affinity, not the cgroup CPU quota: a 1-CPU container on a 4-CPU host still gets 2 slots + 2 slow places |
| 7 | Medium | SG25's estimate runs after the whole verify: a design it rejects is found out after 13 sequential calls instead of 3, on each of up to 3 attempts |

One critical, three high and three medium findings.

---

## 1. Critical: the bounded probe is still quadratic in a declaration's width

**Impact.** SG24 replaced the 2^k applied-builtin probe by 2k + 2 probes (`_numeric_variants`), so the number of parses is linear in the arity k. But each probe (`_applied_use`) declares k constants and applies the name to all k of them, so each parse is O(k) characters and the probe as a whole is O(k²). It still runs inside `Vocabulary.merged`, synchronously in `Formalizer.formalize` on the event loop, so no timeout, cancel or other run can proceed meanwhile. The input is one line of one LLM reply; `(declare-fun f (Int Int … Int) Bool)` is 4 characters per argument, and nothing bounds the arity.

`audit_load_6 probe` (one declaration, caches cleared, load 1.3):

| Int arguments | Declaration | Characters parsed | × the line | `merged` |
|---|---|---|---|---|
| 16 | 88 B | 17 K | 193 | 10 ms |
| 64 | 280 B | 261 K | 933 | 65 ms |
| 256 | 1.0 KB | 4.4 M | 4 179 | 0.71–0.74 s |
| 512 | 2.1 KB | 17.8 M | 8 592 | 3.1–3.4 s (event-loop stall measured through `formalize`: 3 104 ms) |
| 1024 | 4.1 KB | 72.0 M | 17 455 | **11.1–13.0 s** (through `formalize`: 11.95 s) |
| 2048 | 8.2 KB | | | **51.7 s** (scratch run) |

- A 4 KB line is about 1 000 output tokens; the client's `max_tokens = 4096` allows ~4 000 arguments, which extrapolates to ~3 min of a frozen process. That is the same failure class as AUDIT-18 #1 (critical), with a polynomial instead of an exponential: every run in the process stops, and pytest-timeout's signal method cannot fire either.
- A wide uninterpreted function is not a realistic modelling choice, but the formalizer's input is untrusted LLM output (CLAUDE.md §4), and a degenerate reply is enough.
- Checked and not a finding: SG24's tokenizer. Its regex is quadratic in trailing whitespace (`_tokens("a" + 16 000 blanks)`: 12.6 s, also before SG24), but every string reaching it is stripped first (`FormalizedStep` strips whitespace, `merged` strips each line), and an unterminated `"` is refused by `_is_single_term` in linear time. Through `Formalizer.formalize`, 16 000 trailing blanks cost 0.3 ms.

**Repro.** `test_probing_a_wide_declaration_parses_text_linear_in_it` merges one 1024-argument declaration and counts the characters handed to `Z3_parse_smtlib2_string`, stopping once they exceed 64× the line. It asserts they stay within that budget; today the budget is exceeded within the first parses (72 M needed). Deterministic, 0.1 s. Script: `audit_load_6 probe`.

**Root cause.** `src/autolab/verification/formalizer.py:282` (one probe per variant, each built by `_applied_use`, `:316-320`, over all k arguments) and `:286-288` (each probe also re-sends `sort_decls`), called from `Vocabulary.merged` (`:78`) on the event loop (`Formalizer.formalize`, `:130`).

**Suggested fix.**
- Bound the arity before probing: refuse (fail closed with `FormalizationError`) a declaration with more than a small number of arguments, say 16. No Z3 builtin that a user function could take over needs more, and no step needs a 1 000-argument function.
- Or bound the probe's total size: stop probing (and refuse the step) once the parsed text exceeds a fixed multiple of the line.
- Longer term, run `formalize`'s Z3 parses off the event loop (the solver executor), so a slow parse can be timed out (AUDIT-18 #1's last suggestion, still open).

---

## 2. High: SG27's loading pays one GIL hand-off per premise

**Impact.** SG27 loads every pool of more than `_LOAD_ABOVE` = 100 premises one formula at a time: for each premise, a `Z3_solver_check`, a `Z3_solver_assert`, a `Z3_ast_vector_get` and some Python between them (`_load`). Each C call goes through ctypes, which drops the GIL and takes it back. When another thread is running Python (the event loop formalizing or validating other runs, another worker between its C calls), taking it back waits up to the switch interval (5 ms). AUDIT-10 #1 removed exactly this per-premise hand-off (the one-call `from_string` path, "asserting the parsed AstVector item by item costs a GIL hand-off per call"); SG27 brought it back for every long pool, Boolean and Int included, although only Real pools needed a new path (AUDIT-18 #3).

Premise check of `bool_chain(n)` (`audit_load_6 gil`, best of 3 / 2, load 1.5):

| Premises | Path | Quiet | Beside a Python thread |
|---|---|---|---|
| 100 | one call | 6.3 ms | 44.6 ms (7×) |
| 101 | loading | 16.4 ms | **1 317 ms (80×)** |
| 200 | loading | 29.2 ms | 2 495 ms (86×) |
| 400 | loading | 43.6 ms | 4 613 ms (106×) |
| 800 | loading | 72.9 ms | 8 424 ms (116×) |

- **Verdicts change.** `bool_chain(800)` entails its claim in 82–88 ms alone; beside a Python thread it returns `timeout` (UNGROUNDED) at the default 5 s (5.05 s), and at 20× its quiet time (1.64 s). Same for 400 premises (quiet 42 ms; `timeout` at 0.84 s). So whether a long Boolean or Int step is VALID depends on what else the process is doing, the AUDIT-18 #5 failure through a different door.
- **The step budget breaks under ordinary concurrency.** `audit_load_6 concurrent`, k concurrent uncited verifies on a default executor, against the same load with one-call loading (`one_call_loading()`, the pre-SG27 path for these pools):

  | Trace | k × steps | Step p95 as built | One call | Wall as built | One call |
  |---|---|---|---|---|---|
  | Boolean | 1 × 200 | 22.3 ms | 6.9 ms | 2.46 s | 1.24 s |
  | Boolean | 4 × 200 | 33.6 ms | 7.7 ms | 8.11 s | 3.54 s |
  | Boolean | 1 × 400 | 38.8 ms | 8.2 ms | 9.42 s | 2.95 s |
  | Boolean | 4 × 400 | **151.7 ms** | 9.0 ms | 30.4 s | 8.10 s |
  | Int | 1 × 200 | 39.4 ms | 22.2 ms | 4.19 s | 2.86 s |
  | Int | 4 × 200 | 62.5 ms | 28.1 ms | 12.0 s | 8.46 s |
  | Int | 1 × 400 | 105.0 ms | 41.6 ms | 19.7 s | 9.49 s |
  | Int | 4 × 400 | 221.8 ms | 82.6 ms | 45.4 s | 29.0 s |
  | Real | 1 × 200 | 37.6 ms | 22.7 ms | 3.95 s | 2.70 s |
  | Real | 4 × 200 | 50.1 ms | 22.0 ms | 11.4 s | 6.68 s |
  | Real | 1 × 400 | 89.0 ms | 92.5 ms | 18.6 s | 15.5 s |
  | Real | 4 × 400 | 203.1 ms | 97.9 ms | 45.4 s | 31.7 s |

  Only Real pools of 400 steps break even; every other row costs 1.5–17× its one-call twin, and the 4-run rows are what a process verifying a few long traces at once sees. AUDIT-18 measured Int traces of 400 steps at p95 44.9 ms alone (41.6 ms here with one-call loading, 105 ms as built). Every step stayed VALID in these runs (the default 5 s timeout was not reached), but each long pool now costs several times its one-call twin even on a quiet machine, and the whole trace pays it on every premise step (O(n²) `Z3_solver_check` calls per trace: about 80 000 for a 400-step uncited trace).
- There is a cliff at the threshold: one more premise (100 → 101) costs 2.6× quiet and 30× beside Python.

**Repro.**
- `test_a_long_premise_check_waits_for_the_gil_a_bounded_number_of_times`: the 200-premise Boolean premise check alone and beside a Python thread; asserts the difference is at most 40 switch intervals (200 ms; the one-call path at 100 premises pays ~40 ms). Today 2.5 s. Independent of CPU speed: it counts GIL waits. 2.6 s.
- `test_an_entailment_beside_python_work_reaches_its_quiet_verdict`: 400 Boolean premises, timeout 20× the check's quiet run time, beside a Python thread; asserts `entailed`. Today `timeout`. 1.0 s.

Script: `audit_load_6 gil concurrent`.

**Root cause.** `src/autolab/verification/solver.py:164` (`len(premises) > _LOAD_ABOVE` sends every long pool, whatever its theory, to `_load`) and `solver.py:244-255` (four ctypes calls and the `time.monotonic()` / `interrupt.requested()` polls per premise), against the AUDIT-10 #1 rationale at `solver.py:167-168`.

**Suggested fix.**
- Load in growing chunks rather than one formula at a time (for example assert 1, 2, 4, … premises between checks, or a fixed chunk of 32): the simplex stays short for chained bounds, and the hand-offs drop from n to O(log n) or n / 32.
- Or keep one-call loading for pools whose simplex is cheap (Boolean, Int, or any pool below a size measured in atoms rather than premises), and load incrementally only where the unpolled phase was measured (Real arithmetic).
- Better, AUDIT-18 #3's first suggestion: one solver per verify, premises asserted as they become VALID, each step under `push`/`pop`. Each step then adds O(1) premises, so both the per-premise hand-offs and the O(n²) total disappear.
- Add a concurrent long-trace load (4 × 400 uncited Boolean) to `bench_verifier` or the latency tests; today they would have caught this.

---

## 3. High: pools of few, long premises still run one unpolled simplex

**Impact.** SG27 decides how to load by the number of premises, but what makes Z3's simplex run without polling its limit is the number of bounds. A pool of ≤ 100 premises that together state hundreds of Real bounds takes the one-call path, and its single `solver.check()` overruns its timeout and ignores interrupts exactly as AUDIT-18 #3 measured. In a long pool, one wide premise does the same inside a single `Z3_solver_check` of `_load`. A step whose formalization is one conjunction of many bounds, a fact list built as one formula, or a context with a few wide facts all produce this shape.

`audit_load_6 unpolled` (premise checks, claim `true`, load 1.4):

| Pool | Alone | Timeout | Result | Cancel 1/10 in |
|---|---|---|---|---|
| 1 premise of 1 000 Real bounds | 2.59 s | 0.5 s | `timeout` after 2.68 s (5.4×) | worker free after 2.38 s |
| 50 premises × 20 bounds | 2.76 s | 0.5 s | `timeout` after 2.61 s (5.2×) | free after 2.20 s |
| 100 premises × 16 bounds | 10.6 s | 0.5 s | **`timeout` after 10.16 s (20.3×)** | free after 10.11 s |
| 150 premises, one of 1 000 bounds (loading path) | 1.23 s | 0.25 s | `timeout` after 1.23 s (5.0×) | free after 1.35 s |

- These are the numbers AUDIT-18 #3 reported for 1 000–1 600 one-bound premises (5–20×, a worker held 9.4 s), which SG27 fixed only for that shape. The AUDIT-18 #3 repros pass because they use one bound per premise.
- A held worker also feeds finding 5 and AUDIT-18 #4 / SG26's slow-lane cap; `shutdown()` joins it.

**Repro.**
- `test_a_pool_of_long_real_premises_returns_within_its_timeout`: 50 premises of 20 chained Real bounds, timeout a fifth of the check's own uninterrupted run time; asserts `timeout` within 1.5× of it. Today 4.6–5.2×. ~5 s.
- `test_cancelling_a_check_of_long_real_premises_frees_its_worker_promptly`: the same pool cancelled a tenth of the way in; asserts the worker is free within a fifth of the run time. Today after 2.1–2.2 s of 2.3–2.8 s. ~5 s.

Script: `audit_load_6 unpolled`.

**Root cause.** `src/autolab/verification/solver.py:164-169` (the path is chosen by `len(premises)`; ≤ 100 premises go to `from_string` and one `solver.check()` at `:181-182`) and `solver.py:251` (`_load` checks once per premise, however many bounds the premise holds); Z3's arithmetic core on chained strict Real bounds does not poll the resource limit that `timeout` and `interrupt()` set.

**Suggested fix.**
- Split top-level `and`s of each premise (and fact) into separate assertions before choosing the path, and choose by atom count, not premise count; or always load Real pools incrementally.
- Bound what Z3 cannot: check in a worker process that can be killed at `timeout + grace` (AUDIT-10 #1's and AUDIT-18 #3's alternative), or refuse pools beyond a measured number of Real atoms with `timeout`.
- Add the conjunction shape to the SG27 tests and the AUDIT-18 #3 load.

---

## 4. High: a new run's first check waits until no short caller is left

**Impact.** SG26 serves callers "whose last check was short" first (`_Slots._wake` sorts waiters by `short`), to keep easy runs moving beside hard ones (AUDIT-18 #4). But `short` is "this task is in `_short`", and a task enters `_short` only after its first check ends. So the first check of every new verify (a new task) is ranked with the hard runs, and as long as any short caller waits, it is never served. With more short-check runs than slots, a short caller always waits, so a new run is starved until the others stop. There is no aging, so the same holds for any run classified hard, for as long as the short stream lasts.

`audit_load_6 newcomer` (2 slots, busy runs making back-to-back checks that stay within one slice, for 3 s; the newcomer is submitted once 3 busy runs have finished two checks):

| Busy runs | Check | Newcomer's first check waited | Busy runs had left |
|---|---|---|---|
| 3 | 20 ms | **2.94–2.96 s** | 2.93–2.94 s |
| 6 | 20 ms | 2.96–2.99 s | 2.90–2.94 s (in one run a busy run had not had a single check) |
| 6 | 40 ms | 2.96–2.99 s | 2.83–2.87 s |
| 12 | 10 ms | 3.03 s | 2.97 s |

- With no more busy runs than slots nobody waits, and the newcomer was served in 0.02 s (2 runs, scratch run).
- With real Z3 work the same appears: 6 runs making back-to-back `lia_chain(150)` premise checks (~30 ms) on 2 slots held a new run's first check for 5.58 s, the whole 6 s window (scratch run). With checks over 50 ms the slow lane frees the slots and the effect disappears.
- FIFO would have served the newcomer after about one check per run ahead of it (~0.05 s). The wait is bounded only by how long the other runs keep making short checks: a server verifying many short traces can hold a new run's first step indefinitely, and with it the run's HITL timers and LLM work.
- On a 2-CPU host (`SOLVER_SLOW_WORKERS` = 0) slots are freed only when checks end, so more checks are slot-bound and the condition is easier to reach.

**Repro.** `test_a_new_runs_first_check_is_not_starved_by_short_callers`: 2 slots, 3 runs of back-to-back 20 ms sleeping checks for 2 s; asserts the newcomer's first check finishes within a quarter of the time the busy runs had left. Today it waits for all of it (1.94 s of 1.94 s). The checks sleep, so CPU speed does not matter. 2.0 s. Script: `audit_load_6 newcomer`.

**Root cause.** `src/autolab/verification/solver.py:540` (`short = task in self._short`: false for a task with no check yet), `solver.py:497-506` (`_wake` grants to every short waiter before any other, with no aging) and `solver.py:576-581` (a task becomes short only after a check).

**Suggested fix.** Treat a task with no last check as short (a new run has not shown it is hard), and age waiters: a non-short waiter that has been passed over for, say, one slice or `workers` grants is served next. Alternatively, admit round-robin per run, as AUDIT-18 #4 suggested.

---

## 5. Medium: a cancelled slow check still holds its thread

**Impact.** When a slow check's caller is cancelled, `SolverExecutor.run` decrements `_slow` and lowers the pool's limit (`resize(-1)`) at once, in its `finally`, while the check is still running on its thread until Z3 honours the interrupt. The pool then has no idle thread and no room for one, so the next caller queues behind the cancelled check (and a spare thread started for an earlier caller retires after its next job, since the pool is over its limit). For a check Z3 cannot interrupt (finding 3) that is the rest of its run: AUDIT-18 #2's effect (a check waits for a thread busy with someone else's slow check), now caused by a cancel.

`audit_load_6 cancel` (finding 3's 50 × 20-bound pool, alone 2.35 s; each hard check had given up its slot and was cancelled a tenth of the way in):

| Workers / slow | Hard checks cancelled | Next easy check |
|---|---|---|
| 1 / 1 | 1 | **2.15 s** (then 7 ms) |
| 2 / 2 | 2 | **2.24 s** |

- Cancels are ordinary: a verify cancelled by an orchestrator timeout, a run aborted from the HITL queue, `asyncio.wait_for` around an agent, shutdown.
- With interruptible checks the window is milliseconds, which is why the SG26 tests do not see it. A cancel that lands before Z3 reaches the unpolled phase is honoured at once (0.01 s in one scratch run).

**Repro.** `test_cancelling_a_slow_check_does_not_hold_up_the_next_one`: 1 worker, 1 slow place; finding 3's pool goes slow and is cancelled a tenth of the way in; asserts the next easy check takes under a fifth of the hard check's run time. Today 2.1 s of 2.5 s. ~5 s. Fixing finding 3 alone also makes this pass (the thread is freed promptly); the accounting is still wrong for any check whose interrupt lands late. Script: `audit_load_6 cancel`.

**Root cause.** `src/autolab/verification/solver.py:561-569` (on `CancelledError`, the `finally` releases `_slow` and the thread budget before the job ends), with `solver.py:434-436` (a thread over the limit retires after its next job).

**Suggested fix.** Give back the slow place and the thread budget when the job ends on its thread (a done callback on the pool future), not when the caller stops waiting.

---

## 6. Medium: the pool is sized from the affinity, not the CPU quota

**Impact.** SG26 sizes the pool from `os.process_cpu_count()` (Python 3.13) or `len(os.sched_getaffinity(0))`. Neither reads the cgroup CPU quota, which is how `docker run --cpus=1`, Kubernetes CPU limits and most CI containers restrict CPUs: the affinity still lists every host CPU. A 1-CPU container on a 4-CPU host gets 2 slots and 2 slow places, the oversubscription AUDIT-18 #5 measured (10 of 10 decidable checks timing out). The AUDIT-18 #5 repro passes because it pins the affinity.

`audit_load_6 cgroup`: a child whose cgroup v2 (`cpu.max`) and v1 (`cpu.cfs_quota_us` / `cpu.cfs_period_us`) files allow 1 or 2 CPUs on this 4-CPU host: `workers` = 2, `slow_workers` = 2 in both cases.

**Repro.** `test_the_solver_is_sized_to_a_one_cpu_quota`: the child above serves the quota files through `open`, `io.open`, `os.path.exists/isfile` and `pathlib.Path.exists/is_file` (this host uses cgroup v1 without a quota), imports `solver` and asserts `SOLVER_WORKERS + SOLVER_SLOW_WORKERS <= 1`. Today 4. Deterministic, 1.5 s. Script: `audit_load_6 cgroup`.

**Root cause.** `src/autolab/verification/solver.py:55-68` (`_available_cpus`, and the sizes derived from it).

**Suggested fix.** Take the minimum of the affinity and the cgroup quota (v2 `cpu.max`, v1 `cpu.cfs_quota_us` / `cpu.cfs_period_us`, rounded up), and let `Settings` override both sizes for deployments that know better.

---

## 7. Medium: the independent estimate runs after the whole verify

**Impact.** `DesignAgent.__call__` runs the base template first (design call, trace call, one formalize call per step, the verify) and only then the `design.estimate` call (and the free-text figure check). So a design whose figures the estimate rejects, which is withheld and retried (up to `max_retries` = 2, so 3 attempts), is found out only after the full trace has been formalized and verified on every attempt. A rejection needs only the design and the estimate. The estimate is also made for designs the verifier has already withheld, and in the passing case it adds one LLM round trip after the verify instead of overlapping it.

`audit_load_6 estimate` (`SlowLLM`: every call answers after 100 ms):

| Trace | Estimate | Calls | Attempt | |
|---|---|---|---|---|
| 10 steps | disagrees (retry) | 13, estimate last | 1.46–1.48 s (14.6–14.8 calls' time) | 3 calls decide it |
| 10 steps | agrees | 13 | 1.44–1.46 s | the estimate could overlap the verify |
| 20 steps | disagrees | 23 | 2.59–2.60 s (26 calls' time) | |

- At a realistic 2 s per call, a 10-step design rejected by its estimate costs ~26 s per attempt instead of ~6 s, ~78 s over three attempts, plus the tokens of 30 formalize calls (60 with the faithfulness judge on a live client) that cannot change the outcome. The verify's own solver time is small beside it.
- No loop: the retries are bounded by `max_retries`, and a human `retry` resets the count deliberately.

**Repro.** `test_a_design_the_estimate_rejects_is_rejected_in_a_few_calls_time`: 10-step trace, 100 ms per call, an estimate 100× the design's cost; asserts the attempt is withheld for a retry within 6 calls' time. Today 14.4–14.6. Calibrated on the fake's latency. 1.5 s. Script: `audit_load_6 estimate`.

**Root cause.** `src/autolab/agents/design.py:85-98` (`super().__call__(state)`, the verify, before `_stated_figures` and the `design.estimate` call).

**Suggested fix.** Make the estimate (and `_stated_figures`) right after `_produce`, concurrently with the verify, and skip or cancel the verify when either rejects the design; keep the policy step on the verified figures as it is.

---

## Earlier fixes, re-measured

Load 1.4–2.1 unless stated.

| Fix | Claim | Re-measured now | Status |
|---|---|---|---|
| AUDIT-18 #1 (SG24) | the applied probe makes a bounded number of parses | 2k + 2 parses (36 at 16 arguments, 2 052 at 1 024); its repro passes. The parses grow with k, so the work is O(k²): 11–13 s at 1 024 arguments | **Holds for the count, not for the time (finding 1)** |
| AUDIT-18 #2 (SG26) | an easy check never queues behind one that gave up its slot | Repro passes; `audit_load_5 handoff`: forced stale permit 0.01 s (was 2.96 s); 10-step verify beside 1–4 hard runs at 0.5 s and 5 s: 0.05–0.26 s in 49 of 49 trials (was 15–150 s) | Holds, except after a cancel (finding 5) |
| AUDIT-18 #3 (SG27) | Real pools honour timeouts and cancels | Repros pass (one bound per premise). The same bounds in ≤ 100 premises, or in one premise of a long pool: 5–20× overruns, cancels ignored 1.4–10 s | **Holds only for one bound per premise (finding 3)** |
| AUDIT-18 #4 (SG26) | hard runs above the slow cap do not starve an easy verify | `audit_load_5 cap`: 10-step verify 0.32–0.45 s beside 8–16 hard runs at 5 s (was 0.36–46 s), 0.40–0.44 s on 1 worker beside 6–7 | Holds for runs past their first check; a new run waits (finding 4) |
| AUDIT-18 #5 (SG26) | slots and slow checks do not outnumber the CPUs | Its repro (affinity pinned to 1 CPU) passes; under a cgroup quota the sizes ignore it (finding 6) | **Holds for affinity only** |
| AUDIT-18 #7 (SG27) | a premise check is not skipped when its entailment step needs no time | Repro passes (`true` shortcut) | Holds |
| AUDIT-14 #1, #2, AUDIT-20 (SG26) | timeout, cancel and starvation repros | `test_audit_14.py`, `test_audit_18.py`, `test_audit_20.py`: 38 passed, 3 xfailed (AUDIT-18 #6, #8, #9, still open) in 46 s at load 1.5 | Holds |
| SG26 threads | the pool's threads are bounded | `audit_load_5 threads`: bursts of 12 and 40 slow checks left 3 pool threads, `_max_workers` back to 2, `_slow` 0 | Holds |

## CLAUDE.md budgets

- **50 ms p95 per step (`scripts/bench_verifier.py --runs 10`).** Step p95 10.74 ms, max 19.85 ms (840 samples, load 1.5): the golden corpus is within budget. It does not hold for uncited traces of 200–400 steps once a few run at once: Boolean 4 × 400 at 151.7 ms, Int 4 × 400 at 221.8 ms, Real 4 × 400 at 203.1 ms; and alone, Int 400 steps at 105 ms and Real 400 at 89 ms (finding 2; with one-call loading the Boolean rows are at 7.7–9.0 ms).
- **10-step trace in < 30 s.** Holds alone (≈ 0.07 s of solver and formalizer time; at 100 ms per LLM call a 10-step Design attempt takes 1.46 s, finding 7). It does not hold while a formalization declares a function of a few thousand arguments (finding 1: 52 s at 2 048), and a new run's first step can wait as long as other runs keep making short checks (finding 4).

## What held up

- **SG26's pool.** No stale-permit stall in 49 trials of the AUDIT-18 #2 loads, bounded threads after bursts, `_max_workers` and `_slow` back to their resting values, and the AUDIT-18 #4 cap load at 0.32–0.45 s instead of up to 46 s.
- **SG27 for the shape it targets.** One-bound-per-premise Real pools honour their timeout and cancels (AUDIT-18 #3 repros pass), and the `true` shortcut removes AUDIT-18 #7. Real 400-step traces cost the same as before (89 vs 92.5 ms p95 alone).
- **SG25 has no retry loop.** Estimate disagreements are retried at most `max_retries` times; only a human `retry` resets the count.
- **SG24's tokenizer on reachable inputs.** Its regex is quadratic in trailing whitespace, but every input is stripped or refused first (finding 1, last bullet).
- **Correctness under load.** Every step of every concurrent verify above was VALID (0 non-VALID steps in all 24 runs of `audit_load_6 concurrent`); the verdict changes in finding 2 needed a Python thread beside the check.

## Verification

`make lint typecheck test` (load 1.7): ruff clean (143 files formatted), mypy: no issues in 131 source files; `make test` 1211 passed, 13 skipped, 44 xfailed (9 new) in 240 s. `uv run pytest tests/audit/test_audit_22.py -q`: 9 xfailed in 24 s. `uv run pytest tests/audit/test_audit_22.py --runxfail -p no:randomly -q`: 9 failed on their assertions, 10 of 10 runs (24.9–26.9 s each, load 0.5–2.0).
