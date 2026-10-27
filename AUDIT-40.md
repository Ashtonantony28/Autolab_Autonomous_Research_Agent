# AUDIT-40: Test coverage and test quality (round 10)

Branch `claude/audit-40`, based on `origin/claude/sg35-stopped-newcomers` (`1e537af`, SG35). This is an independent audit. No production code or existing test was changed.

**Bottom line:** coverage is unchanged at the top level: 97.5% line and branch overall, 97.7% on `verification/` (target ≥90%). The only uncovered `verification/` lines are child-only fork code, as in round 9. Every repro SG35 flipped fails against the pre-fix code (`6c16dce`) on its own assertion, 2 of 2 runs, and passes on HEAD:
- AUDIT-34 #1 (both tests)
- AUDIT-35 #1
- AUDIT-36 #1
- AUDIT-33 #3

SG35's test changes are sound. The two retuned tests now kill the mutants AUDIT-36 #9 said they had stopped guarding (D01, A01), and the new expected order in `test_runs_that_start_together_do_not_hold_up_a_later_newcomer` is a strengthening, not a weakening (it kills U06).

I ran 30 one-line mutations by hand over the code SG35 changed, plus D01 and A01 against the retuned tests:
- 18 were killed.
- 5 are equivalent.
- 7 survived, over 4 rules SG35 added that tests reach only in the common case: the exception path's pause, the "not stuck while booting" rule, the late stop, and `acquire`'s immediate grant with `lend=False`.

The flaky perf test is a real test-quality defect, not just noise. Under load, the AUDIT-14 #1 repro can pass without running the check it is about.

Severity counts: **0 critical, 0 high, 3 medium, 3 low.**
- **#2 (medium):** "a slot whose check waits for its worker is not stuck" is untested. Dropping `not interrupt.paused() and` (U02), or making `paused()` always False (I06), passes the whole suite.
- **#5 (medium):** the flake in `test_audit_24::test_on_the_one_call_path_the_audit_14_1_repro_catches_the_assumption_check` has two causes:
  - The AUDIT-14 #1 repro calibrates its timeout first. When the machine then runs the check about 2× slower, `_solve`'s `_SETUP_SHARE` guard skips the entailment check, so ASSUME is never exercised and the test reports "DID NOT RAISE".
  - Separately, under heavier load the test exceeds pytest's 60 s timeout.
- **#6 (medium):** a non-`perf` unit test, `test_solver_executor::test_solver_ms_excludes_worker_start_up`, depends on when the executor's prestart thread runs. It failed this audit's first coverage run with "too many values to unpack": a pool with `idle_max=1` held two idle workers. The same race lets `prestart` and `discard` together leave one idle worker above `idle_max` in production.

## How this was measured

- **Coverage:** `uv run pytest --cov=autolab --cov-branch --cov-report=term-missing --cov-report=json tests` on the base (1e537af, without this file). Package figures are (covered lines + covered branches) / (lines + branches), from the JSON report.
  - The base run gave 1449 passed, 13 skipped, 111 xfailed and **2 failed**:
    - AUDIT-30 #1's first `perf` repro: "waited 1.13 s = 2.8 hard checks", bound 1.5.
    - `test_solver_ms_excludes_worker_start_up`: ValueError, see #6.
  - It ran on 4 CPUs at a load average of 5–9: other sessions shared the machine, and my own repro runs overlapped it.
  - The final run with this file is at the end.
- **Worker and child processes:** as in AUDIT-28/32/36, pytest-cov does not see code that runs only in a worker or a forked child. SG35's `_children()` (`solver_workers.py` 293-298) is child-only. It was measured by mutants instead (K01–K03), with the mutated copy first on `PYTHONPATH`, so forkserver children and the fork scripts load it.
- **Mutations:**
  - A scratchpad harness (`a40/harness.py`, not committed) copies `src/`, `tests/`, `scripts/` and `pyproject.toml` to a fresh private directory per mutant. It applies exact-match replacements (each asserted to occur once), checks that `autolab` is imported from the copy, and runs the venv's pytest there with `PYTHONPATH=<copy>/src:<copy>`. The worktree was never edited, and every copy was deleted after its run. One copy was left behind when a stream hit its time limit; I deleted it, and re-ran its two mutants (U01, U02).
  - Each mutant ran `pytest -x -q -m "not perf" tests/unit/verification tests/audit --ignore=tests/audit/test_audit_40.py` (902 tests selected, 59 `perf` deselected).
  - Two streams ran in parallel. A kill was accepted only on an assertion that the mutant explains. One stream's "kill" of I03 (by `test_solver_workers::test_a_worker_times_out_where_solve_times_out_here`, a load flake: the worker was killed 30 ms past a 0.3 s timeout) did not repeat: 3 runs of that file alone, all passed. I03 is counted as a survivor.
  - Each survivor, and each equivalent mutant, was then run against `test_audit_40.py`'s plain tests alone, in a fresh copy. Each survivor was killed by the test named for it, and only by that test. Each equivalent mutant and HEAD passed all five.
- **Pre-fix repros:** `solver.py` and `solver_workers.py` from `6c16dce` (origin/claude/audit-round-9) in a fresh copy. HEAD's tests ran there with `--runxfail`, twice, and once more with `--tb=line` to read the failing assertion. HEAD served as the control.
- **Retuned tests:** each was run alone against the mutant it should guard, in a fresh copy, twice.
- **The perf flake (#5):** `a40/flaky_runs.sh` runs the AUDIT-24 test N times beside B busy loops (`python3 -c 'while True: pass'`). `a40/flaky.py` times each phase of the AUDIT-14 #1 repro under ASSUME and prints the core check's outcome details.
- **The prestart race (#6):** first seen in the base coverage run. It did not repeat in 15 runs of the test alone at load 8–9, so the repro forces the failing order with events (see #6).

## Coverage

| Module | Stmts | Miss | Branch | BrPart | Cover | Missing |
|---|---:|---:|---:|---:|---:|---|
| agents/* | 407 | 1 | 82 | 1 | 99.6% | design 90 (carried) |
| cli.py | 79 | 6 | 22 | 0 | 92% | 75, 84-86, 115-117 |
| monitoring/* | 150 | 0 | 38 | 1 | 99.5% | tracer 77->76 |
| orchestrator/* | 288 | 0 | 64 | 0 | 100% | |
| state/checkpointer.py | 95 | 11 | 38 | 14 | **80%** | 55-73, 116->150, 162 (AUDIT-8 M6, carried) |
| state/ (other 3) | 298 | 0 | 36 | 0 | 100% | |
| tools/* | 122 | 5 | 26 | 4 | 94% | simulator 89; literature 81, 107, 124, 130 |
| verification/formalizer.py | 241 | 0 | 98 | 1 | 99% | 419->427 |
| verification/solver.py | 591 | 4 | 182 | 3 | 99% | 815 (`run`'s pid check, see notes), 949-951 + 954->958 (at-fork hook, child only), 972->974 |
| verification/solver_workers.py | 206 | 15 | 46 | 1 | **90%** | 48-52 (worker only), **293-298, 302-304** (child only: `_children`, `_forget_forkserver`), 307->311, 319-320 (atexit) |
| verification/llm.py | 69 | 1 | 16 | 2 | 96% | 189, 219->218 |
| verification/vericot.py | 130 | 1 | 52 | 1 | 99% | 247 |
| verification/ (other 4) | 71 | 0 | 18 | 0 | 100% | |
| **TOTAL** | 2778 | 44 | 718 | 28 | **97.5%** | |

| Package | Lines | Branches | Cover (line + branch) | Target |
|---|---:|---:|---:|---|
| verification/ | 98.4% | 95.6% | **97.7%** | ≥90% ✔ |
| agents/ | 99.8% | 98.8% | 99.6% | ≥70% ✔ |
| orchestrator/ | 100% | 100% | 100% | ≥70% ✔ |
| monitoring/ | 100% | 97.4% | 99.5% | ≥70% ✔ |
| state/ | 97.2% | 78.4% | 94.2% | ≥70% ✔ |
| tools/ | 95.9% | 84.6% | 93.9% | ≥70% ✔ |
| cli.py | 92.4% | 90.9% | 92.1% | ≥70% ✔ |

- SG35 added 24 statements and 6 branches to `solver.py`, and 5 statements to `solver_workers.py`. Every new in-process line is covered.
- Coverage does not show the gaps this audit found: every surviving mutant is on a covered line.
  - The pause in `check_entailment`'s `except` runs (S04 survives).
  - `paused()` runs (I06 survives).
  - `stopped = interrupt.request()` runs (U01 survives).
  - The gaps are in assertions, not in reach.

## Mutation table

ID prefixes:
- S: `check_entailment`'s pause/resume.
- I: `_Interrupt`.
- U: `SolverExecutor.run`'s stop and re-queue.
- C: `_call`.
- Q: `_Slots` arrival order and `lend`.
- K: `Workers._adopt` / `_children`.
- D, A: guards of SG35's retuned tests.

"→" names the `test_audit_40.py` test that now kills a survivor.

| ID | Mutation | Result | Killed by |
|---|---|---|---|
| S01 | no `pause()` before `pool.acquire()` (= AUDIT-28 W10) | killed | test_solver_executor::test_solver_ms_excludes_worker_start_up |
| S02 | no `resume()` after `ready()` (the call is not counted) | killed | same |
| S03 | no `resume()` when `attach` is refused | **equivalent** | the check returns at once as interrupted; with no time between it and `ended`, `ran_s` is the same either way, and the interrupt is never reused |
| **S04** | no `pause()` in the `except BaseException` path | **survived** | → test_only_the_call_itself_is_a_checks_run_time (#1) |
| S05 | no `pause()` after the call (= AUDIT-28 W09) | killed | test_solver_ms_excludes_worker_start_up |
| S06 | no final `resume()` | **equivalent** | `ran_s` subtracts the open pause up to `ended`, the same amount `resume` would add; `paused()` stays True only after `detach`, where neither `stick` nor `_to_slow` reads it. Every `fn` makes one check (`vericot.py` 217) |
| S07 | `resume()` before `ready()` (boot counted) | killed | test_solver_ms_excludes_worker_start_up |
| I01 | `ran_s` ignores a pause in progress | killed | test_audit_34::test_a_newcomer_is_not_stopped_while_its_worker_starts |
| I02 | `ran_s` ignores `_overhead_s` | killed | test_solver_ms_excludes_worker_start_up |
| **I03** | `pause()` without its `is None` guard | **survived** | → test_only_the_call_itself_is_a_checks_run_time (#1); see "How this was measured" for its one spurious kill |
| I05 | `resume()` keeps `_paused` set | killed | test_faithfulness (negative `solver_ms` fails `StepVerdict` validation) |
| **I06** | `paused()` always False | **survived** | → test_a_slot_whose_check_waits_for_its_worker_lends_nothing (#2) |
| **I07** | `request()` returns True with no target attached | **survived** | → test_a_stop_after_the_check_left_its_worker_keeps_the_result (#3) |
| I08 | `request()` returns True when already requested | **equivalent** | only `shutdown` (which sets `_closed` first) or `_call` under `_closed` request before `run` does, and `run` returns the result when `_closed` |
| **U01** | `interrupt.request(); stopped = True` (the pre-SG35 stop) | **survived** | → test_a_stop_after_the_check_left_its_worker_keeps_the_result (#3) |
| **U02** | `stuck = slots.stick(lent)` (a booting slot sticks) | **survived** | → test_a_slot_whose_check_waits_for_its_worker_lends_nothing (#2) |
| U03 | stopped: `new = False` always (the pre-SG35 re-queue) | killed | test_audit_34::test_a_newcomer_that_gives_way_to_a_short_caller_is_served_before_hard_runs |
| U04 | stopped: `new = True` always (a hard stop stays a newcomer) | killed | test_solver_scheduling::test_runs_that_start_together_do_not_hold_up_a_later_newcomer |
| U05 | stopped: `lend = True` (lent again) | killed | test_a_short_caller_takes_a_lent_place_from_a_newcomer_after_a_slice (its 10 s bound) |
| U06 | stopped: a fresh arrival number | killed | test_runs_that_start_together_… (order r0, r2, r1) |
| C01 | `_call` returns `ended - started` | killed | test_solver_ms_excludes_worker_start_up |
| Q01 | a re-queued caller is appended, not inserted by arrival | killed | test_solver_scheduling::test_a_caller_queued_again_keeps_its_arrival_and_is_lent_nothing |
| Q02 | insertion compares `>=` | **equivalent** | arrival numbers are unique per waiter (one `acquire` per `run` at a time) |
| **Q03** | `acquire`'s immediate grant ignores `lend` | **survived** | → test_a_caller_queued_again_is_lent_nothing_even_when_a_place_is_free (#4) |
| Q04 | `_wake`'s grant ignores `lend` | killed | test_a_caller_queued_again_keeps_its_arrival_… |
| Q05 | `lendable()` ignores `lend` | **equivalent** | `lent_past` only orders `_lent_rank`; an entry that cannot take a lent place is skipped by `_grant` whatever its rank |
| Q06 | `_grant` lends to short callers only | killed | test_solver_scheduling::test_newcomers_are_lent_places_latest_first |
| K01 | `_adopt` leaves the owner's workers in `_children` | killed | test_solver_workers::test_a_forked_child_starts_its_own_workers_and_leaves_the_parents_alone[exit] |
| K02 | `_adopt` drops only the idle workers from `_children` | killed | test_audit_35::test_a_forked_child_that_exits_normally_leaves_the_owners_workers_alone (a check in flight at the fork) |
| K03 | `_children()` returns a copy | killed | …leaves_the_parents_alone[exit] |
| D01 | slow cap at `>` | killed | test_solver_executor::test_slow_checks_are_bounded_by_slow_workers (`assert 2 == 1`), alone, 2/2 |
| A01 | `acquire` grants a newcomer no lent place | killed | test_solver_scheduling::test_a_short_caller_overtakes_hard_checks_above_the_slow_cap (`(0, False) == (1, False)`), alone, 2/2 |

**Totals:** 32 mutations: 30 over SG35's code, plus D01 and A01.
- The existing suite killed 20: 18 over SG35's code, plus D01 and A01.
- 5 are equivalent: S03, S06, I08, Q02, Q05.
- 7 non-equivalent survived: S04, I03, I06, I07, U01, U02 and Q03, over 4 rules. I06 and U02 are one rule (`paused()` and its one reader), and so are I07 and U01 (`request()`'s result and its one reader). Each is killed by a plain test in `test_audit_40.py`, checked in a fresh mutant copy. A strict xfail applies S04, I03, U02, U01 and Q03 (one per rule, and both exception-path mutants).

## Findings

### Critical

None.

### High

None.
- No surviving mutant lets a check come back VALID that should not, or loses a verdict.
- Every survivor changes how long a check counts as running, whether a slot lends a place, or whether a check runs twice; each is a cost or ordering effect.
- The fork-exit fix is pinned: K01–K03 are all killed, including the worker-in-use case (K02, by AUDIT-35's in-flight check).

### Medium

**#2. "A slot whose check waits for its worker is not stuck" is untested (U02, I06 survived; repro: `test_a_slot_whose_check_waits_for_its_worker_lends_nothing`, strict xfail "AUDIT-40 #2").**
- SG35, ADR amendment: "a slot whose check waits for its worker is not stuck, so it lends nothing". Otherwise a slot whose worker boots lends its CPU to a newcomer and then takes it back once the worker is up: one more Z3 check than places, the AUDIT-34 #2 oversubscription, on every cold start.
- `stuck = not interrupt.paused() and slots.stick(lent)` is covered, but no test has a slot's check stay paused across a slice. AUDIT-34's boot test has the *newcomer* boot slowly, not the slot's check.
- *Added:* a slot's check attaches while paused, as `check_entailment` does before `ready`. The executor polls it three slices: `stuck` must be 0, then 1 once it resumes. The test is event-driven, with no wall-clock assertion. Under U02 or I06: `(1, 1)`.

**#5. The AUDIT-14 #1 repro passes vacuously when the machine slows after calibrating; this is the AUDIT-24 one-call-path flake (repro: `test_the_audit_14_1_repro_catches_assume_when_the_machine_slows_after_calibrating`, `perf`, strict xfail "AUDIT-40 #5"; control: `…_when_calibrated_on_this_machine`).**
- `test_audit_24::test_on_the_one_call_path_the_audit_14_1_repro_catches_the_assumption_check` applies ASSUME and expects `test_audit_14::test_a_check_returns_within_its_timeout` to fail.
- That test sets the timeout to 3× `until_s`, the time until the entailment check starts, measured beforehand (best of 2).
- `_solve` does not start the entailment check when less than `_SETUP_SHARE` (0.5) × the consistency check's time is left. So if the check runs about 2× slower than the calibration, it returns "timeout" inside the timeout ("too little time left for the entailment check"), and ASSUME's unpolled phase never runs.
- The worker leg cannot catch ASSUME either: the kill bounds it (AUDIT-24 #1). It measured 1.09–1.62× the timeout, so it "fails" only through start-up noise.
- Measured beside 8 busy loops on 4 CPUs (load about 12), the AUDIT-24 test went 1 pass, 1 "DID NOT RAISE", 1 "Failed: Timeout (>60.0s)" (77.7 s). Beside 4 busy loops: 3 of 3 passed, in 28.7–41.5 s.
- `a40/flaky.py` under 8 busy loops: 3 of 7 core checks under ASSUME returned within the timeout (0.71–0.93×). The one run whose details were printed said "too little time left for the entailment check". The other 4 took 2.45–6.77× the timeout. A quiet run took 16.7 s; one loaded run took 81 s, which is over pytest's 60 s timeout.
- Consequences:
  - The perf repro of a *high* finding (AUDIT-14 #1) can pass without exercising the code it guards. The plain AUDIT-14 #1 test has the same vacuous mode.
  - Its run time (two full ~4–14 s entailment checks in `_phases_s`) can push it past the per-test timeout.
- *Added:* the repro fixes the skew at 2.5×. The calibration is the time of a `true` claim (which stops after the consistency check) divided by 2.5, and the worker leg is taken out. Today: "DID NOT RAISE". The control (skew 1) catches ASSUME.
- *Suggested:* have the AUDIT-14 #1 repro assert that the entailment check ran (the outcome's details are not "too little time left"), or recalibrate when it did not. Give the test a `timeout` mark scaled to its measured phases.

**#6. `test_solver_ms_excludes_worker_start_up` depends on thread timing (repro: `test_solver_ms_excludes_worker_start_up_whenever_prestart_runs`, strict xfail "AUDIT-40 #6").**
- The test patches `solver_mod._workers` to return its own pool (`idle_max=1`) and patches `pool._start` to add 1000 s to a fake clock. Then it builds a `SolverExecutor`, whose prestart thread starts a worker in that same pool.
- The expected `spent == 1000 + 100 + 1 + 10 + 1000` holds only if prestart's worker is the one the check takes.
- If prestart finds no idle worker, the check starts its own. If prestart then appends its worker after the check's replacement, the idle list has two workers (the observed "ValueError: too many values to unpack") and `spent` is 3111.
- It is not marked `perf` and failed once in this audit's base coverage run (load 5–9). It passed 15 of 15 alone at load 8–9.
- The repro forces that order with events and fails on `assert 3111.0 == 2111`, every time.
- The same race exists in production: `prestart` and `discard` each check `idle_max` and then append after starting a worker, without re-checking. Together they can leave `idle_max + 1` idle workers. That is harmless (one extra process), so it is noted rather than ranked separately.
- *Suggested:* in the test, build the executor first and patch `_workers` after its prestart thread has finished, or patch `Workers.prestart` to a no-op. In `Workers`, re-check `idle_max` under the lock before appending.
- *Also seen:* `test_solver_workers::test_a_worker_times_out_where_solve_times_out_here` (not `perf`) gave one spurious failure in a mutant stream at load about 8: the worker was killed 0.03 s (the grace) past a 0.3 s timeout. It passed 12 of 12 alone beside 8 busy loops. `test_audit_34::test_a_newcomer_is_not_stopped_while_its_worker_starts` also bounds a real worker's easy check by 20 slices (1 s) of wall time without `perf`. Both are the AUDIT-8 H1 pattern in tests not marked `perf`.

### Low

Each low finding has a plain test that passes today and a strict xfail `AUDIT-40 #n` that applies the mutant.
- **#1 (S04, I03). The exception path's pauses are untested.**
  - S04: when a check raises while its worker is mid-check (a signal's exception), `check_entailment` pauses before discarding the worker. Without that, the worker's stop and replacement count as run time, and the task can be recorded as hard.
  - I03: if `ready` itself raises, a `pause()` without its guard restarts the pause, and the boot counts.
  - The suite's fake-clock test covers only the killed path.
  - *Added:* `test_only_the_call_itself_is_a_checks_run_time`. A fake pool and worker advance a fake clock by 1/2/4/8 slices in acquire/ready/call/release-or-discard, on four paths (reply, lost, raised in the call, raised in `ready`). The check's run time must be exactly the call's 4 slices (0 when `ready` raises), and 0 while it acquires or boots. Under S04 the raised path gives 12; under I03 the boot-raised path gives 2.
- **#3 (U01, I07). "A stop that comes too late keeps the result" is untested.**
  - SG35's `stopped = interrupt.request()` keeps a lent newcomer's result when its check left its worker between `_yields` and the stop. The pre-SG35 line throws the result away and runs the check again (the "`fn` must be pure" cost).
  - The window is a thread race, so no test reached it.
  - *Added:* `_yields` is wrapped to detach the check as it decides, as the pool thread would at that moment. Expected `("kept", 1)`. Under U01 or I07: `("kept", 2)`.
- **#4 (Q03). `acquire`'s immediate grant with `lend=False` is unpinned.**
  - SG35's new test reaches `lend=False` only through `_wake`: it queues before the slot sticks. If a lent place is free when a stopped newcomer asks again (its short caller took a slot that freed meanwhile, or left), `acquire` itself must not lend it one.
  - *Added:* `test_a_caller_queued_again_is_lent_nothing_even_when_a_place_is_free`, on `_Slots`. Under Q03 the caller is lent the place at once.

### Notes (no finding)

- **Carried:**
  - Coverage targets are not enforced by `make test` or CI (AUDIT-8 M5).
  - `state/checkpointer.py` is at 80% (AUDIT-8 M6).
  - `perf` tests run in the default run (AUDIT-8 H1). This audit saw AUDIT-30 #1's first repro fail once more under load (2.8 hard checks).
  - AUDIT-34 #2 (CPU bound) stays open by SG35's choice (ADR 0009 amendment).
- `SolverExecutor.run`'s pid check (`solver.py` 815) is still never reached in-process: the at-fork hook always adopts first (AUDIT-36 notes).
- AUDIT-28's W10 xfail is still named "excludes starting a worker". With the `pool` fixture's idle worker and no `_start` patch, the retargeted mutant counts the boot (`ready`, 10 s), not a start. It fails on its own assertion (11000 ≠ 1000), and W09 does too (101000), so the retarget is valid; only the name is stale.

## SG35's test changes

| Test | Change | Checked against | Verdict |
|---|---|---|---|
| test_solver_executor::test_slow_checks_are_bounded_by_slow_workers | adds `slow_peak == 1` via a `_to_slow` wrapper | D01 (slow cap at `>`), alone, 2/2: `assert 2 == 1` | **restored** (AUDIT-36 #9). Not machine-dependent: the slot's check runs Z3 to a 0.3 s timeout, so it always outlives a slice and is moved once; the cap makes a second move impossible |
| test_solver_scheduling::test_a_short_caller_overtakes_hard_checks_above_the_slow_cap | newcomer returns `slots.lent` from inside its check; asserts `(1, False)` and `hard.gate` unset | A01, alone, 2/2: `(0, False) == (1, False)` (after the held check's 10 s gate timeout) | **restored**: it now asserts the lending itself |
| test_solver_scheduling::test_a_caller_queued_again_keeps_its_arrival_and_is_lent_nothing (new) | `_Slots` only: arrival kept, lent nothing on `_wake` | Q01, Q04 killed | good; misses `acquire`'s own grant (Q03, #4) |
| test_solver_workers::test_a_forked_child_starts_its_own_workers_and_leaves_the_parents_alone[_exit, exit] | parametrized ending; the `exit` child calls `sys.exit(0)` after its checks and `shutdown_default_executor()` | pre-fix code: `[exit]` fails (`assert alive`), `[_exit]` passes; K01, K03 killed | good. It checks idle workers only; the worker-in-use case is AUDIT-35's |
| test_solver_scheduling::test_runs_that_start_together_do_not_hold_up_a_later_newcomer | re-run order `r0, r2, r1` → `r0, r1, r2` | pre-fix code gives `r0, r2, r1` (fails); U06 (fresh arrival) gives `r2` at index 5 (fails); U04 fails its `not w.new` check | **not a weakening**: the old order was the order the runs were stopped in (the back of the hard queue); the new one pins SG35's arrival-order re-queue and kills U06 |
| test_audit_28 W09/W10 strings | target `interrupt.pause()` before `detach` / before `acquire` | `--runxfail`: 101000 and 11000 ≠ 1000, each on its own assertion | valid (name of W10 stale, see notes) |
| xfail markers removed: AUDIT-34 #1 ×2, AUDIT-35 #1, AUDIT-36 #1, AUDIT-33 #3 | see next section | | |

## Round-9 repros flipped by SG35, against the pre-fix code

HEAD's tests with `6c16dce`'s `solver.py` and `solver_workers.py`, `--runxfail`, twice (both runs: 8 failed, 3 passed of the 11 selected); HEAD: 11 passed.

| Repro | Result on pre-fix code | Verdict |
|---|---|---|
| AUDIT-34 #1 (gives way to a short caller) | fail 2/2: `['new', 'short', 'h1', 'h2', 'new']`, expected `new, short, new` first | not weakened |
| AUDIT-34 #1 (not stopped while its worker starts) | fail 2/2: "started 2 times; done before the slot freed: set()" | not weakened; uses wall-clock bounds without `perf` (see #6) |
| AUDIT-35 #1 | fail 2/2: `in_flight: 'worker_died'`, idle `[False, False, False]`, "can only join a child process" | not weakened |
| AUDIT-36 #1 | fail 2/2: parent's idle workers all `False` | not weakened |
| AUDIT-33 #3 | fail 2/2: `victim == 255` (expected -9) | not weakened |

All five fail on their own assertion, not on setup. `test_a_caller_queued_again_keeps_its_arrival_and_is_lent_nothing` fails on the pre-fix code with `AttributeError: '_Slots' object has no attribute 'arrive'`. That is expected for a test of a new API, but it means the test does not repro AUDIT-34 #1 by itself; the two AUDIT-34 tests do.

## Added tests (`tests/audit/test_audit_40.py`)

| Test | Kills / reproduces |
|---|---|
| test_only_the_call_itself_is_a_checks_run_time (+ xfails `_under_s04`, `_under_i03`) | S04, I03, #1 |
| test_a_slot_whose_check_waits_for_its_worker_lends_nothing (+ xfail `_under_u02`) | U02, I06, #2 |
| test_a_stop_after_the_check_left_its_worker_keeps_the_result (+ xfail `_under_u01`) | U01, I07, #3 |
| test_a_caller_queued_again_is_lent_nothing_even_when_a_place_is_free (+ xfail `_under_q03`) | Q03, #4 |
| test_the_audit_14_1_repro_catches_assume_when_calibrated_on_this_machine (`perf`, control) + xfail `…_when_the_machine_slows_after_calibrating` | #5 |
| xfail test_solver_ms_excludes_worker_start_up_whenever_prestart_runs | #6 |

- Waits are bounded by events, by polls of `_to_slow`, or by a fake clock. The `perf` pair compares the machine's own measurements and has no absolute seconds.
- Under `--runxfail`, every xfail fails on its own assertion, not on setup (7 of 7):
  - S04 / I03: the run-time table differs.
  - U02: `(1, 1)`.
  - U01: `('kept', 2)`.
  - Q03: `(True, 1)`.
  - #5: "DID NOT RAISE".
  - #6: `3111.0 == 2111`.

Verification:
- `make lint typecheck`: ruff clean, 164 files already formatted, mypy strict clean on 149 source files.
- `uv run pytest tests/audit/test_audit_40.py -q`: 5 passed, 7 xfailed. With `--runxfail`: 7 failed (each on an `assert`), 5 passed.
- Full coverage run with this file (the audit's command): **1456 passed, 13 skipped, 117 xfailed** in 5 min 51 s, green; 97.5% overall, `verification/` 97.7%. The 117 are the base's 111, less the AUDIT-36 #8 duplicate XFAIL of `test_audit_6::test_reused_graph_memory_does_not_grow_per_run` (twice in the base run, once here), plus this file's 7. The base run's two failures (#6 and AUDIT-30 #1's `perf` repro) did not recur in this quieter run.
