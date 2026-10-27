# AUDIT-26: Performance under load, round 7

Auditor: independent. I wrote none of the audited code or the earlier audits. Base: `origin/claude/sg30-fair-slots` @ `7123eed` (SG28, SG29 and SG30 merged).
Machine: 4 CPUs, Linux, Python 3.11.17, z3 4.16.0, cgroup v1 with no CPU quota. Defaults here: `SOLVER_WORKERS` = 2, `SOLVER_SLOW_WORKERS` = 2, `_IDLE_WORKERS` = 6. Kernel autogroup is on, so burners are started from the measuring process's own session.
All runs use `FakeLLMClient`, subclasses of it, or stubs. Nothing touches the network. No source file or existing test was changed.

**Load caveat.** Other agents' suites and mutation runs shared the machine for most of the audit (load average 2–9 on 4 CPUs). The machine rebooted mid-audit, and the final repro runs were made afterwards at load 2.4–5.3. Every timing repro compares the machine against itself: one warm hard check, one Z3 parse of the same text, or the same probe at half the size. A faster CI runner scales both sides.

- **Repros:** `tests/audit/test_audit_26.py` has 6 tests. Each is marked `xfail(strict=True, reason="AUDIT-26 #n")` and asserts the correct behaviour. The three wall-clock tests also carry `perf`. With `--runxfail -p no:randomly`, all 6 failed on their assertions in 4 of 4 runs, about 13 s each, including one run under 4 CPU burners (21 s). Without `--runxfail`, all 6 xfail.
- **Measurements:** `uv run python -m scripts.audit_load_7 [--burn N] [section ...]`. The sections are `judges`, `cold`, `boot`, `storm`, `newcomer`, `probe`, `unguarded`, `leaks` and `wake`. Version comparisons ran the same scenario against `git archive` trees of `94976ff` (before SG28/SG29, "pre"), `cccb368` (SG29) and HEAD, all on the same venv, with `PYTHONPATH` pointing at each tree.

| # | Severity | Title |
|---|---|---|
| 1 | **High** | A new task's first check still queues FIFO behind every hard caller. A 10-step easy verify beside 16 hard runs takes 20.2 s (0.05 s alone). |
| 2 | **High** | SG28 bounds one line, not one reply. A legal 15 KB reply blocks the event loop for 1.8–3.2 s, and a nested sort is quadratic (16 KB: 4.2 s). |
| 3 | Medium | Worker start-up is on the first check's critical path. The default executor, and so the pre-start, is created only at the first check. This is what fails `test_judges_overlap_but_formalization_stays_ordered`. |
| 4 | Medium | The forkserver never preloads a path-run `__main__` (CPython 3.11), so every worker boot re-imports the main script: 103–159 ms under `pytest`, 0.8–1.5 s for a heavy script. |
| 5 | Low | An unguarded main makes every check UNGROUNDED with the message "worker killed past the timeout", in 0.3 s, and costs a process start per check. |

In total: two high, two medium and one low finding. None is critical.

---

## Verdict on `test_judges_overlap_but_formalization_stays_ordered`

**It is a real regression, not only test calibration.** Worker start-up sits on the critical path of a process's first verify (findings 3 and 4). The bound is also too tight for a loaded machine, but that is the smaller and pre-existing part.

The test was run on its own (fresh pytest process, `-p no:randomly`) three times per tree (`scratchpad alone.sh`):

| Tree | Quiet (load 2.4–2.7) | 4 CPU burners (load 2.3–5.3) |
|---|---|---|
| pre (`94976ff`) | passed 3/3 | 0.824, 0.893 s (failed), 1 passed |
| SG29 (`cccb368`) | **1.130, 1.048, 1.068 s** (failed 3/3) | 1.723, 1.826, 1.509 s |
| SG30 (HEAD) | **1.080, 1.020, 1.100 s** (failed 3/3) | 1.866, 1.951, 1.903 s |

- **Inside its own test file it passes.** `test_solver_executor.py` reports 28 passed, because earlier tests have already started the workers.
- **The bound is 0.8 s.** The scenario spends 0.7 s in LLM latency, which leaves 0.1 s for twelve checks.
- **Warm runs fail at about the same rate on every tree under burners.** Eight verifies in one pytest process, under 4 burners, first one cold:

  | Tree | Cold | Warm walls | Warm over 0.8 s |
  |---|---|---|---|
  | pre | 0.907 s | 0.784–0.935 s | 6 of 7 |
  | SG29 | **1.660 s** | 0.782–0.822 s | 3 of 7 |
  | HEAD | **1.860 s** | 0.792–0.892 s | 5 of 7 |

  So the warm failures under burners are calibration. The cold verify costs +0.75–0.95 s on SG29/SG30 and nothing extra on pre.
- **Warm per-check IPC is not the cause.** Warm overhead (check phase minus `solver_ms`) was 2–36 ms per verify on all trees.
- **8f3cf14 hides the cold cost in the trace.** HEAD's cold verify reported a `solver_ms` total of 0.036–0.056 s while its wall included the 0.5–1.4 s start-up. On SG29 the same cost showed up in `solver_ms` (0.77–1.15 s).

**Suggested handling.**
- Fix finding 3: start the workers at `Verifier` construction or at import. Then the test passes on its own when the machine is quiet.
- Keep the bound machine-relative. For example, bound it at `(6 + 2) * delay_s + k ×` a measured warm check, rather than leaving 0.1 s for twelve checks.

---

## 1. High: a new task's first check queues behind every hard caller

**Impact.** SG30 ages waiters: a caller passed over `workers` times is served next. But a pass is only counted when someone who arrived *later* is served first. A task with no check yet (`task not in self._short`) ranks with the hard callers and is queued FIFO behind every hard caller already waiting. It also gets no lent place, because lent places go to short callers only. This is the remainder of AUDIT-22 #4: its first suggested fix, "treat a task with no last check as short", was not taken.

Each `Verifier.verify` that the orchestrator or an agent starts in a new task (an `asyncio.gather` of runs, a LangGraph node, `asyncio.wait_for`) is such a newcomer. The first step of every new run therefore waits out the hard queue. That is about `hard runs / (workers + slow_workers)` hard checks, at up to the 5 s default timeout each.

The same verify run in the old task, which has already checked, is fine. That is why the AUDIT-18 #4 load (`fast_trace_under_slow_runs`) does not see it: it verifies in the task that already ran the "alone" verify.

`audit_load_7 newcomer` (2 slots, 2 slow places; hard runs make back-to-back checks that run to the timeout):

| Hard runs | Timeout | Easy 10-step verify alone | In a new task | In the old task |
|---|---|---|---|---|
| 8 | 0.5 s | 0.03 s | 1.26 s | 0.12 s |
| 16 | 0.5 s | 0.03 s | 2.39 s | 0.23 s |
| 16 | **5.0 s (default)** | 0.05 s | **20.2 s** | (0.26 s in the AUDIT-18 #4 load) |

- **The first check alone, in units of one warm hard check (timeout 0.4 s):** 1.9 at 8 hard runs, 2.8 at 12, 3.9 at 16. It grows linearly with the queue.
- **The same shape appears during a kill storm.** In `audit_load_7 storm`, with 20 unpolled checks killed at 0.5 s, an easy prober in a new task waited up to 2.84 s for its first check, then a median of 7 ms.
- **This is pre-existing, not new in SG29/SG30.** With 8 hard runs at 1 s, a new task's easy verify took 4.04 s on pre, 2.30 s on SG29 and 2.12 s on HEAD. SG30 helps somewhat but does not remove it.

**Repro.** `test_a_new_tasks_first_check_does_not_queue_behind_every_hard_run`:
- Setup: 2 + 2 places, 12 hard runs at timeout 0.4 s, and a new task makes one easy check.
- Assertion: it waits at most 1.5 warm hard checks.
- Today it waits 2.8–3.0 (1.13–1.24 s). Calibrated on the machine's own hard-check wall, and it takes about 4 s.

**Root cause.**
- `src/autolab/verification/solver.py:720`: `short = task in self._short` is false for a task with no check yet.
- `solver.py:664-665`: `_rank` ages only on `passed`.
- `solver.py:680-682`: `passed` counts only grants to later arrivals.
- `solver.py:659`: lent places go to short callers only.

**Suggested fix.** Treat a task with no last check as short (it has not shown it is hard), so it may take a lent place and rank ahead of hard callers. Or keep the hardness per run (for example per trace) instead of per task. A long-lived hard run is then still demoted after its first hard check, but a new run is not queued behind the hard queue.

---

## 2. High: SG28 bounds one declaration, not one reply

**Impact.** SG28 caps each declaration at `MAX_ARITY` = 32 arguments, so one line's probe is bounded (AUDIT-22 #1's repro passes). But the probe still makes up to 2k + 2 = 66 Z3 parses per declaration. It runs synchronously in `Vocabulary.merged`, inside `Formalizer.formalize`, on the event loop. A reply is bounded only by the client's `max_tokens` (4 096, about 16 KB), so one legal reply buys thousands of parses.

Separately, Z3's own parser is quadratic in the nesting depth of a sort. A declaration whose argument is a deeply nested `Array` sort costs seconds to parse even once, and the probe parses it several times.

While either runs, every run in the process stops: LLM calls, HITL timers, cancels and other verifies.

`audit_load_7 probe` (caches cleared, load 2.8–3.0):

| Reply | Size | `merged` | Parses | One parse of the same text |
|---|---|---|---|---|
| 100 declarations × 32 Int args (about 3 500 tokens) | 14.9 KB | **1.8–3.2 s** | 6 800 | 2.6 ms (×700) |
| 300 × 8 args | 16.9 KB | 0.94–1.8 s | 6 000 | 1.8 ms (×516) |
| 600 × 1 arg | 17.5 KB | 0.35–0.71 s | 2 400 | 1.7 ms (×205) |
| one `(Array Int …)` sort nested 400 deep | 4.7 KB | 0.29–0.34 s | 4 | |
| nested 800 deep | 9.4 KB | 1.2–1.5 s | 4 | |
| nested 1 400 deep | 16.4 KB | **4.2 s** | 4 | |

- **The parse profile:** 6 800 calls to `Z3_parse_smtlib2_string` take 2.3 of the 2.9 s. The deep sort spends 1.60 of its 1.63 s in 4 parses.
- **How it compares to AUDIT-22 #1** (critical, 12 s at one 4 KB line): the worst case is now bounded by `max_tokens`, at about 3–4 s per formalize call. But that is per step, per run, on the event loop, and the calls add up across concurrent runs.

**Repro.**
- `test_probing_a_reply_of_many_declarations_costs_a_few_parses_of_it` merges the 100 × 32 reply and asserts it costs at most 50 single parses of the same text. Today it costs 238–910 times as much (1.9–3.2 s). Calibrated, about 4 s.
- `test_probing_a_nested_sort_is_linear_in_its_depth` asserts that doubling the depth from 400 to 800 at most multiplies the time by 2.5. A refusal counts as cheap, so a depth cap also passes. Today the factor is 3.2–4.8. About 4 s.

**Root cause.**
- `src/autolab/verification/formalizer.py:75-85`: per line, arity is capped, then `_applied_builtin` makes 2k + 2 parses.
- `formalizer.py:291-350`: the probe, one parse per numeric variant.
- No bound on the number of declarations per reply, and none on sort depth.
- All of it runs on the event loop: `Formalizer.formalize` calls `Vocabulary.merged`.

**Suggested fix.**
- Probe all of a reply's declarations in one parse, by declaring every variant under fresh names in one document. Or bound the declarations per step (a step needs a handful).
- Cap the nesting depth of a sort, for example at 8, and refuse deeper ones with `FormalizationError`.
- Longer term, run the formalizer's Z3 parses in the solver workers, which ADR 0009 already provides. The parse is then off the loop and killable.

---

## 3. Medium: worker start-up is on the first check's critical path

**Impact.** ADR 0009 says "Start-up off the hot path: `SolverExecutor()` pre-starts workers on a background thread". But `Verifier._check` creates the process-wide executor lazily: `self._executor or default_executor()`, at `vericot.py:215`. That happens only when the first check is already being submitted, after the whole formalization.

So the forkserver start (a new interpreter importing z3 and `solver`) and the first workers' boots all land on the first check of every fresh process. Finding 4 multiplies the cost for path-run mains. 8f3cf14 then excludes the wait from `solver_ms`, so the reasoning trace (the product, CLAUDE.md §8) shows a 30–50 ms step that took a second.

`audit_load_7 cold` (fresh interpreter; 6-step verify, 100 ms per LLM call; warm = best of the next 3):

| Main | Tree | First verify | Warm | Cold cost |
|---|---|---|---|---|
| `python -c` (nothing to re-import) | HEAD | 0.97–1.02 s | 0.73 s | +0.25–0.29 s |
| path-run script (imports `scripts.audit_load`, so anthropic) | HEAD | 2.09–3.44 s | 0.73 s | **+1.4–2.7 s** |
| same script | SG29 | 1.86 s (`solver_ms` 1.15 s) | 0.73 s | +1.13 s |
| same script | pre | 0.732 s | 0.725 s | +0.007 s |

- **In every run, no worker had been started when the first check was requested.** `start_before_check` was False.
- **The pytest numbers are in the verdict section above.** The first verify costs +0.3–0.4 s quiet and +0.75–0.95 s under 4 burners.
- **Who pays it:** `autolab run` (one-shot CLI) pays it on every run, and every test that is the first to verify in its process pays it.

**Repro.** `test_workers_start_before_a_fresh_process_requests_its_first_check`:
- A fresh `python -c` interpreter runs a 6-step verify with 100 ms LLM calls, which gives 0.7 s of formalization first.
- It records when `Workers._start` and `Verifier._check` are first entered, and asserts a worker start came first.
- Today the start always comes second. Deterministic, about 3 s.

**Root cause.**
- `src/autolab/verification/vericot.py:215`: the default executor is created at the first check.
- `solver.py:710-713`: the pre-start is tied to `SolverExecutor()` construction.
- `solver.py:249-253` (8f3cf14): start-up is excluded from `solver_ms` without being reported anywhere else.

**Suggested fix.**
- Pre-start the shared workers when a `Verifier` is constructed, or at `vericot` import behind a cheap guard, so the formalization's LLM time hides the start-up.
- Record the start-up wait in the step's trace (for example `queue_ms` or `startup_ms`) rather than dropping it.

---

## 4. Medium: every worker boot re-runs a path-run main's imports

**Impact.** ADR 0009 says "The forkserver imports [the main module] once". The code asks for that with `set_forkserver_preload([..., "__main__", ...])`. On CPython 3.11 that request is a no-op:
- `ForkServer.ensure_running` keeps only the keys `{'main_path', 'sys_path'}` from `spawn.get_preparation_data()`.
- That function emits `init_main_from_path`, never `main_path`.

So the forkserver never imports a path-run main. Each new worker's `spawn.prepare` runs it from scratch with `runpy.run_path(..., "__mp_main__")`, re-importing all of its top-level imports.

`_main_imports()` only covers `-m` mains. Path-run mains are not covered, and they include `uv run pytest` (`.venv/bin/pytest` imports `pytest`) and any `python scripts/x.py`.

`audit_load_7 boot` (forkserver warm unless stated; boots of a fresh worker until ready):

| Main's top-level imports | Forkserver + first boot | Sequential boot | 4 parallel boots |
|---|---|---|---|
| none (`-c` or a light script) | 0.30 s | 5–11 ms | 0.02–0.05 s |
| `from autolab.cli import main` (the `autolab` console script) | 0.21–0.40 s | 6–27 ms | 0.03–0.10 s |
| `import pytest` (`bin/pytest`) | 0.33–0.48 s | **103–185 ms** | 0.24–0.95 s |
| `import anthropic` | 1.0–1.1 s | 588–955 ms | 2.5–2.7 s (2–4) |
| `import scripts.audit_load_6` | 1.05–1.83 s | **815–1 458 ms** | 2.1 s (4), 9.7 s (8) |

- **A marker module imported by the main ran in 4 processes for 3 boots.** The 4 are the parent plus each worker; the forkserver was not among them. `-X importtime` showed `anthropic` imported 14 times in one run against `autolab.verification.solver` twice.
- **It is on the critical path after every kill.** With a main importing `pytest`, a 20-check kill storm at 0.5 s took 4.73 s against 3.20 s with a light main. The time spent waiting in `Worker.ready()` summed to 5.1 s against 0.2 s, and the easy prober's worst wait was 4.39 s against 2.91 s.
- **It also compounds finding 3.** The pre-started workers boot this way, so the first check of a path-run process waits for 4 parallel re-imports.
- **The production CLI is light**, because `autolab.cli` imports lazily. The cost lands on the test suite, on scripts run by path, and on any future heavy entry point.

**Repro.** `test_worker_boots_do_not_rerun_the_main_scripts_imports`: a path-run main imports a marker module that logs its pid, and boots 3 workers. The test asserts at most 2 processes ran it (the parent and the forkserver). Today 4. Deterministic, about 2 s.

**Root cause.**
- `src/autolab/verification/solver_workers.py:143-146`: `set_forkserver_preload([..., "__main__", ...])` relies on CPython's `__main__` preload.
- `solver.py:435`: passes `"__main__"`.
- CPython 3.11 `multiprocessing/forkserver.py` (`desired_keys = {'main_path', 'sys_path'}`) against `multiprocessing/spawn.py` (`d['init_main_from_path']`).

**Suggested fix.** Workers do not need the main module at all: their target is `solver._solve`. So stop the child from re-running it:
- Start workers with a `Process` subclass or a preparation step that drops `init_main_from_path` and `init_main_from_name`.
- Or launch the worker loop as `python -c`/`-m autolab.verification.solver_workers` over a pipe (spawn of a fixed module).

Either way, the "main must be safe to import" requirement (finding 5) goes away too.

---

## 5. Low: an unguarded main is reported as a solver timeout

**Impact.** ADR 0009 requires a `__main__` guard. Without one, the main's top level runs again in each worker during `spawn.prepare`. Making a check there raises multiprocessing's bootstrapping `RuntimeError`, and the worker dies before it is ready.

`check_entailment` then returns UNGROUNDED / `timeout` / "worker killed past the timeout" after 0.31 s, with no worker ever killed for time. It discards and restarts a worker on every check, so each check costs a process start.

It fails closed, but the reason misleads an operator into raising timeouts. The only trace of the real cause is a traceback on stderr.

**Repro.** `test_an_unguarded_main_is_not_reported_as_a_solver_timeout`: an unguarded path-run script makes one easy check. The test asserts it is VALID, or at least not reported as a timeout. Today it is UNGROUNDED/`timeout`, "worker killed past the timeout". Deterministic, about 1 s.

**Root cause.**
- `src/autolab/verification/solver.py:233-237, 254-255`: a worker that never became ready (`booted` False) is reported as `_KILLED`.
- `solver_workers.py:83-93`: `ready()` cannot tell a boot crash from a kill.
- Finding 4: the worker runs the main at all.

**Suggested fix.**
- Report a boot failure as its own outcome (for example `SolverWorkerError("worker exited during start-up; is the main module guarded?")`, raised once) instead of `_KILLED`.
- Fixing finding 4 removes the cause.

---

## Earlier fixes and SG28–SG30 claims, re-measured

| Fix | Claim | Re-measured now | Status |
|---|---|---|---|
| AUDIT-22 #1 (SG28) | the probe is bounded | `test_audit_22` repro passes. One reply of many max-arity lines: 1.8–3.2 s; a nested sort: 4.2 s at 16 KB | **Holds per line, not per reply (finding 2)** |
| AUDIT-22 #2 (SG29) | no per-premise GIL hand-offs | Step p95 with 4 × 400 uncited Boolean traces: **11.6 ms** (was 151.7). Int 4 × 400: 47.1 ms (was 221.8). Int 1 × 400: 56.8 ms (was 105). All steps VALID | Holds |
| AUDIT-22 #3 (SG29) | timeouts and cancels hold for any shape | Repros pass. 20 unpolled 2.6 s checks at timeout 0.5 s: all 20 killed, storm wall 3.08–3.20 s, kill + replace 5 ms start + 16 ms stop each | Holds |
| AUDIT-22 #4 (SG30) | a new run is not starved by short callers | Repro passes (aging) | Holds against short callers; not against hard ones (finding 1) |
| AUDIT-22 #5 (SG29) | a cancelled slow check's place returns when it ends | Repro passes. A 20-check cancel storm of easy checks: next easy check 8.8 ms | Holds |
| AUDIT-22 #6 (SG29) | sized from the cgroup quota | Repro passes. This host has no quota: sizes 2 + 2 | Holds |
| ADR 0009 | "about 0.5 ms of IPC" per check | Trivial check, 200 runs: worker median 6.78 ms against 6.01 ms in-process (+0.8 ms). A 0.53 MB document: 15.86 s against 15.86 s (IPC negligible next to Z3's parse) | Holds |
| ADR 0009 | start-up off the hot path | No worker exists until the first check (finding 3). Boots re-import a path-run main (finding 4) | **Does not hold** |
| ADR 0009 | cleanup | SIGKILL or SIGTERM of the parent mid-check: the forkserver, resource tracker and worker were gone after 0.35–0.60 s (1.6 s under load). After storms: fd delta 0, no zombies among our descendants. `shutdown_default_executor()` leaves `live()` 0 | Holds |

## Checked and not a finding

- **Worker memory.** One worker reused for 3 000 mixed VALID/INVALID checks went from 42 to 60 MB in one run, flat from the first fifth onwards, and from 88 to 104 MB in another, also flat. There is no growth per check.
- **Churn beyond the idle cap.** 4 event loops × 4 tasks on one executor made 320 checks with 8 worker starts and 6 stops. With separate executors there were 16 starts and 14 stops. This only matters together with finding 4.
- **Kill grace under load.** 40 concurrent 20 ms checks at a 50 ms timeout, under load 5: 3–6 were killed at the 10 ms grace, where Z3 would also have returned `timeout`. The verdict is the same and the only cost is a replacement. At the default 5 s timeout the grace is 0.5 s.
- **`_Slots._wake`.** It sorts all waiters on every grant, which is quadratic: 0.09 s of loop time for 1 000 waiters and 1.5–2.0 s for 4 000. Each verify has at most one check waiting, so that takes thousands of concurrent runs. This predates SG30.
- **Hardness by run time (SG30).** Under 4 burners, an easy verify's 12 checks summed to 57–161 ms of `solver_ms`, so no single check reached the 200 ms (`_HARD_SLICES × _SLICE_S`) line.

## CLAUDE.md budgets

- **50 ms p95 per step.** `scripts/bench_verifier.py --runs 5` (load 3.2): step p95 4.76 ms, max 7.06 ms, within budget. Concurrent long uncited traces:
  - Boolean 4 × 400: 11.6 ms.
  - Int 4 × 400: 47.1 ms; Int 1 × 400: 56.8 ms.
  - Real 1 × 400: 86.7 ms; Real 4 × 400: **179.5 ms**. The Real pools still take SG27's loading path, as in AUDIT-22, so this is pre-existing.
- **10-step trace in < 30 s.**
  - Warm: 0.03–0.05 s of solver and formalizer time.
  - Beside 16 hard runs in a new task: 20.2 s (finding 1).
  - A formalize step can block the loop for 3–4 s (finding 2).
  - A fresh process's first verify pays 0.25–2.7 s of start-up (findings 3 and 4).

## What held up

- **SG29's bounding.** Kills land at timeout + grace for unpolled shapes. Cancels kill at once. Replacements are cheap with a light main (5 ms start), and helper processes do not outlive a killed parent.
- **SG29 removed the GIL penalty.** Long Boolean and Int traces under concurrency run 4–13× faster than in round 6.
- **No resource leaks.** No fd, zombie, process or worker-memory growth in any storm or long run.
- **IPC.** It is under 1 ms per check and negligible for megabyte documents.
- **SG30 aging.** It bounds starvation by short callers (the AUDIT-22 #4 repro passes), and its cost is negligible at realistic queue lengths.

## How this was measured

- **Harness.** `scripts/audit_load_7.py`:
  - Child processes are generated as path-run scripts (or `-c`) in a temporary directory, so the main module is controlled.
  - CPU burners are `python -c "while True: pass"` subprocesses started by `burners(n)` (`--burn N`). They are always killed in a `finally`, and `ps` was checked afterwards.
- **Cross-version runs.** These used `git archive` trees of `94976ff`, `cccb368` and HEAD in the scratchpad, with the same venv and `PYTHONPATH=<tree>/src:<tree>`.
- **The known test.** It was run alone with `pytest -p no:randomly -p no:cacheprovider -o addopts=` from each tree, three times quiet and three times under 4 burners. The cold/warm split ran the test's own scenario eight times in one pytest process.
- **Calibration.** Timing repros are read in units the machine measures in the same run:
  - finding 1: a warm hard check;
  - finding 2: one parse of the same text, and the same probe at half the depth.

  Findings 3–5 are deterministic (event order, an import count, an outcome).

## Verification

- **Lint and types:** `make lint typecheck` reports ruff clean (151 files formatted) and mypy "no issues in 138 source files". `uv run mypy scripts/audit_load_7.py` is clean.
- **New tests:** `uv run pytest tests/audit/test_audit_26.py -q` gives 6 xfailed in 13.5 s. With `--runxfail -p no:randomly`, 6 failed on their assertions in 4 of 4 runs, one of them under 4 CPU burners.
- **Earlier audits:** `uv run pytest tests/audit/test_audit_22.py tests/audit/test_audit_18.py -q -p no:randomly` gives 16 passed and 4 xfailed (AUDIT-22 #7 and AUDIT-18 #6, #8, #9, all still open).
