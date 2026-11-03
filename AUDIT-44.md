# AUDIT-44: Test coverage and test quality (round 11)

Branch `claude/audit-44`, based on `origin/claude/sg36-pause-lender` (`3d2d7b4`, SG36). This is an independent audit. No production code or existing test was changed.

**Bottom line:** coverage held: 97.5% line and branch overall, 97.6% on `verification/` (target ≥90%). Every in-process line SG36 added is covered. The only SG36 line not covered is the exit hook's SIGCONT loop, which runs after coverage stops; mutant W15 shows it is tested.

Coverage hides weak assertions. I ran 68 one-line mutants over SG36's code:
- 41 were killed.
- 7 are equivalent.
- 20 survived the whole non-`perf` suite.

The 20 survivors fall into 15 findings, all now pinned by `tests/audit/test_audit_44.py`. The worst is R01: one changed token makes the executor's event loop spin for ever once a newcomer that gave way gets a slot back while a short caller waits. No test reaches that state.

SG36's central guarantee is that a paused check keeps its budget. Its only test kills the relevant mutants by luck of timing. The mutant that drops the shared paused total (W04) passed the whole suite under load.

Every flipped repro fails against the pre-fix code (`9792264`, the merge base with `origin/claude/plan-execute-modes-PT67G`), 2 of 2 runs, but three fail there for the wrong reason:
- AUDIT-34 #2 and #3 fail on setup (`Worker` has no `pause`). They fail on their own assertions once no-op shims are added, so the adaptation is sound.
- The rewritten AUDIT-39 #3 no longer reproduces AUDIT-39 #3. With shims it times out instead of failing its assertion; it is now a test of SG36's freeze path.

SG36's other test edits are sound: the widened grace, the changed assertion, the timeout(300) mark, the removed P04 xfail, and the four retargeted mutant strings.

Severity counts: **0 critical, 1 high, 6 medium, 9 low.**
- **#13 (high):** "gave way" counts only on a lent place, and nothing pins it. With `ceded = gave_way` (R01), a newcomer that gave way and is back on a slot skips every slice wait while a short caller waits. The event loop then spins in `run`. With the lent places taken (the 1-CPU defaults: 1 slot, no slow place), nothing can serve the short caller, so every caller on that loop hangs.

## How this was measured

- **Coverage:** `uv run pytest --cov=autolab --cov-branch --cov-report=term-missing --cov-report=json tests` on the base (`3d2d7b4`, without this audit's files). Package figures are (covered lines + covered branches) / (lines + branches), from the JSON report.
  - The base run gave 1479 passed, 13 skipped, 113 xfailed and **1 failed**, in 11 min 20 s: the `perf` test `test_solver_executor::test_hard_runs_do_not_starve_an_easy_verify_on_one_worker` (`assert 1.23 < 0.5`).
  - That failure passed 3 of 3 alone right after. Other sessions had the 4 CPUs at a load average of 5–8 during the run: the AUDIT-8 H1 pattern, carried.
  - The final run with this audit's file is at the end.
- **Worker and child processes:** pytest-cov does not see code that runs only in a worker, a forked child or an atexit hook. That covers `_Watchdog` and `_now` in a worker, `paused_s` in a worker, and `_close_all`'s new SIGCONT loop (`solver_workers.py` 377-382). These were measured by mutants instead (W15, W17, W20, W21, P01–P10), with the mutated copy first on `PYTHONPATH`, so forkserver workers and subprocess scripts load it.
- **Mutations:**
  - A scratchpad harness (`a44/harness.py`, not committed) copies `src/`, `tests/`, `scripts/`, `pyproject.toml` and `PLAN.md` to a fresh private directory per mutant. It applies exact-match replacements (each asserted to occur once), checks that `autolab` imports from the copy, and runs the venv's pytest there with `PYTHONPATH=<copy>/src:<copy>`. The worktree was never edited.
  - Every copy was deleted after its run. Two copies were left behind when the first two streams hit their background time limit; I deleted them and re-ran their mutants (W16, W17).
  - Each mutant ran `pytest -x -q -m "not perf" tests/unit/verification tests/audit --ignore=tests/audit/test_audit_44.py` (927 tests selected, 62 `perf` deselected; HEAD 927 passed in 6 min). Two streams ran first, then three.
  - A kill was accepted only on an assertion the mutant explains. The one exception is W02, which drops `_PAUSABLE`; that constant is True on Linux, so W02 changes nothing here. Its "kill", by AUDIT-34 #1's order assertion, is a test race (#14), and W02 is counted equivalent.
  - Each survivor, each equivalent mutant and HEAD were then run against `test_audit_44.py`'s plain tests alone, in a fresh copy:
    - Each survivor was killed by the test named for it.
    - W04, P05, P06 and P01 also fail the neighbouring budget tests, which share their rule.
    - The 7 equivalent mutants and HEAD passed all 18 plain tests.
- **Pre-fix repros:** HEAD's tests with `9792264`'s `solver.py` and `solver_workers.py`, in a fresh copy, `--runxfail`, twice, with `--tb=line` to read each failing assertion.
  - AUDIT-40 #5 and #6 were fixed in tests, not in production code (`test_audit_14.py`, `test_solver_executor.py`). They were run with those two files from `9792264` and HEAD's `src`.
  - AUDIT-39 #1 is bookkeeping. It was run with `9792264`'s `tests/audit/` and `PLAN.md`.
  - A further copy added no-op `Worker.pause/resume` and `_Interrupt.freeze/frozen` to the pre-fix code. This separates "fails on setup" from "fails on its assertion".
- **The paused-budget test (#1):** `test_a_check_paused_past_its_timeout_keeps_its_budget`, 6 times in one process (a parametrized wrapper), in fresh copies under HEAD, W04 and P01, at load about 2.
- **The AUDIT-34 #1 race (#14):** it failed once in a mutant stream. It passed 40 of 40 alone at load 7. The repro forces the window open by delaying the newcomer's return until the slot has been handed on (see #14).

## Coverage

| Module | Stmts | Miss | Branch | BrPart | Cover | Missing |
|---|---:|---:|---:|---:|---:|---|
| agents/* | 407 | 1 | 82 | 1 | 99.6% | design 90 (carried) |
| cli.py | 79 | 6 | 22 | 0 | 92% | 75, 84-86, 115-117 |
| monitoring/* | 150 | 0 | 38 | 1 | 99.5% | tracer 77->76 |
| orchestrator/* | 288 | 0 | 64 | 0 | 100% | |
| state/checkpointer.py | 95 | 11 | 38 | 14 | **80%** | 55-73, 116->150, 162 (AUDIT-8 M6, carried) |
| state/ (other 3) | 297 | 0 | 36 | 0 | 100% | |
| tools/* | 122 | 5 | 26 | 4 | 94% | simulator 89; literature 81, 107, 124, 130 |
| verification/formalizer.py | 241 | 0 | 98 | 1 | 99.7% | 419->427 |
| verification/solver.py | 672 | 4 | 214 | 3 | 99.0% | 912 (`run`'s pid check, never reached in-process), 1079-1081 + 1084->1088 (at-fork hook, child only), 1102->1104 |
| verification/solver_workers.py | 248 | 19 | 60 | 1 | **90.3%** | 51-55 (worker only), 350-355, 359-361 (child only: `_forget_forkserver`, `_after_fork_in_child`), 364->368, **377-382** (atexit `_close_all`, SG36's SIGCONT loop included) |
| verification/llm.py | 69 | 1 | 16 | 2 | 96.5% | 189, 219->218 |
| verification/vericot.py | 130 | 1 | 52 | 1 | 98.9% | 247 |
| verification/ (other 4) | 71 | 0 | 18 | 0 | 100% | |
| **TOTAL** | 2901 | 48 | 764 | 28 | **97.5%** | |

| Package | Lines | Branches | Cover (line + branch) | Target |
|---|---:|---:|---:|---|
| verification/ | 98.3% | 95.6% | **97.6%** | ≥90% ✔ |
| agents/ | 99.8% | 98.8% | 99.6% | ≥70% ✔ |
| orchestrator/ | 100% | 100% | 100% | ≥70% ✔ |
| monitoring/ | 100% | 97.4% | 99.5% | ≥70% ✔ |
| state/ | 97.2% | 78.4% | 94.2% | ≥70% ✔ |
| tools/ | 95.9% | 84.6% | 93.9% | ≥70% ✔ |
| cli.py | 92.4% | 90.9% | 92.1% | ≥70% ✔ |

- SG36 added 81 statements and 32 branches to `solver.py`, and 42 statements and 14 branches to `solver_workers.py`. Every in-process line is covered, including `_again`'s cancellation branch (`solver.py` 996-997).
- Every surviving mutant is on a covered line; the gaps are in assertions.
- `_children` (`solver_workers.py` 343-347) is now covered: SG35's `_adopt` calls it in-process.

## Mutation table

ID prefixes:
- W: `Worker` / `Workers` in `solver_workers.py`.
- P: the worker-side clock (`_now`, `_Watchdog`, `_decide`, `_load`).
- I: `_Interrupt`'s freeze/thaw/attach/detach.
- L: `_Slots`' lenders and `short_queued`.
- R: `SolverExecutor.run`, `_again`, `_yields`, `_to_slow`.

"→" names the `test_audit_44.py` test that now kills a survivor.

| ID | Mutation | Result | Killed by |
|---|---|---|---|
| W01 | `pause` without its `_paused_at is None` guard | **equivalent** | no caller pauses a paused worker: `freeze` and `attach` pause only when `_frozen` is newly set or the target is new, and `detach` resumes before the next attach |
| W02 | `pause` without `_PAUSABLE` | **equivalent** | True on Linux; its one "kill" was the #14 race |
| W03 | `pause` records a pause though no signal was sent | killed | test_solver_pausing::test_a_worker_reaped_behind_its_exit_code_is_not_signalled |
| **W04** | `resume` never writes the shared total | **survived** | → test_a_check_paused_mid_check_runs_what_was_left_of_its_budget (#1), test_a_workers_pauses_add_up |
| W05 | `resume` sends no SIGCONT | killed | test_a_paused_worker_is_stopped_and_its_call_outlives_its_budget |
| **W06** | `_paused_s +=` → `=` | **survived** | → test_a_workers_pauses_add_up (#2) |
| **W07** | `_signal` without the `exitcode` check | **survived** | → test_a_worker_that_has_ended_is_never_signalled (#6) |
| W08 | no `ProcessLookupError` handler | killed | test_a_worker_reaped_behind_its_exit_code_is_not_signalled |
| **W09** | `_poll`'s end without `- self._paused()` | **survived** | → test_earlier_pauses_do_not_extend_a_calls_wait (#3) |
| **W10** | `_poll`'s timeout without `+ self._paused()` | **survived** | → test_a_call_waits_for_a_paused_worker_without_spinning (#5) |
| W11 | `_poll`'s deadline test without `+ self._paused()` | killed | test_a_paused_worker_is_stopped_and_its_call_outlives_its_budget |
| W12 | `ready` polls the pipe directly | killed | test_a_paused_worker_boots_past_the_boot_limit |
| W13 | `interrupt` without `resume` | killed | test_a_paused_worker_is_continued_before_it_is_killed |
| W14 | `stop` kills without `resume` | **equivalent** | SIGKILL ends a stopped process and `join` returns; `_paused_at` is never read after `stop` |
| W15 | `_close_all` sends no SIGCONT | killed | test_a_process_exits_while_its_worker_is_paused |
| **W16** | `prestart` appends instead of `release` | **survived** | → test_a_start_never_leaves_more_than_idle_max_idle[prestart] (#7) |
| W17 | the worker never stores the shared total (`_serve`) | killed | test_a_check_paused_past_its_timeout_keeps_its_budget, by timing (#1) |
| W18 | `_paused()` ignores a pause in progress | killed | test_a_paused_worker_is_stopped_and_its_call_outlives_its_budget |
| **W19** | `discard` appends instead of `release` | **survived** | → test_a_start_never_leaves_more_than_idle_max_idle[discard] (#7) |
| W20 | a child's `_adopt` keeps the owner's `_live` | killed | test_a_child_forked_while_a_worker_is_paused_leaves_it_to_its_owner |
| W21 | `_signal` sends nothing | killed | test_a_paused_worker_is_stopped_and_its_call_outlives_its_budget |
| P01 | `_now` is `time.monotonic` | killed | test_a_check_paused_past_its_timeout_keeps_its_budget, by timing (#1) |
| P02 | `_Watchdog.__exit__` does not join | **equivalent** | the thread holds the per-check context; a late interrupt reaches a context no check uses again |
| P03 | interrupt once, not every `_INTERRUPT_EVERY_S` | killed | test_audit_8::test_exhausted_deadline_never_becomes_an_unlimited_z3_timeout |
| P04 | `left <= 0` → `< 0` | **equivalent** | a float difference of exactly 0; the next pass interrupts |
| **P05** | `started`/`took` on `time.monotonic` | **survived** | → test_a_check_paused_in_its_consistency_check_still_decides (#4) |
| P06 | the guard's time left on `time.monotonic` | killed | test_a_check_paused_past_its_timeout_keeps_its_budget, by timing (#1) |
| **P07** | `_load`'s deadline poll on `time.monotonic` | **survived** | → test_a_worker_paused_before_decides_its_next_checks_in_their_budget (#3) |
| **P08** | `deadline = time.monotonic() + timeout_s` | **survived** | → same test (#3) |
| P09 | the watchdog's first wait is 0 | **equivalent** | the wait returns at once and the loop recomputes `left` |
| P10 | the watchdog's deadline + 3600 s | killed | test_a_check_paused_past_its_timeout_keeps_its_budget (the parent's kill) |
| I01 | `freeze` without its guard | killed | test_one_lender_is_paused_per_lent_place_longest_stuck_first |
| I02 | `freeze` does not pause the target | killed | test_a_check_paused_past_its_timeout_keeps_its_budget |
| I03 | `thaw` does not resume the target | killed | test_solver_executor::test_slow_checks_are_bounded_by_slow_workers |
| I04 | `ran_s` without `_frozen_s` | killed | test_a_frozen_check_neither_moves_to_a_slow_place_nor_counts_its_pause |
| I05 | `ran_s` ignores a freeze in progress | killed | same |
| I06 | `attach` while frozen does not pause | killed | test_a_check_attached_while_its_call_is_frozen_starts_paused |
| **I07** | `detach` while frozen does not resume | **survived** | → test_a_check_detached_while_frozen_is_continued (#11) |
| **I08** | `_frozen_s +=` → `=` | **survived** | → test_a_checks_frozen_spans_add_up (#2) |
| L01 | `acquire`'s immediate grant without `_balance` | killed | test_one_lender_is_paused_per_lent_place_longest_stuck_first |
| L02 | `_wake` without `_balance` | killed | same |
| L03 | `_balance`'s `i < lent` → `<=` | killed | test_slow_checks_are_bounded_by_slow_workers |
| L04 | `_balance` latest stuck first | killed | test_one_lender_is_paused_per_lent_place_longest_stuck_first |
| L05 | `stick` does not record the lender | killed | same |
| L06 | `unstick` does not thaw | killed | same |
| L07 | `unstick` without `_balance` | killed | same |
| L08 | any queued caller completes `short_queued` | killed | test_a_short_caller_that_queues_is_signalled |
| **L09** | `short_queued` returns a fresh future every call | **survived** | → test_every_newcomer_waiting_on_a_short_caller_is_signalled (#12) |
| L10 | `unstick` does not remove the lender | killed | test_one_lender_is_paused_per_lent_place_longest_stuck_first |
| **R01** | `ceded = gave_way` | **survived** | → test_a_newcomer_back_on_a_slot_waits_its_slices (#13) |
| **R02** | always wait, even when ceded with a short caller waiting | **survived** | → test_a_newcomer_that_gave_way_gives_way_to_a_short_caller_already_waiting (#10) |
| R03 | a ceded check waits on its future only | killed | test_solver_scheduling::test_a_short_caller_takes_a_lent_place_from_a_newcomer_after_a_slice |
| R04 | no `freeze` when it gives way | killed | test_a_cancelled_paused_newcomer_is_killed_and_leaves_nothing_behind |
| R05 | `new = False` when it gives way (pre-AUDIT-38 #1) | killed | test_a_short_caller_takes_a_lent_place_from_a_newcomer_after_a_slice |
| R06 | `new = True` when it gives way | killed | test_a_cancelled_paused_newcomer_is_killed_and_leaves_nothing_behind |
| **R07** | `held = True` after `_again` | **survived** | → test_a_late_freeze_leaves_every_place_free (#9) |
| R08 | `held` not cleared before `_again` | killed | test_a_cancelled_paused_newcomer_is_killed_and_leaves_nothing_behind |
| R09 | `extra = False` | killed | same |
| **R10** | the slow path's `unstick()` without the lender | **survived** | → test_a_lent_place_pauses_the_slot_check_that_lent_it[slow] (#8) |
| **R11** | the `finally`'s `unstick()` without the lender | **survived** | → test_a_lent_place_pauses_the_slot_check_that_lent_it[ends] (#8) |
| R12 | `stick` without the lender | killed | test_audit_34::test_z3_checks_never_outnumber_the_slots_and_slow_places |
| **R13** | `_again`'s cancel path never releases a granted place | **survived** | → test_a_newcomer_cancelled_as_it_gets_its_place_back_returns_it (#15) |
| **R14** | `_again` does not cancel its request when the check ends first | **survived** | → test_a_late_freeze_leaves_every_place_free (#9) |
| R15 | `elif held:` → `else:` | killed | test_a_cancelled_paused_newcomer_is_killed_and_leaves_nothing_behind |
| R16 | `_yields` without `gave_way or` | killed | test_a_newcomer_that_gave_way_gives_way_again_without_a_slice |
| R17 | `_to_slow` moves a frozen check | killed | test_a_frozen_check_neither_moves_to_a_slow_place_nor_counts_its_pause |
| R18 | no `thaw` after `_again` | killed | test_a_short_caller_takes_a_lent_place_from_a_newcomer_after_a_slice |
| R19 | no `if future.done(): break` | **equivalent** | a finished check is detached, so every branch of that pass is false and the loop condition ends it |

**Totals:** 68 mutations.
- 41 were killed by the existing suite. The kills of W17, P01 and P06 depend on timing (#1).
- 7 are equivalent: W01, W02, W14, P02, P04, P09, R19.
- 20 non-equivalent survived, over 15 rules. Each is killed by a plain test in `test_audit_44.py`, checked in a fresh mutant copy. A strict xfail applies one mutant per rule.
  - For the worker-side rules, the mutant runs in a process `monkeypatch` cannot reach. Those xfails apply a parent-side mutant of the same rule: W04 for #1, W09 for #3, and P05 in-process for #4.

## Findings

### Critical

None.
- No surviving mutant lets a check come back VALID when it should not. Every survivor changes how long a paused check may run, which check is paused, whether a place is given back, or whether a caller is served.
- No stopped, paused or forked check returned a wrong verdict in any run.

### High

**#13. "Gave way" counts only on a lent place, and nothing pins it (R01; repro: `test_a_newcomer_back_on_a_slot_waits_its_slices`, strict xfail "AUDIT-44 #13").**
- `run` lets a check skip its slice wait only while it is `ceded = lent and gave_way`: on a lent place, having given way before. That way it can give way again as soon as a short caller queues.
- A newcomer that gave way can get a *slot* back, not a lent place: the slot frees while it waits in `_again`. With `ceded = gave_way` (R01), it then skips the wait in every pass while a short caller waits. No branch of that pass awaits: `_to_slow` is False with no slow room, `lent and new` is False, and the slot is already stuck. So the event loop spins in `run`.
- The spin ends only if the slot's own `stick` lends the short caller a place at once. With the lent places taken, nothing can serve it, not even a check that ends, because its future is completed by the loop. That happens on the 1-CPU defaults (1 slot, no slow place) whenever one short check runs on the lent place, and every verify on that loop then hangs.
- No test has a newcomer that gave way get a slot back while a short caller waits. The whole suite passes under R01.
- *Added:* blocker stuck; a newcomer is lent its place and gives way to s1 (whose check holds the lent place); the blocker ends, so the newcomer gets the slot; then s2 queues.
  - `_Slots.short_waiting` is wrapped to count its calls. Past 10 000 calls it raises `_Spinning`, which no slice-paced loop can reach, so the test cannot hang.
  - Expected `['new', 's1', 's2']`. Under R01: `[_Spinning(), 's1', 's2']`.
- *Severity:* the code is correct today; this is a test gap. It is ranked high because the mutant it lets through turns a plausible scheduling state into a hang of every verify on that loop.

### Medium

**#1. The paused check's own budget is pinned only by luck of timing (W04 survived; W17, P01 and P06 were caught only by the same luck; repro: `test_a_check_paused_mid_check_runs_what_was_left_of_its_budget`, strict xfail "AUDIT-44 #1").**
- ADR 0009, SG36: Z3's own timeout runs on through a SIGSTOP, so "a watchdog thread in the worker calls `ctx.interrupt()` once a deadline passes on a clock that excludes paused time (the parent writes the paused total to shared memory before SIGCONT)". That was the owner's condition for pausing ("exclude paused time from the paused check's budget").
- `test_a_check_paused_past_its_timeout_keeps_its_budget` freezes the check as soon as `interrupt.attached() and not interrupt.paused()`. At that point the pool thread has not yet sent the request, so the worker is usually stopped before `_solve` sets its deadline. A check paused before its deadline is set loses nothing on either clock, and the test passes under the mutants.
  - Measured: W04 (the parent never writes the total) survived the full suite in a loaded stream.
  - Alone, at load about 2, that test failed 4 of 6 times under W04 and 6 of 6 under P01.
- Under W04, P01 or W17, a lender frozen mid-check while its lent place is used comes back UNGROUNDED "timeout" a few milliseconds after its SIGCONT, however much budget it had left. That is the budget loss SG36 exists to avoid.
- *Added:* a real worker from the shared pool runs an undecidable check with budget B (0.5 s). The parent's budget is 20 × B, so only the worker's own deadline can end it.
  - The test waits until the worker has used 2 CPU ticks (`/proc/<pid>/stat`), which is evidence that `_solve` has set its deadline. It then pauses the worker for 2 × B and continues it.
  - The check ran at most `before` (wall time from request to pause), so at least B − `before` is left. The test asserts the time after SIGCONT is ≥ half that.
  - When the machine is too slow for the pause to land in the first half of B, the test doubles B and retries, up to 4 times.
  - Today: 5 of 5 pass, after-SIGCONT time ≈ 0.45 s. Under W04: 3 of 3 fail, with 5–10 ms after SIGCONT.
- *Suggested:* in SG36's test, freeze only once the worker has started the check (as here), and assert on the time after the thaw rather than on total wall time ≥ 3 × the pause.

**#2. A second pause replaces the first instead of adding to it (W06, I08; repro: `test_a_workers_pauses_add_up`, `test_a_checks_frozen_spans_add_up`, strict xfails "AUDIT-44 #2").**
- `_balance` freezes and thaws a lender each time a lent place comes and goes, so one check is often paused several times. Every SG36 test pauses once.
- With `=` for `+=` in `Worker.resume` (W06), the shared total the worker's watchdog reads goes *down* at the second pause. The worker's deadline then jumps forward by the first pause, and the parent's wait for the reply shrinks by as much.
- With `=` in `_Interrupt.thaw` (I08), `ran_s` counts the earlier frozen spans as run time, so hardness and `solver_ms` include them.
- *Added:* both on fake clocks. A worker paused for 2 s and then 3 s must report 3.5 during the second pause, then 5.0, and write 5.0 for the worker (W06 gives `(3.5, 3.0, 3.0)`). A 10 s call frozen for 2 s and then 3 s must have run 10 s (I08: 12).

**#3. A worker's earlier pauses must not move a later check's deadline (W09, P07, P08; repro: `test_earlier_pauses_do_not_extend_a_calls_wait`, `test_a_worker_paused_before_decides_its_next_checks_in_their_budget`, strict xfail "AUDIT-44 #3" under W09).**
- A worker is reused across checks, and its paused total only grows. Two subtractions keep that total out of each new check:
  - `_poll` subtracts the total at the start of each wait.
  - `_solve`'s deadline is on `_now`, which subtracts it too.
- Every test starts from a fresh worker whose total is 0, so dropping either subtraction passes the suite.
  - W09: a worker once paused for 1000 s waits 1000 s for a reply that never comes.
  - P08: the next check runs on a deadline 1000 s away.
  - P07: with the loading poll on `time.monotonic`, a loaded pool is cut off at once ("timeout").
- *Added:*
  - A piped worker with `_paused_s = 1000` must give up a 0.05 s call within `_WAIT_S`.
  - With the worker's `paused_s()` reading 1000, a loaded 120-premise pool must decide (`not_entailed`), and an undecidable 0.3 s check must time out within `_WAIT_S`. A run that fails interrupts its own Z3 context, so it cannot outlive the test.

**#8. A lender that stops lending must leave the lender list (R10, R11; repro: `test_a_lent_place_pauses_the_slot_check_that_lent_it[slow, ends]`, strict xfail "AUDIT-44 #8" under R11).**
- `run` passes its interrupt to `unstick` both when its stuck check moves to a slow place (R10) and when it ends (R11), so `_Slots` drops it from `_lenders`.
- With a bare `unstick()` the interrupt stays in the list, first in line. The next lent place freezes that stale entry, and not the slot check that lent it: either a check now on a slow place, or a finished interrupt, which freezes nothing. The real lender runs on beside the lent check, which is the AUDIT-34 #2 oversubscription SG36 fixes.
- Only `_Slots` unit tests pass a lender to `unstick`. No executor test has a second lender after the first has left.
- *Added:* 1 slot and 1 slow place, on a frozen fake clock.
  - A holds the slow place. B sticks in the slot, then leaves it: to the slow place when A ends, or by ending. C sticks. A newcomer is lent C's place.
  - Expected pauses `['pause C']`. Under R11: `[]`. Under R10: `['pause B']`, which freezes the slow place's check.

**#9. A frozen check that ends before it gets a place must withdraw its request and hold none (R14, R07; repro: `test_a_late_freeze_leaves_every_place_free`, strict xfail "AUDIT-44 #9" under R14).**
- `_again` waits for a place or for the check to end. When the check ends first (it had left its worker as it was frozen), `place.cancel()` withdraws the queued `acquire`, and `run` records that it holds nothing.
- Under R14 the request stays queued, is granted once a place frees, and is never released: the executor loses that slot for good. Under R07, `run`'s `finally` releases a place it does not hold.
- AUDIT-40 #3's test reaches this path as SG36 retargeted it, but checks only the result and how soon it came back, not the places afterwards.
- *Added:* AUDIT-40's `_late_stop`, then `(held, lent, stuck, waiters)` once every check has ended. Expected `('kept', 0, 0, 0, 0)`. Under R14: `held == 1`. Under R07: `held == -1`.

**#11. A check detached while its call is frozen must be continued (I07; repro: `test_a_check_detached_while_frozen_is_continued`, strict xfail "AUDIT-44 #11").**
- `_Interrupt.detach` resumes a frozen call's target, because `check_entailment` then hands the worker back to the pool.
- A freeze can land after the worker has replied but before the detach. Without that resume (I07), a stopped process goes back to the idle list. The next check given that worker waits for ever, because its wait excludes paused time and nothing will send SIGCONT.
- SG36's one test of `detach` on a frozen call thaws first, so the resume in `detach` is never needed there.
- *Added:* attach, freeze, detach, thaw. Expected `['pause a', 'resume a']`. Under I07: `['pause a']`.

### Low

Each low finding has a plain test that passes today and a strict xfail "AUDIT-44 #n" that applies the mutant (for #14, that forces the race).

- **#4 (P05). The `_SETUP_SHARE` guard's two times are unpinned on the paused clock.**
  - With `started` and `took` on `time.monotonic`, a check paused during its consistency check counts the pause as Z3 work. It then skips its entailment check ("too little time left"), giving UNGROUNDED for a claim with its whole budget left.
  - P06, the time left on `time.monotonic`, is caught only by #1's timing-dependent test.
  - *Added:* during the first `Solver.check`, the worker's paused total grows as the worker would see it, with no time passing on its clock. The check is stopped for 3 × its 0.2 s budget. Expected VALID. Under P05 (and P06, P01): UNGROUNDED, "too little time left for the entailment check".
- **#5 (W10). The parent's wait for a paused worker may spin.**
  - Without `+ self._paused()` in `_poll`'s pipe timeout, once the wall-clock budget has passed, the loop polls with timeout 0 until the pause ends. The pool thread of a frozen lender then burns the CPU SG36 freed.
  - *Added:* the calling thread's CPU time over a 0.5 s pause must be under 0.2 of the wall time. Measured about 0.0 today, and 0.96–0.97 under W10.
- **#6 (W07). "One reaped meanwhile is not signalled" is pinned for a gone pid, not a reused one.**
  - SG36's test passes a reaped pid with `exitcode` None, which only the `ProcessLookupError` handler sees.
  - Without the `exitcode` check, a worker that has ended is signalled by pid. Once the pid is reused, SIGSTOP goes to an unrelated process.
  - *Added:* `exitcode = -9` and the pid of a live `sleep`. Expected `(False, 'running')`. Under W07: `(True, 'T')`; the unrelated process is stopped.
- **#7 (W16, W19). AUDIT-40 #6's production half is fixed but unpinned.**
  - SG36 routes `prestart`'s and `discard`'s new workers through `release`, which re-checks `idle_max` under the lock.
  - AUDIT-40 #6's repro was fixed in the test instead (the executor is now built first), so reverting either line passes the suite.
  - *Added:* a fake `_start` that returns another caller's worker to the idle list while it starts. Expected `(1, 1)` idle/idle_max. Under W16 or W19: `(2, 1)`.
- **#10 (R02). "It gives way again as soon as a short caller queues" is pinned only for a short caller that queues later.**
  - A short caller may already be waiting when the newcomer gets its place back. `run` then skips the slice wait and gives way at once.
  - Without the skip, the newcomer waits on a fresh `short_queued()` future that only a later short caller completes, so the waiting caller waits a whole slice.
  - *Added:* s2 queues inside `_again` as the newcomer gets its place back, and the slice is then made far longer than the test. Expected `['s1', 's2']` within `_WAIT_S / 2`. Under R02: `['s1', 's2 still waiting']`.
- **#12 (L09). Two newcomers that gave way must share one `short_queued` signal.**
  - A fresh future per call wakes only the last newcomer to ask; the others give way a slice late.
  - *Added:* two calls must return futures that are both done once a short caller queues. Under L09: `(False, True)`.
- **#14. The AUDIT-34 #1 repro times the newcomer's return by its thaw, not by its grant.**
  - `test_a_newcomer_that_gives_way_to_a_short_caller_is_served_before_hard_runs` records "new" when the paused check is continued. That is a loop turn or two after `_again` gets its place.
  - If the blocker's slot frees in that window, h1 is granted the slot and its check runs on a pool thread first. The order then reads new, short, h1, though the newcomer was served first.
  - It failed this way once in this audit's mutant streams (under W02, which changes nothing on Linux): `['new', 'short', 'h1', 'new', 'h2']`. It passed 40 of 40 alone at load 7.
  - *Added (strict xfail only):* `_again` is wrapped to wait, after it has been granted its place (`granted == [True]`), until the slot has been handed on. Result: `['new', 'short', 'h1', 'h2', 'new']`.
  - *Suggested:* record the order at the grant, or release the blocker only after the newcomer has resumed.
- **#15 (R13). A paused newcomer cancelled just as it gets its place back must return the place.**
  - `_again`'s cancellation handler releases a place granted in the same loop turn. SG36's cancellation test cancels while the request is still queued, so only the other branch runs.
  - *Added:* the newcomer is cancelled from inside the `release` that grants it its lent place back. Expected `(0, 0, 0, 0)` at the end. Under R13: `lent == 1`, leaked.
- **#16. Three flipped repros do not fail on the pre-fix code on their own assertions** (see the next section).
  - AUDIT-34 #2 and #3 patch `Worker.pause/resume`, so they fail pre-fix on `AttributeError` (setup). With no-op shims they fail on their assertions, so the adaptation keeps its strength.
  - The rewritten AUDIT-39 #3 patches `_Interrupt.freeze`. With shims it times out (`asyncio.TimeoutError` after 30 s) instead of returning "interrupted", so it no longer reproduces AUDIT-39 #3. It pins SG36's own path (a check attached while its call is frozen runs once thawed), but it does not assert that this check started paused; I06 is caught only by SG36's unit test.
  - *Suggested:* use `getattr(..., None)` guards in `_running`, so the AUDIT-34 repros run on any code. Name the AUDIT-39 #3 test for what it now checks, and keep the original as history.

### Notes (no finding)

- **Load-sensitive tests not marked `perf`:**
  - SG36's retargeted AUDIT-40 #3 test bounds the late-freeze return by 20 slices (1 s) of wall time.
  - `test_the_watchdog_runs_on_the_workers_clock` fails falsely if the main thread is descheduled for more than 0.1 s between entering the watchdog and setting the paused total.
  - `test_a_paused_worker_is_stopped_and_its_call_outlives_its_budget` has a 0.2 s margin on a sleeping worker.
  - Each is the AUDIT-8 H1 pattern; none failed in this audit's runs.
- **`test_every_loading_check_runs_under_a_limit`** (both copies) checks that a thread named `z3-watchdog` exists during each loading check. It would pass with a watchdog that never interrupts, though P03 and P10 are caught elsewhere.
- **Carried:**
  - Coverage targets are not enforced by `make test` or CI (AUDIT-8 M5).
  - `state/checkpointer.py` is at 80% (AUDIT-8 M6).
  - `perf` tests run in the default run (AUDIT-8 H1); the base run's one failure was one of them.
  - `run`'s pid check (`solver.py` 912) is never reached in-process.
  - PLAN.md's SG36 row says the 5 loaded `make test` runs have not been repeated since the last fix. This audit's base run, on a loaded machine, had 1 `perf` failure.

## SG36's test changes

| Test | Change | Checked against | Verdict |
|---|---|---|---|
| test_audit_34 #2 (`test_z3_checks_never_outnumber_…`, the `perf` fresh-tasks test) and #3 (`…_as_before_sg34`) | count calls in flight whose worker is not paused (`_running`), not every call; `(peak, restarts) == (1, 0)` and `(peak, calls) == (2, 3)` | pre-fix with shims: `(2, 2) ≠ (1, 0)`, `(3, 3) ≠ (2, 3)`; R12 (no lender) killed by #2 | **sound**: the counted peak is exact, not `≤`. It measures the parent's pause calls, not processes; AUDIT-38 #2's /proc test (`perf`) measures processes. Fails pre-fix only on setup (#16) |
| test_audit_39 #3, now plain | patches `freeze` instead of `request`; expects the second check `entailed` | pre-fix with shims: times out | **a different test now** (#16) |
| test_audit_36 P04 xfail removed | its line `if not stopped or self._closed:` is gone | the plain test still passes and reaches a frozen newcomer at shutdown | **valid**: a frozen check is never run again, so there is no re-run to refuse |
| test_audit_37 `timeout(300)` ×2 | per-test timeout 60 → 300 s | the fixture took 24 s in the base run at load 5–8 | **not a weakening**: it bounds a hang, not an assertion |
| test_solver_pausing paused-budget test | `_KILL_GRACE_SHARE = 10`; asserts reason `timeout`, not killed or interrupted, instead of exact details | P10 (watchdog never fires) still killed by the parent-kill clause | **edits sound**; the test itself is timing-dependent (#1) |
| retargeted strings: AUDIT-32 `_S04` (`_poll`), AUDIT-36 `_Y09`, AUDIT-40 U02, AUDIT-40 #3's U01 → `_again`'s wait | as named | `--runxfail` at HEAD: each fails on its own assertion (`SolverOutcome` mismatch; yield table; `(1, 1) ≠ (0, 1)`; `('kept', 1, False)`) | **valid**. AUDIT-40 #3's xfail now pins "returns before the slot frees", not "not re-run" (SG36 never re-runs) |
| AUDIT-24 `_ASSUME`, `_L01`, `_L06` → `_decide`/`_solve`; #5 (L02) and SG27's `_RESET_SHARE` tests deleted | `_RESET_SHARE` and the per-check Z3 timeout are gone | L06 xfail fails on its assertion; L01's plain test checks the watchdog thread | **valid**; see the loading-limit note |
| AUDIT-20 S09, AUDIT-8 S08 rewritten | one deadline; repeated interrupts past it | P03 killed by S08 | **valid** |
| AUDIT-40 #5, #6 un-xfailed | AUDIT-14 #1 recalibrates until its entailment check starts, with timeout(180); the executor is built before patching `_workers` | pre-fix tests: "DID NOT RAISE", `3111.0 ≠ 2111` | **fixed in the tests**; the production half of #6 is #7 |

## Repros flipped by SG36, against the pre-fix code

Run on `9792264`, `--runxfail`, twice; HEAD passes all 10.

| Repro | Result on pre-fix code (2/2) | Own assertion? |
|---|---|---|
| AUDIT-34 #2 `test_z3_checks_never_outnumber_the_slots_and_slow_places` | `AttributeError: Worker has no attribute 'pause'`; with shims `(2, 2) ≠ (1, 0)` | setup (#16); with shims yes |
| AUDIT-34 #2 `test_runs_that_check_in_fresh_tasks_waste_little_z3_time` (`perf`) | "3.8 of 20.5 Z3 s wasted on 15 re-runs, 20 restarts, 5.13 checks at once" | yes |
| AUDIT-34 #3 `test_slow_checks_are_bounded_by_slow_workers_as_before_sg34` | `AttributeError`; with shims `(3, 3) ≠ (2, 3)` | setup (#16); with shims yes |
| AUDIT-35 #2 `test_run_calls_fn_once` | "fn was called 2 times" | yes |
| AUDIT-38 #1 `test_a_newcomer_that_gave_way_is_served_before_the_slot_frees` | "still waiting for the slot (1, 0, 1); order ['new', 'short', 'new']" | yes |
| AUDIT-38 #2 `test_z3_processes_running_at_once_never_outnumber_the_places` (`perf`) | "more than 1 Z3 process running in 46% / 43% of samples" | yes |
| AUDIT-39 #1 `test_a_done_sub_goal_leaves_none_of_its_flips_open` (pre-fix `tests/audit`, `PLAN.md`) | `['AUDIT-34 #2', 'AUDIT-34 #3']` still open | yes |
| AUDIT-39 #3 `test_a_stop_between_two_checks_of_one_fn_is_never_its_result` | `AttributeError: _Interrupt has no attribute 'freeze'`; with shims a 30 s `TimeoutError` | no (#16) |
| AUDIT-40 #5 (pre-fix `test_audit_14.py`) | "DID NOT RAISE" | yes |
| AUDIT-40 #6 (pre-fix `test_solver_executor.py`) | `assert 3111.0 == 2111` | yes |

## Added tests (`tests/audit/test_audit_44.py`)

| Test | Kills / reproduces |
|---|---|
| test_a_check_paused_mid_check_runs_what_was_left_of_its_budget (+ xfail `_under_w04`) | W04, W17, P01, P05, P06; #1 |
| test_a_workers_pauses_add_up (+ xfail `_under_w06`), test_a_checks_frozen_spans_add_up (+ xfail `_under_i08`) | W06, I08 (and W04); #2 |
| test_earlier_pauses_do_not_extend_a_calls_wait (+ xfail `_under_w09`), test_a_worker_paused_before_decides_its_next_checks_in_their_budget | W09, P07, P08; #3 |
| test_a_check_paused_in_its_consistency_check_still_decides (+ xfail `_under_p05`) | P05, P06, P01; #4 |
| test_a_call_waits_for_a_paused_worker_without_spinning (+ xfail `_under_w10`) | W10; #5 |
| test_a_worker_that_has_ended_is_never_signalled (+ xfail `_under_w07`) | W07; #6 |
| test_a_start_never_leaves_more_than_idle_max_idle[prestart, discard] (+ xfail `_under_w16`) | W16, W19; #7 |
| test_a_lent_place_pauses_the_slot_check_that_lent_it[slow, ends] (+ xfail `_under_r11`) | R10, R11; #8 |
| test_a_late_freeze_leaves_every_place_free (+ xfail `_under_r14`) | R14, R07; #9 |
| test_a_newcomer_that_gave_way_gives_way_to_a_short_caller_already_waiting (+ xfail `_under_r02`) | R02; #10 |
| test_a_check_detached_while_frozen_is_continued (+ xfail `_under_i07`) | I07; #11 |
| test_every_newcomer_waiting_on_a_short_caller_is_signalled (+ xfail `_under_l09`) | L09; #12 |
| test_a_newcomer_back_on_a_slot_waits_its_slices (+ xfail `_under_r01`) | R01; #13 |
| xfail test_the_audit_34_1_repro_passes_when_the_newcomer_resumes_late | #14 |
| test_a_newcomer_cancelled_as_it_gets_its_place_back_returns_it (+ xfail `_under_r13`) | R13; #15 |

- Waits are bounded by events, by a fake clock, by /proc evidence that the worker is running its check, or by `_WAIT_S`.
- The one wall-clock comparison (#1) is between two spans of the same run, and is recalibrated when the machine is too slow. #5 compares a thread's CPU time with its own wall time.
- Under `--runxfail`, all 16 xfails fail on their own assertion, not on setup.

Verification:
- `make lint typecheck`: ruff clean, 170 files already formatted, mypy strict clean on 154 source files.
- `uv run pytest tests/audit/test_audit_44.py -q`: 18 passed, 16 xfailed. With `--runxfail`: 16 failed, each on an `assert`; 18 passed.
- Full coverage run with this file (the audit's command): 1498 passed, 13 skipped, 129 xfailed, 0 failed in 10 min 30 s (the base run plus 18 passed and 16 xfailed from this file; the perf test that failed under load in the base run passed). Coverage was unchanged: 97.5% overall and 97.6% on `verification/`.
