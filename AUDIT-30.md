# AUDIT-30: Performance under load, round 8

Auditor: independent. I wrote none of the audited code or the earlier audits. Base: `origin/claude/sg32-newcomers` @ `f09ea60` (SG31, SG32 and SG33 merged).
Machine: 4 CPUs, Linux, Python 3.11, z3 4.16.0, no cgroup CPU quota, so the defaults are 2 slots and 2 slow places. Load average was 0.1–5.1 during the audit, because other agents shared the machine.
All runs use `FakeLLMClient`, stubs, or scripted replies. Nothing touches the network. No source file or existing test was changed.

- **Repros:** `tests/audit/test_audit_30.py` has 5 tests. Each is `xfail(strict=True, reason="AUDIT-30 #n")` and asserts the correct behaviour. The four timing tests also carry `perf`.
  - With `--runxfail -p no:randomly`, all 5 failed on their assertions in 2 of 2 runs (11.4 s quiet, 14.1 s under 4 CPU burners).
  - Without `--runxfail`, all 5 xfail.
- **Measurements:** `uv run python -m scripts.audit_load_8 [--burn N] [section ...]`. The sections are `together`, `fresh`, `graph`, `sg32`, `starve`, `probe`, `vocab`, `cold`, `fork` and `ipc`. For the version comparison in #2, the same script ran against a `git archive` of `41348b0` (SG31 + SG33, before SG32).

| # | Severity | Title |
|---|---|---|
| 1 | **High** | AUDIT-26 #1 still holds for runs that start together, and in the orchestrator every verify is a newcomer. A 10-step easy verify beside 16 hard runs takes 20.3 s (0.04 s alone), the same as round 7. SG32's harness change hides this. |
| 2 | Medium | Regression from SG32: a process forked after a `Verifier` is built can never check. Every check raises (`AssertionError`, then `ChildProcessError`). Before SG32, the same child checked VALID. |
| 3 | Medium | Event-loop work in the formalizer grows with the vocabulary, outside SG33's budget. The same admitted reply costs 23 → 134 ms at 20 000 symbols. 6 000 facts take 2.8–3.5 s to merge at verify start. A 100-step trace takes 224 ms per step. |
| 4 | Low | A first check's wait for worker start-up is still recorded nowhere. When a verify starts right after `Verifier()`, the first check takes 240–420 ms and its verdict records 13–27 ms. |

In total: one high, two medium and one low finding. None is critical.

---

## 1. High: newcomers still queue behind hard runs that start together, and every verify is a newcomer

**What SG32 changed.** A task with no completed check now ranks with short callers (`_rank` 1). It still gets no lent place.

**Why that does not help.** When the hard runs are newcomers too, all of them rank 1 and are served FIFO. So the easy newcomer waits behind every hard run's first check, which is the AUDIT-26 #1 load exactly as round 7 measured it. The ADR amendment admits this under "What this does not bound".

**The harness change.** SG32 changed `scripts/audit_load_7.newcomer_first_check_s`: it now sends the newcomer only after every hard run has completed a check. That removes the case the finding was about, and the round-7 repro now passes without the behaviour changing. This weakens the repro.

**The production shape is the unbounded case.** LangGraph runs each node in a task of its own (`audit_load_8 graph`: 7 distinct tasks for main + 6 node runs). Every agent's `Verifier.verify`, every monitor retry and every contradiction check therefore starts in a task with no history. Hardness learned in one node never carries to the next. Many runs at once (a benchmark, or several research questions) are all newcomers to each other.

### Measurements

Setup: 2 slots and 2 slow places. Hard checks run to the timeout. Waits are given in units of one warm hard check (timeout 0.4 s, unit 0.40 s).

| Load | 8 hard runs | 12 | 16 |
|---|---|---|---|
| Round-7 harness: newcomer 0.1 s after runs start together (`together`) | 1.8 | 2.9 | 3.9 |
| Steady state, hard runs check one per fresh task (`fresh`) | 1.1 / 1.8 / 1.8 | 2.2 / 2.8 / 2.8 | 3.3 / 4.0 / 3.8 |
| SG32's harness (every hard run has completed a check first, `sg32`) | 0.1 | 0.7 | 0.1 |
| Round 7 (AUDIT-26), same load as the first row | 1.9 | 2.8 | 3.9 |

- **At the default 5 s timeout**, a 10-step easy verify in a new task, 50 ms after 16 hard runs started, took **20.29 s** (20.65 s in a second run). Alone it took 0.04 s. Round 7 measured 20.2 s.
- **With 12 runs of 3 or 10 checks per fresh task**, the newcomer waited 0.3–2.7 hard checks, depending on how many runs were on their first check at that moment.
- **The kill storm shows the same queue.** In `audit_load_7 storm`, the easy prober's worst wait was 2.90 s (round 7: 2.84 s).
- **The "lent no place" trade-off is bounded.** With known-hard runs (SG32's harness), a newcomer waits at most about one hard check (0.1–0.7 units here). At the default 5 s timeout that is up to about 5 s on each new verify's first check when every slot is stuck.

### Repros (both `perf`, about 4–5 s each)

- `test_a_newcomer_does_not_queue_behind_hard_runs_that_started_together`: the round-7 load with 12 hard runs. It asserts a wait of at most 1.5 hard checks. Observed 2.9–3.0, including under 4 burners.
- `test_a_newcomer_beside_runs_that_check_in_fresh_tasks_waits_one_hard_check`: the steady state with 12 runs, using the median of 3 newcomers. It asserts at most 1.5. Observed 2.8–2.9.

### Root cause

- `src/autolab/verification/solver.py` `SolverExecutor.run` keeps hardness per `asyncio.Task` (`_short` and `_hard` weak sets), so every fresh task is `new`.
- `_Slots._rank` gives new and short callers the same rank 1, and serves within a rank in arrival order.
- `_Slots._grant` lends places only to `short` callers.

### Suggested fix

- **Keep hardness per run rather than per task.** One option is a `ContextVar` that tasks created from a run inherit, which LangGraph node tasks do. Another is to key it on the `Verifier` or the trace. Then a retried hard verify is not a newcomer again.
- **For runs that really are new together,** stop unknown checks from filling every place. For example, cap first checks at `workers - 1` slots, or give a newcomer a lent place but move it to a slow place (or kill and requeue it) once it passes `_HARD_SLICES`.
- **Restore the round-7 load** in `newcomer_first_check_s`, or keep both loads, so the repro covers runs that start together.

---

## 2. Medium: a process forked after a `Verifier` is built can never check (SG32 regression)

**Impact.** SG32 starts the default workers in `Verifier.__init__`. The process-wide `Workers` pool then holds idle `Worker` objects whose processes are children of the builder.

A process forked after that inherits the pool. Each check pops one of the inherited workers, and `worker.alive()` runs multiprocessing's `is_alive`. That asserts "can only test a child process", so every check raises `AssertionError`. Once the idle list is empty, `_start()` asks the inherited forkserver and gets `ChildProcessError: [Errno 10] No child processes`. The child can never check.

`check_entailment` raises rather than returning UNGROUNDED, so the agent's verify fails. Before SG32, building a `Verifier` started nothing, so the same pattern worked.

**Who hits it.** Any process that builds the graph or a `Verifier` and then forks:
- a prefork server (`gunicorn --preload`, Celery prefork) with `build_graph()` at import;
- `multiprocessing.Pool()`, whose default start method on Linux 3.11 is `fork`.

Processes that make a first check before forking had this problem before SG32 too. SG32 widens it to merely constructing a `Verifier`.

### Measurements

`audit_load_8 fork`: build `Verifier(FakeLLMClient())`, wait for the 4 pre-started workers, `os.fork()`, then make 6 easy checks in the child.

| Tree | Child's 6 checks |
|---|---|
| HEAD (`f09ea60`) | 4 × `AssertionError: can only test a child process`, then 2 × `ChildProcessError` |
| `41348b0` (before SG32) | 6 × VALID (first 0.21 s, then 0.00–0.01 s) |

The parent's own checks stay VALID.

### Repro

`test_a_process_forked_after_a_verifier_is_built_can_still_check` runs the scenario in a fresh interpreter and asserts every check is `VALID entailed`. It is deterministic and takes about 1 s.

### Root cause

- `src/autolab/verification/vericot.py` `Verifier.__init__` calls `default_executor()`, and `SolverExecutor.__init__` pre-starts `_workers()`.
- `solver_workers.Workers` has no fork handling: `acquire` calls `worker.alive()` on processes the caller does not own, and `_context()` caches the parent's forkserver context.

### Suggested fix

- In `Workers`, remember the owning pid. On a mismatch, drop the inherited idle workers and forkserver context without touching them, and start fresh ones. Use `os.register_at_fork(after_in_child=...)` or a pid check in `acquire` and `_start`.
- Also reset `_default_executor` and the module's `_shared_workers` in the child.

---

## 3. Medium: event-loop work in the formalizer grows with the vocabulary (outside SG33's budget)

**What SG33 bounds.** SG33 bounds the probe parses of one reply. The worst admitted reply now costs 23–50 ms (table below), so the AUDIT-26 #2 shape is fixed.

**What it does not bound.** `Vocabulary.merged` still does work proportional to the vocabulary the reply joins, all on the event loop:
- `out = dict(self.declarations)` copies the whole vocabulary.
- For **every declared line**, `sorts = tuple(d for n, d in out.items() if d.startswith("(declare-sort") ...)` scans the whole vocabulary. That is lines × vocabulary `startswith` calls. In a profile of a 100-step trace, this generator was 17.8 of 23.2 s (60.5 M `startswith` calls).
- `Formalizer.formalize` then tokenizes and parses the full vocabulary on every step.

**Facts bypass the per-reply budget.** `Verifier.verify` merges each fact separately (`formalizer.vocabulary.merge(fact.declarations)`), with one `measurement_facts` entry per measurement. That is quadratic in the number of measurements, at verify start, on the loop, and outside the per-reply budget. A single merge of all facts would be refused by that same budget, which allows about 133 constants.

### Measurements (`audit_load_8 vocab`)

| Load on the loop | Time |
|---|---|
| The same 100-constant reply merged into 0 / 5 000 / 10 000 / 20 000 symbols | 40 / 63 / 71 / 123 ms (another run: 25 / 48 / 77 / 125 ms) |
| 750 / 1 500 / 3 000 / 6 000 facts merged at verify start | 186 / 674 / 1 222 / **3 480 ms** (another run: 178 / 397 / 1 031 / 2 776 ms) |
| 100-step trace, each step an admitted 110-constant reply | step 1: 26 ms, last: **224 ms**, total 8.0 s |
| 50 steps × 110 constants | last 73 ms, max 120 ms, total 3.0 s |
| 400 steps × 10 constants | max 27 ms, total 3.25 s |

- **Every one of those milliseconds blocks the loop.** LLM calls, HITL timers, cancels and every other run in the process stop while it runs.
- **The step budget.** The 50 ms p95 step budget is exceeded from about 5 000 symbols onwards.
- **Who hits it.** A plate-reader result of 1 536 wells, or a few such reads, puts a verify at the 0.4–3.5 s rows. Today's simulator emits one measurement per instrument, but `ExperimentResult.measurements` is unbounded and the `HardwareAdapter` is meant for real instruments.

### Repro

`test_merging_a_reply_does_not_scale_with_the_vocabulary_it_joins` (`perf`) merges the same 100-constant reply into an empty vocabulary and into one of 20 000 symbols. It asserts the second costs at most 2× the first. Observed 5.8× (134 against 23 ms), and 3.7× under 4 burners (280 against 76 ms). The test takes about 1 s.

### Root cause

`src/autolab/verification/formalizer.py`, `Vocabulary.merged`:
- the per-line `sorts` scan over `out.items()`;
- the full `dict` copy per call;
- `Verifier.verify` merging facts one at a time;
- `Formalizer.formalize`: `_tokens(self.vocabulary.smt())` and the full-vocabulary `parse_smt2_string` on every step.

### Suggested fix

- Keep a set (or dict) of declared sort names beside the declarations. Look up only the names a line mentions.
- Merge the facts in one call, charged against a separate fact budget, or through a trusted path with no probe, since facts are built in code.
- Keep the used-token set incrementally instead of re-tokenizing the vocabulary on every step.
- Longer term, run the formalizer's parse in a solver worker (ADR 0009).

---

## 4. Low: the first check's start-up wait is recorded nowhere

**What SG32 changed.** Workers now start in `Verifier()`, so start-up is hidden whenever the LLM's formalization takes longer than about 0.3 s. With 100 ms per LLM call, the first check took 17 ms.

**What still happens.** A verify that starts at once with a fast LLM still waits for the forkserver and the first boot. Examples are a cached LLM, `FakeLLMClient`, a short trace, error-injection runs, or the first test in a process. Since 8f3cf14 that wait is subtracted from `solver_ms`, and `StepVerdict` has no other timing field (`details, reason, solver_ms, step_index, verdict`). The trace shows a 15 ms step that took 0.3 s.

### Measurements (`audit_load_8 cold`, fresh process, 2-step verify right after `Verifier()`)

| Run | First check wall | Recorded (`solver_ms`) |
|---|---|---|
| 1 | 289 ms | 24 ms |
| 2 | 304 ms | 27 ms |
| 3 | 242 ms | 17 ms |
| under 4 burners | 418 ms | 13 ms |

Building the `Verifier` itself takes 0.4–0.8 ms on the caller. The pre-start thread caused no loop stall over 5 ms in 2 of 2 runs (`lag_child`).

### Repro

`test_the_first_checks_start_up_wait_is_recorded_in_its_verdict` (`perf`, about 1 s) asserts the first verdict's `*_ms` fields add up to at least half of the first check's wall. Today they add up to 5–9 %.

### Suggested fix

Record the wait in the verdict, for example a `queue_ms` or `startup_ms` field, as AUDIT-26 #3 suggested. `_worker.overhead_s` already measures it.

---

## SG31–SG33 claims, re-measured

| Claim | Re-measured now | Status |
|---|---|---|
| SG32: a newcomer never queues behind every hard caller (AUDIT-26 #1) | Holds only once every hard run has completed a check. Runs that start together: 1.8 / 2.9 / 3.9 hard checks; 20.3 s at 5 s | **Does not hold for its original load (#1)** |
| SG32: workers start at `Verifier()` (AUDIT-26 #3) | A worker starts before the first check in both `-c` and path-run mains. Cold first verify: 0.76 s (`-c`) and 0.84 s (path-run) against 0.735 s warm, with 100 ms LLM calls. The judges test runs cold in 0.750 s, with 0/7 warm runs over 0.8 s | Holds. Side effect: #2. The wait is unrecorded: #4 |
| SG32: workers do not re-run `__main__` (AUDIT-26 #4) | 1 process ran the main's imports for 3 boots (round 7: 4). Sequential boot is 5–6 ms with an `audit_load_6` main (round 7: 815–1 458 ms) and 4–21 ms with a `pytest` main. 4 parallel boots take 0.02–0.03 s | Holds |
| SG32 / SG31: unguarded main (AUDIT-26 #5) | `test_audit_26` passes | Holds |
| SG33: probe work bounded per reply | The worst admitted reply per shape: 133 constants 27 ms, 62 unary functions 37 ms, 17 × 4-ary 29 ms, 8 × 8-ary 23 ms, 77 functions over a sort 40 ms, 85 unary sorts 50 ms. A binary `Array` tree of depth 7 takes 3 ms. Assertions are not budgeted but parse linearly: 4 000 nested `not` 13 ms, 4 000 nested `+` 35 ms, 1 000 nested `forall` 6 ms | Holds per reply. Growth with the vocabulary is #3 |
| SG31: sequence-tagged replies | A raw pipe round trip is 58 µs median (p99 127 µs). An easy check takes 2.69 ms in a worker against 2.29 ms in-process (+0.4 ms), and in another run 3.42 against 3.04 ms | Negligible cost |
| SG29 / SG31: kill storm | 20 unpolled checks at a 0.5 s timeout: all 20 killed, wall 3.12 s, fd delta 0, no zombies, `live` 4 | Holds |

## Checked and not a finding

**Starvation of hard runs by a stream of new tasks.** SG32's newcomer rank does not starve known-hard runs. Aging (`passed ≥ workers`) still promotes a hard waiter after two grants to later arrivals.

`audit_load_8 starve` measured 2 + 2 places, timeout 0.4 s, over 8 s. The longest hard wait is the wait beyond the hard check's own timeout:

| Hard runs | Stream of new tasks | Hard checks done | Longest hard wait |
|---|---|---|---|
| 4 | none | 76 | 0.3 units |
| 4 | 20 easy/s | 68 | 0.3 |
| 4 | 100 easy/s | 46 | 1.3 |
| 4 | 5 hard/s | 40 | 1.9 |
| 8 | none | 75 | 1.2 |
| 8 | 20 easy/s | 64 | 1.6 |
| 8 | 100 easy/s | 48 | 2.8 |
| 8 | 5 hard/s | 39 | 3.4 |

- **The throughput loss is capacity.** The 100/s easy stream uses about one slot of capacity while 4 CPUs run 4 Z3 workers, so the hard runs' share drops by that much. They are not starved.
- **Under real overload** (20 hard newcomers/s, which is twice the capacity), every waiter ages to rank 0. The queue then degrades to FIFO by arrival, which is fair, and the hard runs' longest wait grew to 10–12 units in an earlier run, as any FIFO under overload does.

**Other checks.**
- **Start-up loop cost.** Building the default executor causes no event-loop stall over 5 ms. The prestart thread only waits on process start.
- **Generic `Workers` with a `__main__` target.** Since SG32 a worker cannot unpickle a target defined in `__main__`: it reports "failed to start" while its boot traceback is printed. Production's target is `solver._solve`, so this only affects ad-hoc use of `Workers`.

## CLAUDE.md budgets

- **50 ms p95 per step.** Holds for ordinary traces. It is exceeded on the event loop by formalization:
  - from about 5 000 vocabulary symbols (#3);
  - at verify start with more than about 200 facts (#3).
- **10-step trace in < 30 s.**
  - Holds warm (0.04 s).
  - An easy verify that starts within one timeout of 16 hard runs takes 20.3 s at the default timeout (#1). That is 68 % of the budget, and it grows linearly with the number of hard runs.

## Bottom line

- **What SG31–SG33 fixed.**
  - Boot cost per worker: 5 ms, against 0.1–1.5 s before.
  - Unguarded mains.
  - The probe budget per reply: worst admitted reply 23–50 ms, against 1.8–4.2 s.
  - Start-up hidden behind LLM latency.
  - The sequence tags cost nothing measurable.
- **What is still open.**
  - The high finding from round 7 is not fixed for the load it was written against. In the orchestrator every verify is a fresh task, so that load is the normal case.
  - SG32's earlier start-up added a fork hazard (#2).
  - The remaining loop blocking has moved from the probe to work proportional to the vocabulary (#3).

## How this was measured

- **Harness.** `scripts/audit_load_8.py`. It reuses `audit_load_7`'s `run_child`, `burners`, `hard_check_s` and `newcomer_first_check_s`.
  - Child processes are path-run scripts in a temporary directory.
  - Burners are `python -c "while True: pass"` subprocesses, always killed in a `finally`. `ps` was checked afterwards and showed none left.
- **Calibration.** Timing repros compare the machine against itself: one warm hard check (#1), the same merge into an empty vocabulary (#3), and the first check's own wall (#4). #2 is deterministic.
- **Version comparison.** For #2, `git archive 41348b0` was extracted into the scratchpad and run with the same venv, with `sys.path` pointing at its `src`.
- **Scratch experiments.** LangGraph task identity, loop-lag sampling and the profile of #3 were scratch scripts. Their results are reproduced by the harness sections `graph`, `cold` and `vocab`.

## Verification

- **Lint and types:** `make lint typecheck` reports ruff clean (155 files) and mypy "no issues found in 141 source files".
- **New tests:**
  - `uv run pytest tests/audit/test_audit_30.py -q -p no:randomly` gives 5 xfailed in 11.0 s.
  - With `--runxfail`, 5 failed on their assertions, both quiet and under 4 CPU burners.
- **Round 7 repros:** `uv run pytest tests/audit/test_audit_26.py -q -p no:randomly` gives 6 passed in 10.0 s (their xfail markers were removed by SG32/SG33).
