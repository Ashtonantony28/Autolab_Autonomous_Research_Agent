# AUDIT-43: API and SDK consistency, round 11

- **Auditor:** an independent agent. I wrote none of the audited code and none of the earlier audits.
- **Base:** `origin/claude/sg36-pause-lender` @ `3d2d7b4` (SG36: WIP commit `48c2a50` plus its completion). Branch `claude/audit-43`.
- **Scope:** what changed since round 10 (`git diff 1e537af..3d2d7b4 -- src scripts docs`):
  - `verification/solver.py`: `_Interrupt.freeze/thaw/frozen`, `_Slots._lenders/_balance/short_queued`, `run`'s give-way path (`freeze`, `_again`), `_Watchdog` and `_now`, and the removal of `_RESET_SHARE` and of Z3's `timeout`.
  - `verification/solver_workers.py`: `Worker.pause/resume/_signal/_poll`, `paused_s`, `_close_all`.
  - ADR 0009's SG36 amendment, and PLAN.md's SG36 row, Result and Log.
  - No agent, orchestrator, CLI, config, model or LLM-client code changed, so the SDK surface is as round 6 left it.
- **Re-checked:** AUDIT-31 #1, AUDIT-21 #1 / AUDIT-23 #1, AUDIT-39 #2 to #4, and the other open AUDIT-35/39 items.
- **Repros:** `tests/audit/test_audit_43.py` has 4 strict-xfail tests (#1 to #4). Each asserts the correct behaviour. Under `--runxfail` all 4 fail on their stated assertion, 3 of 3 runs. Without it the file gives 4 xfailed, 3 of 3 runs, in about 4 s. There are no timing assertions, no network and no LLM. #5 is docs and records only, with `file:line` citations.

## Summary

| # | Severity | Title | Repro |
|---|---|---|---|
| 1 | Medium | PLAN.md marks SG36 "done (PR pending)", but its Result says one Verify item ("`make test` green 5 times under 4 CPU burners") failed three rounds in a row and was not re-run. The plan's rules call for stop-and-report at 3 failures. The Log just says "SG36 done" | test |
| 2 | Medium | Each newcomer that gives way now waits frozen, holding a worker process (about 61 MB RSS) and a pool thread. With k hard runs that started together, k + 1 calls hold a worker on a 1-slot pool. Before SG36 the bound was 2 × workers + slow_workers. The ADR records the cost of one frozen check, not that the total has no bound | test |
| 3 | Low | "While a place is lent … Z3 checks never outnumber slots and slow places" does not hold once the lender ends (for example, it is cancelled) while its place is still lent. Its slot goes to the next caller, which runs beside the lent check with no lender left to freeze. This matches the measured "over 4 in 0–1 % of samples" that the ADR's "at most" leaves out | test |
| 4 | Low | Without SIGSTOP (non-POSIX), a newcomer that "gives way" is frozen in name only. It keeps running, and the next newcomer takes its lent place. Running Z3 checks grow with the number of newcomers: 4 on a 1-slot pool with 3 newcomers, against SG35's 2. The ADR's "the bound is not held" understates this, and AUDIT-34 #2's adapted repro would pass there | test |
| 5 | Low | Docs and records: stale "Z3's `timeout`" in `TIMEOUT_MAX_S` and the ADR; `_IDLE_WORKERS`, `_Slots.acquire`, `SolverExecutor` and `run` docstrings; two `pause` APIs with different meanings; superseded ADR bullets not marked; a non-conventional `wip:` commit; scripts edited outside Owns; no Resolution lines | citations |

**Count:** 0 critical, 0 high, 2 medium, 3 low.

**Carried over, still open:**
- AUDIT-31 #1 (medium). #2 here adds pressure: more live processes means more targets for the OOM killer.
- AUDIT-21 #1 / AUDIT-23 #1 (medium), open since round 5.
- AUDIT-39 #2 (low).
- AUDIT-31 #2 to #6 (low).
- AUDIT-39 #4 and AUDIT-35 #3 (low), in part. See "Earlier findings re-checked".

**Fixed by SG36 and confirmed here:**
- AUDIT-34 #2, #3.
- AUDIT-35 #2 (`fn` runs once).
- AUDIT-38 #1, #2.
- AUDIT-39 #1, #3.
- AUDIT-40 #5, #6.

**Why nothing is High.**
- **Soundness held.** Every new path fails closed:
  - The watchdog's interrupt gives `unknown`, which is UNGROUNDED.
  - Under a pause the parent's budget and the worker's budget both move by the same shared total.
  - A frozen check killed at shutdown or cancel returns "interrupted".
  - I found no route to VALID for a check that is not entailed.
- **The CPU bound SG36 was asked for holds in the cases it names.** #2 is about memory, processes and threads, not CPU. #3 and #4 are edge cases: a cancelled lender, and a platform the code never tests.

---

## 1. Medium: SG36 is "done" with a Verify item failing three rounds in a row

**What the plan says.**
- **Rules** (`PLAN.md:9`): "**Stop and report** if a task fails its verification 3 times, or if a fix needs a change to an SG0 interface. Do not work around it."
- **SG36's Verify list** (`PLAN.md:477`): "`make lint typecheck test` · `make test` green 5 times under 4 CPU burners · error injection (34/34, 26/26) · bench · the AUDIT-38 #2 /proc test 3 of 3."
- **SG36's Result** (`PLAN.md:479`): "Open: `make test` under 4 CPU burners failed in 1–2 of 5 runs, three rounds in a row, on load-sensitive tests. These were hardened, and the last failure … is fixed; the 5 loaded runs have not been repeated since."
- **SG36's row** (`PLAN.md:54`): "done (PR pending); the 5 loaded runs are not yet green (see Result)".
- **Log** (`PLAN.md:503`): "SG36 done (PR pending): …". This line has no caveat.

**Why it matters.**
- This is the same pattern as AUDIT-39 #1 for SG35, and it again goes against the plan's rule.
- The Result also records no error-injection numbers (34/34, 26/26), although they are a Verify item.
- The hardened tests and the `ProcessLookupError` fix landed in `3d2d7b4` with no green loaded run after them. So no record shows that the shipped code passes its own gate.

**Repro.** `test_a_sub_goal_is_done_only_once_its_verification_is_green`:
1. It checks that the Result records the failure ("failed in … of 5 runs, three rounds in a row").
2. It then asserts that the row's status is not `done`.

Observed, 3 of 3 runs:

```
AssertionError: done (PR pending); the 5 loaded runs are not yet green (see Result)
```

**Fix.** Two options:
- Re-run the 5 loaded runs and record the result.
- Or mark SG36 `blocked` and report, as the rules require. Then record the error-injection numbers, and make the Log line match the row.

## 2. Medium: frozen newcomers each keep a worker process and a pool thread, without bound

**What changed.**
- **Before SG36.** A newcomer that gave way was killed and re-run (SG34/SG35). So the calls holding a worker were at most slots + slow places + lent places = 2 × workers + slow_workers.
  - `_IDLE_WORKERS` is sized to that number (`solver.py:172`: "every slot, slow check and lent place of the default pool").
- **Since SG36** (`solver.py:945-956`). The newcomer is `freeze()`d and gives back its lent place. In `_again` it waits for a new place while still holding its worker process and its pool thread.
  - The thread comes from `self._pool.resize(1)  # kept while the check lasts, paused or not` (`:924`).
  - The next newcomer is lent the place (latest first) and starts its own worker. If none is idle, that is a fresh boot.
  - Once that newcomer shows it is hard, it is frozen too, and so on.
- **Result.** k hard runs that started together, the normal LangGraph case (AUDIT-30 #1), hold k + 1 worker processes and k + 1 pool threads until slots free up, one hard timeout at a time.

**What the docs say.**
- ADR 0009 SG36 (`0009-…md:81`) and the PLAN Result list the cost of one frozen check: "While it waits it holds a worker process and a pool thread". Neither says that the number of frozen checks is bounded only by the number of concurrent runs.
- Each such worker is a forkserver child with Z3 loaded.

**Measured** (scratch script, `SolverExecutor(1, 0)`, a stuck hard check plus k hard newcomers, `/proc/<pid>/stat` and `VmRSS`):

| k | calls holding a worker | stopped (`T`) | pool threads | RSS per worker |
|---|---|---|---|---|
| 4 | 5 | 4 | 5 | 60–61 MB |
| 8 | 9 | 8 | 9 | 60–61 MB |

SG35's bound for this pool is 2. At the default 2 + 2 places, 16 hard runs give about 17 Z3 processes, about 1 GB. SG35 had at most 6 in use.

**Repro.** `test_worker_processes_in_use_stay_within_the_pools_places`:
- The setup is 1 slot, 0 slow places, a stuck check and 3 hard newcomers.
- The test first checks that the CPU bound holds: 3 frozen, 1 running. Then it asserts that calls in flight and pool threads stay at or below 2.

Observed, 3 of 3 runs:

```
AssertionError: (4, 4)
```

**Fix.** Pick one:
- Bound the number of frozen give-ways, for example to `workers + slow_workers`. Past that bound, kill and re-run as SG35 did.
- Or let a frozen newcomer that has shown it is hard give back its worker, as SG35 did: it is queued hard anyway, and its progress is the only thing lost.
- At the least, state the bound, or its absence, in the ADR. Also re-size or re-document `_IDLE_WORKERS`.

## 3. Low: the CPU bound breaks when a lender ends while its place is lent

**What the docs claim.**
- `_Slots` docstring (`solver.py:732-734`): "While a place is lent, one lending slot's check is frozen, so lent checks run on its CPU and Z3 checks never outnumber slots and slow places".
- ADR 0009 SG36 (`0009-…md:79`): "so at most `workers + slow_workers` Z3 checks run at once". The same bullet reports "over 4 in 0–1 % of samples".

**What the code does.**
- A frozen lender cannot finish its check, but its call can still end:
  - it is cancelled, which kills the worker;
  - its worker dies;
  - or its check had left the worker when `freeze` ran.
- `run`'s `finally` then calls `unstick` and `release(False)` (`solver.py:970-973`). The slot is granted at once to the next waiter, while the lent place is still in use and `stuck` is now below `lent`.
- `_balance` has no lender left to freeze. So a slot check and the lent check run together on a 1-slot pool.
- This lasts until the new slot check sticks, at least one `_SLICE_S`. If the slot serves easy checks, it lasts for the whole lent check. A short caller's check on a lent place never gives way, so that can be a full timeout.

**Repro.** `test_checks_never_outnumber_the_slots_once_a_lender_ends`, deterministic, `_Slots` only:
1. It sets up 1 slot and a lender stuck and frozen for a short caller's lent place.
2. The lender then unsticks and releases, as `run`'s `finally` does for a cancelled lender.
3. A new caller arrives.

Observed, 3 of 3 runs:

```
AssertionError: (1, 1, 2)    # held, lent, running (unfrozen) places on a 1-slot pool
```

`test_solver_pausing.py:213` ("`a` ends with a place still lent: `b` lends it now") covers the case where a second lender exists. The case with no second lender is not tested.

**Fix.** When a lender ends with `lent > stuck`, hold its slot until the lent place comes back: treat the lent check as the slot's holder. Or soften "never" and "at most" in the docs to the measured bound.

## 4. Low: without SIGSTOP, giving way frees a place but not a CPU

**What the docs say.**
- ADR 0009 SG36 (`0009-…md:83`): "Signals are POSIX-only (`hasattr(signal, "SIGSTOP")`); elsewhere nothing is paused and the bound is not held."
- `solver_workers.py:39` and `Worker.pause` (`:178-182`): "a no-op where there is no SIGSTOP".

**What the code does without SIGSTOP.**
- `_Interrupt.freeze` still sets `_frozen`. `run` still releases the lent place and lends it to the next newcomer, while the "frozen" check keeps running.
- **Running checks grow without bound.** Before SG36, a give-way killed the worker, so the SG35 bound (2 × workers + slow_workers) held everywhere. Now running Z3 checks grow with the number of newcomers. This is a regression from SG35, not just "the bound [SG36 adds] is not held".
- **Run time is under-counted.** `ran_s` subtracts the "frozen" interval although the check ran (`solver.py:525-531`). So `solver_ms` and hardness under-count it.
- **The adapted repro passes vacuously.** AUDIT-34 #2's repro was adapted to count `Worker.pause` calls (`test_audit_34.py`, `_running`: `counted_pause` marks a worker paused even when `pause` was a no-op). So on such a host the AUDIT-34 #2 repro passes while the bound fails.

**Repro.** `test_without_sigstop_the_pre_sg36_bound_still_holds`:
- It simulates the platform with `monkeypatch.setattr(solver_workers, "_PAUSABLE", False)`.
- It uses the same scenario as #2: 1 slot, 3 hard newcomers.
- It counts calls in flight whose worker is not really stopped.

Observed, 3 of 3 runs:

```
AssertionError: 4    # assert 4 <= 2 * 1 + 0
```

**Impact.** Small. The forkserver-less (`spawn`) path is `pragma: no cover`, and CI is Linux only. But the ADR's sentence is the only place the trade-off is stated, and it understates it.

**Fix.** Where `_PAUSABLE` is False, keep SG35's behaviour: request (kill) and re-run, with `fn` pure. Alternatively, state the actual behaviour in the ADR. Also make `counted_pause` count only real pauses.

## 5. Low: docs and records

- **Stale "Z3's `timeout`".**
  - Since SG36, Z3's `timeout` is never set (ADR `:80`).
  - `TIMEOUT_MAX_S`'s docstring (`solver.py:169-171`) still says the cap keeps "the 2^31 ms a wait on the worker's pipe and Z3's `timeout` accept". ADR "Timeout cap" (`:52`) says the same.
  - Only the pipe wait still needs it. The watchdog's `Event.wait` does not.
- **`_IDLE_WORKERS`** (`solver.py:172-173`): "every slot, slow check and lent place". No longer the number of workers in use (see #2).
- **The immediate re-give-way, and where each docstring stands on it.**
  - **Where it is recorded.** A newcomer that gave way gives way again at once when a short caller queues. This is SG36's deviation from AUDIT-38 #1's suggested fix ("stop it again only at `_HARD_SLICES`, never again for a short caller"). It is recorded consistently, with the reason (the AUDIT-18 #4 load: 0.17 s against 0.32 s), in the ADR (`:82`), the PLAN Result, the commit message, the module docstring (`solver.py:27-29`), `_yields` and the comment in `run`.
  - **`_Slots.acquire`** (`:755-757`) still says only "once it shows it is hard, or after a slice while a short caller waits".
  - **`SolverExecutor`** (`:867-876`) does not mention freezing at all.
  - **`run`** (`:906`) still says the ms is "queueing excluded" only. It also excludes worker start, boot and replacement, and now frozen time (AUDIT-39 #4, carried).
  - **AUDIT-38.md** has no Resolution line recording that its suggested fix was not taken.
- **Two `pause` APIs.**
  - `_Interrupt.pause/resume/paused` (`solver.py:533-546`) only stop run-time accounting while a worker boots, and send no signal.
  - `Worker.pause/resume` (`solver_workers.py:178-191`) and the `_Pausable` protocol (`solver.py:499-501`) mean SIGSTOP/SIGCONT.
  - On the same `_Interrupt`, the SIGSTOP is called `freeze`.
  - Inside `check_entailment`, `interrupt.pause()` sits beside `attach`, which may really pause the worker. Renaming the accounting pair (for example to `waiting()`/`ran_again()`) would make the code read as it behaves.
- **Superseded ADR text not marked.**
  - SG34's "is stopped (its worker killed) … A stopped check is re-run, as every check is pure" (`:64`) and its "Cost … one worker restart" (`:66`) are superseded by SG36's "paused, not killed" (`:81`), which does not say "Supersedes".
  - SG35's "Not changed: the CPU bound" (`:75`) is superseded in prose (`:79`) but not struck. The ADR's own convention is `~~…~~` (`:44`).
  - SG32's `:60` bullet is still unstruck (AUDIT-35 #3, AUDIT-39 #4).
- **`solver_workers` API surface.** It still has no `__all__`. It adds the public `paused_s` and the module global `_paused: Any` (`:58`). The private-stdlib accesses `_children()` and `forkserver._forkserver._*` are still recorded nowhere by the `<3.13` pin (AUDIT-31 #4, #6, AUDIT-39 #4).
- **Commit hygiene.** `48c2a50` is titled `wip: SG36 pause lender (saved after container restart)`. `wip` is not a Conventional Commits type (CLAUDE.md §8), and CLAUDE.md asks that tests pass before every commit. `3d2d7b4` says squashing it "needs a force-push, which was not allowed". A squash merge of the PR would remove it from `main`.
- **Owns.** SG36's row owns `verification/solver.py`, `solver_workers.py`, `tests/unit/verification/test_solver_*.py` and `tests/audit/`. The SG36 commits also edited `scripts/audit_load_3.py` and `scripts/audit_load_4.py`, and added `scripts/audit_load_10.py`.
- **Resolution lines.** AUDIT-34, 35, 38, 39 and 40.md have none for the items SG36 flipped (AUDIT-34 #2, #3; AUDIT-35 #2; AUDIT-38 #1, #2; AUDIT-39 #1, #3; AUDIT-40 #5, #6). This is the same gap as AUDIT-39 #4. On the other side, SG36 is the first sub-goal since SG27 to have a Result paragraph.

---

## Earlier findings re-checked

| Item | Holds? |
|---|---|
| **AUDIT-31 #1** (medium): `worker_died` on a non-final step passes the trace | **Still open.** `test_audit_31.py::test_a_check_whose_worker_died_is_rerun_or_escalated_not_passed` fails under `--runxfail`. SG36 adds one more way to lose a worker: a frozen worker is still a live process for the OOM killer, and #2 multiplies them. |
| **AUDIT-21 #1 / AUDIT-23 #1** (medium): approving an escalated design re-checks cost on the design's own figures | **Still open.** No `agents/` or `orchestrator/` change. `test_audit_21.py::test_approving_a_figure_request_still_pauses_on_the_larger_figure` (both cases) and `test_audit_23.py::test_approving_a_disagreement_still_pauses_on_the_estimated_cost` fail under `--runxfail`. |
| AUDIT-39 #2 (low): a worker started at the fork is SIGTERMed by the child's exit | **Still open.** The repro fails under `--runxfail`. `_adopt` still keys on `_live` (`solver_workers.py:251-253`). |
| AUDIT-39 #3 (low): a stop between two checks of one `fn` | **Fixed.** The newcomer is frozen, not stopped, and a later `attach` starts paused (`solver.py:594-595`). The repro was adapted from `request` to `freeze` and passes. |
| AUDIT-39 #4 (low): docs and records | **Partly fixed.** AUDIT-35 #2's xfail is retired, consistent with the new "`fn` is called once". Still open: `run`'s ms, the private-stdlib record, stale ADR bullets, Resolution lines (see #5). |
| AUDIT-35 #2 (low): `run` may call `fn` twice | **Fixed.** `run` submits `fn` once (`solver.py:927`). |
| AUDIT-35 #3 (low): SG34 records | Still open in part: the ADR `:60` bullet and Resolution lines. |
| AUDIT-31 #2, #3, #4 (low) | Still open. Each repro fails under `--runxfail`. |
| AUDIT-31 #5, #6 (low) | Still open (see AUDIT-39's table; #6 overlaps #5 here). |

## What held up

- **The watchdog against Z3's own timeout.** Measured in-process (`_solve`): 2 000 and 20 000-declaration documents at 1 ms, 10 ms and 5 s budgets.
  - The interrupt never surfaces as a parse error: no `formalization_failed`, all `timeout` or `entailed`.
  - `_load`'s C-API checks treat an interrupted check as undecided and then stop on the deadline.
  - `_Watchdog.__exit__` joins before `_solve` returns, so no interrupt reaches a later call's context.
- **Both sides of the pipe use one pause total.**
  - The parent adds the pause to `_paused_s` and writes it to the shared `RawValue` before SIGCONT.
  - The worker's `_now()` subtracts it.
  - `Worker._poll` subtracts the same total, a pause in progress included.
  - So the parent's kill grace and the worker's deadline move together. `test_a_check_paused_past_its_timeout_keeps_its_budget` pins this.
- **Continue before kill, release, detach and exit.**
  - `Worker.interrupt` resumes first.
  - `_Interrupt.detach` resumes a frozen target before it goes back to the pool.
  - `_close_all` resumes live workers. It is registered after `multiprocessing.util`'s exit hook, so it runs first (LIFO).
- **Docstrings that match behaviour.**
  - `Worker.call` ("excludes the time the worker is paused").
  - `Worker._signal` ("False if it has ended", including `ProcessLookupError`).
  - `_Interrupt.ran_s`, `freeze`, `thaw`, `attach` and `detach`.
  - `run`'s "`fn` is called once".
  - `_Slots._balance` ("longest stuck first": `_lenders` is in stick order).
- **Constants.**
  - `config.py` holds only `SOLVER_TIMEOUT_S` (5.0), and `Settings.solver_timeout_s` defaults to it.
  - The new scheduler and worker constants (`_INTERRUPT_EVERY_S`, `_PAUSABLE`, `_BOOT_S`) are module `Final`s with docstrings.
  - `_RESET_SHARE` is gone from code and tests. Only the ADR mentions it, as removed.
- **The SDK is unchanged.** `call_structured` still uses `messages.parse(output_format=...)`, and no `json.loads` or regex was added on LLM output.
- **SG36's flip list.** No strict xfail remains for AUDIT-38 #1, #2, AUDIT-34 #2, #3 or AUDIT-40 #5, #6. AUDIT-40 #2's remaining xfail is a mutant guard: it asserts that the U02 mutant fails. Its plain test passes, as the Result says.

## How this was measured

- **Reading.**
  - CLAUDE.md, PLAN.md (rules, rows SG35/SG36, rounds 9–10, the SG36 section and Log), AUDIT-39.md, AUDIT-38.md #1–#2 and AUDIT-40.md (summary).
  - ADR 0009 in full, `solver.py` and `solver_workers.py` in full, `test_solver_pausing.py` in full.
  - The SG36 diffs of `test_audit_34.py` and `test_audit_39.py`.
  - I grepped `_RESET_SHARE`, `timeout`, `__all__`, `Resolution`, `~~` and the `reason="AUDIT-…"` xfails across `src/`, `tests/`, `docs/` and the AUDIT files.
- **#1.** Parsing PLAN.md text. No code is run.
- **#2 and #4.** Scratch script, then the repro. `SolverExecutor(1, 0)` gets a hard check (`UNDECIDABLE`, 20–30 s timeout, always cancelled), polled until `_Slots.stuck == 1`. Then k hard newcomers, polled until k waiters are queued as hard.
  - Measured at that point: calls in `executor._running`, `frozen()`, real stops (`Worker._paused_at`, plus `/proc/<pid>/stat` state `T` in the script), `_pool._max_workers` and `VmRSS`.
  - #4 sets `solver_workers._PAUSABLE = False`.
  - The script ran with k = 4 and 8 on 4 CPUs at load average 4.1. The repros use k = 3.
- **#3.** `_Slots(1)` driven directly, with a fake pausable target. No threads or timing.
- **Re-checks.** `uv run pytest tests/audit/test_audit_{21,23,31,35,39}.py --runxfail -p no:randomly`: 15 failed, 7 passed. Every open repro named above failed. AUDIT-35 #2 and AUDIT-39 #1, #3 passed.
- **Stability.** `uv run pytest tests/audit/test_audit_43.py`, 3 runs: 4 xfailed each time. With `--runxfail`, 3 runs: 4 failed each time, each on its stated assertion.

## Gate

- `make lint typecheck`: ruff clean (170 files formatted) and `mypy --strict` clean (154 source files).
- `uv run pytest tests/audit/test_audit_43.py -q`: 4 xfailed.
- `uv run pytest -m "not perf" -q -p no:randomly`: 1427 passed, 13 skipped, 62 deselected, 108 xfailed (including the 4 here). `make test` (with `perf`) was not run.

## Bottom line

**SG36 does what the owner chose, where it says it does:**
- On Linux, a lending slot's worker is SIGSTOPped while its place is lent.
- Paused time is excluded on both sides of the pipe, by a watchdog that replaces Z3's wall-clock timeout.
- `fn` runs once.
- AUDIT-34 #2/#3, AUDIT-35 #2, AUDIT-38 #1/#2, AUDIT-39 #1/#3 and AUDIT-40 #5/#6 now pass.

**The SDK surface is unchanged and consistent.**

**What does not hold:**
- **The record.** SG36 is "done" with a Verify item that failed three rounds in a row and was never re-run. This is the same stop-and-report breach AUDIT-39 #1 found for SG35.
- **The resources behind the CPU bound.** Every newcomer that gives way keeps its Z3 process (about 61 MB) and a thread for as long as it waits. The number of such processes is now bounded only by the number of runs, where SG35 bounded it by the places.
- **"Never" and "at most".** These are not true at the edges: when a lender ends with its place lent (#3), or without SIGSTOP (#4).

**Still open:**
- AUDIT-31 #1: an unchecked non-final step passes.
- AUDIT-21 #1 / AUDIT-23 #1: an approved estimate disagreement skips the cost pause.

Neither has changed since rounds 8 and 5.
