# AUDIT-36: Test coverage and test quality (round 9)

Branch `claude/audit-36`, based on `origin/claude/sg34-together-fork` (`10ae977`, SG34). This is an independent audit. No production code or existing test was changed.

**Bottom line:** coverage is high and the suite is green on a quiet machine: 97.5% line and branch overall, 97.6% on `verification/` (target ≥90%). Every round-8 repro SG34 flipped fails against the pre-fix code (`dcef66c`) on its own assertion, 2 of 2 runs, and passes on HEAD. The scheduling half of SG34 is well pinned where it was the point of the change (latest-first lending, re-queueing a stopped newcomer, `_LENT_AGING` ≥ comparisons). The fork half is not: all of it runs only in a forked child, so coverage never sees it, and the one fork test exits its child with `os._exit`. I ran 62 one-line mutations by hand over the code SG34 changed:
- The existing suite killed 31.
- 15 are equivalent (7 of them only because the at-fork hook and the pid check duplicate each other).
- 16 survived.

`tests/audit/test_audit_36.py` adds a plain test that kills each non-equivalent survivor, and a strict xfail that applies the mutant.

Severity counts: **0 critical, 0 high, 3 medium, 6 low.** The main problems:
- **A forked child that exits normally kills every idle worker of its parent (#1, medium).** SG34 says a child "never us[es] or kill[s] the parent's" workers. That holds only for `os._exit`, which every fork test uses. On `sys.exit` multiprocessing's own atexit hook SIGTERMs the parent's workers (exit code -15, 5 of 5 idle workers, in each of 3 runs). This is a real defect, not only a test gap; the repro is a strict xfail.
- **The at-fork lock resets are untested (#2, medium).** Dropping the reset of `_default_lock`/`_workers_lock`, `Workers._lock` or the forkserver's lock, or not registering `solver.py`'s hook at all, passes the whole suite; the child then hangs for ever on its first check whenever another thread held that lock at the fork (the prestart thread does, while it boots the forkserver).
- **Which checks SG34 stops is unpinned (#6, medium).** Stopping a newcomer's check that holds a *slot* (Y04) passes everything, and kills the first run of every hard check made from a fresh task. Four more slips in `_yields`/`short_waiting` pass too.
- **The 87/88 xfail count:** a teardown error is counted as a second XFAIL (#8). `test_audit_6::test_reused_graph_memory_does_not_grow_per_run` calls `gc.collect()`, which sometimes finalizes an event loop and its socket pair leaked by an earlier test. pytest turns that ResourceWarning into a teardown error, and the strict xfail marker reports that as a second XFAIL of the same test.

## How this was measured

- **Coverage:** `uv run pytest --cov=autolab --cov-branch --cov-report=term-missing --cov-report=json tests` on the base. Package figures are (covered lines + covered branches) / (lines + branches), from the JSON report. That run gave 1429 passed, 12 skipped, 88 xfailed and **1 failed**: AUDIT-30 #1's first `perf` repro ("waited 0.78 s = 1.9 hard checks", bound 1.5). It ran with two mutant streams on the same 4 CPUs (load average 4–5); the same test passed 2 of 2 on a quiet machine (0.4 hard checks or less), and the final run with this file was green (see the end). Load sensitivity of `perf` tests in the default run is the carried AUDIT-8 H1.
- **Worker and child processes:** as in AUDIT-28/32, pytest-cov cannot see code that runs only in a worker or in a forked child. All of SG34's fork code is child-only (`solver.py` 772, 903-905; `solver_workers.py` 283-294, shown uncovered below). It was measured by mutants instead: the mutated copy is first on `PYTHONPATH`, so forkserver children and the subprocess fork scripts load it.
- **Mutations:**
  - A scratchpad harness (`a36/harness.py`, not committed) copies `src/`, `tests/`, `scripts/` and `pyproject.toml` to a fresh directory per mutant, applies exact-match replacements (each asserted to occur once), checks that `autolab` is imported from the copy, and runs pytest there. The worktree was never edited; every copy was deleted after its run.
  - Each mutant ran `pytest -x -q -m "not perf" tests/unit/verification tests/audit` (937 collected, 57 `perf` deselected), without `test_audit_36.py`. The `perf` tests were not run against the mutants.
  - Each of the 16 survivors was then run against `test_audit_36.py`'s plain tests alone, in a fresh copy: each was killed by the test named for it, and by no other.
  - Two streams ran in parallel on 4 CPUs. Killing tests use fake clocks, events or pipes, so load cannot make a kill.
  - A fork-side mutant cannot be applied with `monkeypatch`: the hook registered by `os.register_at_fork` is the original function object. `test_audit_36.py` swaps its `__code__` inside the fresh interpreter that forks.
- **Earlier repros:** `solver.py` and `solver_workers.py` from `dcef66c` (origin/claude/audit-round-8) in a fresh copy, HEAD's tests run there with `--runxfail`, twice; HEAD alone as control, twice.
- **Retuned tests:** each run alone against the mutant of what it used to guard (Q01, D01, A01, N01), twice.
- **xfail count:** the `-ra` XFAIL lists of four full or audit-only runs were compared, the audit suite was re-run with `--runxfail` (which turns the doubled XFAIL into an `ERROR at teardown` with the warning text), and twice with a plugin that calls `gc.collect()` after every test (87 xfailed, no duplicate, no teardown error).
- **#1:** a scratchpad script forks after the pool started, the child does one check, and it exits with `os._exit(0)`, `sys.exit(0)`, or `sys.exit(0)` without a check; the parent then reads its idle workers' `alive()` and exit codes. 3 runs each.

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
| verification/solver.py | 567 | 5 | 176 | 4 | 99% | **772, 903-905** (child only, #2), 908->912 (no `register_at_fork`), **836** (#6), 926->928 |
| verification/solver_workers.py | 201 | 15 | 44 | 1 | **90%** | 48-52 (worker only), **283-288, 292-294** (child only, #2), 297->301, 309-310 (atexit) |
| verification/llm.py | 69 | 1 | 16 | 2 | 96% | 189, 219->218 |
| verification/vericot.py | 130 | 1 | 52 | 1 | 99% | 247 |
| verification/ (other 4) | 71 | 0 | 18 | 0 | 100% | |
| **TOTAL** | 2749 | 45 | 710 | 29 | **97.5%** | |

| Package | Lines | Branches | Cover (line + branch) | Target |
|---|---:|---:|---:|---|
| verification/ | 98.3% | 95.3% | **97.6%** | ≥90% ✔ |
| agents/ | 99.8% | 98.8% | 99.6% | ≥70% ✔ |
| orchestrator/ | 100% | 100% | 100% | ≥70% ✔ |
| monitoring/ | 100% | 97.4% | 99.5% | ≥70% ✔ |
| state/ | 97.2% | 78.4% | 94.2% | ≥70% ✔ |
| tools/ | 95.9% | 84.6% | 93.9% | ≥70% ✔ |
| cli.py | 92.4% | 90.9% | 92.1% | ≥70% ✔ |

- `verification/` fell from 98.7% (round 8) to 97.6%: SG34 added 75 statements and 30 branches, and 12 of the new lines run only in a forked child. `solver_workers.py` fell from 94% to 90%.
- Line 836 (`_yields` with no check attached) is reachable in-process and was uncovered; `test_audit_36.py` covers it.
- Child-only code stays uncovered with this file too; it is measured by the fork mutants (F02, F05, K06, K07, K11, K14 killed; the rest equivalent).

## Mutation table

ID prefixes: L/A/R/G `_Slots` lending (`_LENT_AGING`, `_lent_rank`, `_wake`); Y/P newcomer stop and re-queue (`short_waiting`, `_yields`, `run`); F fork adoption in `solver.py`; K fork adoption in `solver_workers.py`; Q/D/N guards of the retuned tests. "→" names the `test_audit_36.py` test that now kills a survivor.

| ID | Mutation | Result | Killed by |
|---|---|---|---|
| L01 | `_LENT_AGING = 1` | killed | test_solver_scheduling::test_newcomers_are_lent_places_latest_first |
| **L02** | `_LENT_AGING = 3` | **survived** | → test_a_newcomer_is_overtaken_for_two_lent_places_per_slot_at_most (#3) |
| **L03** | `_LENT_AGING = 1000` (no lent aging) | **survived** | → same (#3) |
| A01 | `acquire` grants a newcomer no lent place | killed | test_runs_that_start_together_do_not_hold_up_a_later_newcomer |
| A02 | `_wake` lends no place to a newcomer | killed | test_newcomers_are_lent_places_latest_first |
| R01 | newcomers lent in arrival order | killed | same |
| R02 | short callers lent latest first | killed | test_a_stuck_slot_lends_one_place_to_a_short_caller_only |
| **R03** | newcomers lent before short callers | **survived** | → test_a_short_caller_is_lent_a_place_before_a_newcomer (#5) |
| R04 | lent aging at `>` | killed | test_a_newcomer_overtaken_for_every_lent_place_twice_is_lent_the_next |
| R05 | an aged caller ranks last | killed | same |
| **R06** | lent aging by `_LENT_AGING` in all, not per slot | **survived** | → test_a_newcomer_is_overtaken_for_two_lent_places_per_slot_at_most (#3) |
| G01 | `_wake` always uses the slot order | killed | test_newcomers_are_lent_places_latest_first |
| G02 | `_wake` always uses the lent order | killed | test_slots_are_granted_in_order |
| G03 | `lent_past` never counted | killed | …_twice_is_lent_the_next |
| G04 | short callers do not age on lent places | survived (**equivalent**) | a later caller can only be lent a place ahead of a short one once aged, and it ages only from grants to callers later still, which rank below the short one |
| G05 | slot aging counts lent grants too | killed | test_a_lent_place_does_not_overtake_a_caller_that_could_not_take_it |
| G06 | hard callers age on lent places too | survived (**equivalent**) | a hard caller is never lent a place, whatever its rank |
| Y01 | `short_waiting` counts finished waiters | survived (**equivalent**) | a served waiter leaves `_waiters` before its future is set; a cancelled one is removed by its own task before the loop runs `_yields` again |
| Y02 | `short_waiting` always False | killed | test_a_short_caller_takes_a_lent_place_from_a_newcomer_after_a_slice |
| **Y03** | `short_waiting` counts newcomers | **survived** | → test_a_lent_newcomer_yields_only_as_documented (#6) |
| **Y04** | a newcomer in a slot is stopped too | **survived** | → test_a_hard_check_in_a_slot_or_of_a_short_caller_is_never_stopped (#6) |
| **Y05** | a short caller on a lent place is stopped too | **survived** | → same (#6) |
| Y06 | nothing is ever stopped | killed | test_runs_that_start_together_do_not_hold_up_a_later_newcomer |
| Y07 | no yield to a waiting short caller | killed | test_a_short_caller_takes_a_lent_place_from_a_newcomer_after_a_slice |
| Y08 | no stop at `_HARD_SLICES` | killed | test_runs_that_start_together_… |
| **Y09** | yields to a short caller before one slice | **survived** | → test_a_lent_newcomer_yields_only_as_documented (#6) |
| Y10 | stop at 2 × `_HARD_SLICES` | killed | test_runs_that_start_together_… |
| **Y11** | `_yields` without an attached check | **survived** | → test_a_lent_newcomer_yields_only_as_documented (#6) |
| P01 | stopped without `interrupt.request()` | killed | test_runs_that_start_together_… |
| P02 | no `break` after the stop | survived (**equivalent**) | the loop asks again (request is idempotent) until the killed worker's future ends, a slice at most |
| P03 | a stopped check's result returned | killed | test_runs_that_start_together_… |
| **P04** | `or self._closed` dropped | **survived** | → test_a_newcomer_stopped_as_shutdown_starts_gets_its_result (#4) |
| P05 | a check stopped after a slice is recorded by its run time | survived (**equivalent**) | the re-run, in the same `run`, records the task again before any later `run` of that task reads it |
| P06 | a check stopped as hard is not recorded | survived (**equivalent**) | same |
| P07 | `new` kept after a stop | killed | test_runs_that_start_together_… |
| P08 | `short = False` dropped after a stop | survived (**equivalent**) | `new` implies `not short`, and only a newcomer is stopped |
| F01 | `run` has no pid check | survived (**equivalent**) | the at-fork hook adopts every executor first |
| **F02** | the hook keeps the parent's `_default_lock`, `_workers_lock` | **survived** | → test_a_lock_held_at_the_fork_is_free_in_the_child[default_lock, workers_lock] (#2) |
| F03 | the hook adopts no executor | survived (**equivalent**) | `run`'s pid check adopts it before taking any lock |
| F04 | executors not listed in `_executors` | survived (**equivalent**) | same |
| **F05** | `solver.py`'s hook never registered | **survived** | → same as F02 (#2) |
| K01 | `Workers._own` never adopts | killed | test_solver_workers::test_a_pool_used_in_another_process_adopts_none_of_the_owners_workers |
| K02 | idle pipes left open in the child | killed | same (white-box) |
| K03 | the child keeps the parent's idle workers | killed | test_a_forked_child_starts_its_own_workers_and_leaves_the_parents_alone |
| K04 | the child keeps `_live` | killed | same |
| K05 | the child keeps `_ctx` | killed | test_a_pool_used_in_another_process_… (white-box) |
| **K06** | the child keeps the pool's `_lock` | **survived** | → test_a_lock_held_at_the_fork_is_free_in_the_child[pool_lock] (#2) |
| **K07** | `_adopt` does not record the child's pid | **survived** | → test_a_forked_child_reuses_its_own_worker (#7) |
| K08 | `close` has no `_own` | killed | test_a_pool_used_in_another_process_… |
| K09 | `acquire` has no `_own` | survived (**equivalent**) | the hook adopts every pool first |
| K10 | `prestart` has no `_own` | survived (**equivalent**) | same |
| **K11** | the parent forkserver's alive fd kept in the child | **survived** | → test_the_child_closes_its_copy_of_the_forkservers_alive_pipe (#2) |
| K12 | the forkserver's pid kept | killed | test_solver_executor::test_solver_ms_excludes_worker_start_up (ChildProcessError in the fork test too) |
| K13 | the forkserver's address kept | survived (**equivalent**) | `ensure_running` sees no pid and overwrites the address |
| **K14** | the forkserver's `_lock` kept | **survived** | → test_a_lock_held_at_the_fork_is_free_in_the_child[forkserver_lock] (#2) |
| K15 | the forkserver not forgotten | killed | test_a_forked_child_starts_its_own_workers_… |
| K16 | the hook adopts no pool | survived (**equivalent**) | `_own` adopts on first use; `_adopt` takes no lock |
| K17 | pools not listed in `_made` | survived (**equivalent**) | same |
| K18 | `solver_workers.py`'s hook never registered | killed | test_a_forked_child_starts_its_own_workers_… |
| Q01 | `solver_ms` counted from the call to `run` (queueing included) | killed | test_run_time_excludes_waiting_for_a_worker (and the retuned test, see below) |
| D01 | slow cap at `>` (one slow check too many) | killed | test_solver_scheduling::test_slow_checks_never_exceed_slow_workers (not the retuned test) |
| N01 | newcomers rank as hard in slot order | killed | test_a_new_runs_first_check_is_served_before_hard_runs |

**Totals:** 62 mutations.
- The existing suite killed 31.
- 15 are equivalent: G04, G06, Y01, P02, P05, P06, P08, F01, F03, F04, K09, K10, K13, K16 and K17. Seven of them (F01, F03, F04, K09, K10, K16, K17) are equivalent only because the at-fork hook and the pid check do the same job; each half of that redundancy is untested alone (a note, not a finding).
- 16 non-equivalent survived: L02, L03, R03, R06, Y03, Y04, Y05, Y09, Y11, P04, F02, F05, K06, K07, K11 and K14. Each is killed by a plain test in `test_audit_36.py`, checked in a fresh mutant copy, and a strict xfail applies the mutant and runs that test (F05 has no xfail of its own: unregistering a hook cannot be undone in-process; it is killed by the same tests as F02).

## Findings

### Critical

None.

### High

None. No mutant, surviving or killed, lets a check come back VALID that should not: every stop SG34 adds re-runs the check (P03, P07 killed), and a stopped check's result never reaches its caller unless the executor is closing (#4).

### Medium

**#1. A forked child that exits normally kills every idle worker of its parent (repro: `test_a_forked_child_that_exits_normally_never_kills_the_parents_workers`, strict xfail "AUDIT-36 #1").**
- The child of `os.fork` inherits `multiprocessing.process._children`, which lists the parent's worker processes (daemons). SG34's `_adopt` forgets the parent's workers in `Workers`, but not there. On a normal exit, multiprocessing's atexit hook (`util._exit_function`) finds them "alive" (their sentinels are open copies), sends each SIGTERM, then fails `AssertionError: can only join a child process`.
- Measured, 3 runs each: after `os._exit(0)` the parent's 5 idle workers are alive; after `sys.exit(0)`, with or without a check in the child, all 5 are dead (exit code -15 in 27 of 30 readings, 255 in the other 3, where the status could not be read; all 30 dead). The parent recovers on its next `acquire` (VALID after), at the cost of a boot per worker, but a check running at that moment would lose its worker.
- Who exits normally: a prefork server's worker (gunicorn's ends with `sys.exit`), or any helper an app forks and lets return. A `multiprocessing` fork child is not affected (`_bootstrap` resets `_children`).
- Why the suite misses it: SG34's `test_a_forked_child_starts_its_own_workers_and_leaves_the_parents_alone`, `audit_load_8.FORK_CHILD` and AUDIT-29's script all end the child with `os._exit(0)`, which skips atexit. The test asserts `alive` for exactly the claim that fails.
- *Added:* the repro (child ends with `sys.exit(0)`) and its control (`os._exit(0)`, passes).
- *Suggested fix:* in `_adopt`, also discard the owner's worker processes from `multiprocessing.process._children` (or clear the set in the at-fork hook, as `_bootstrap` does).

**#2. The at-fork hooks' lock resets are untested and child-only (F02, F05, K06, K14, K11 survived; `solver.py` 903-905, `solver_workers.py` 283-294 uncovered; repros: `test_a_lock_held_at_the_fork_is_free_in_the_child_under[…]`, `…_alive_pipe_under_k11`, strict xfails "AUDIT-36 #2").**
- SG34 replaces four locks in the child: `solver._default_lock` and `_workers_lock` (hook), `Workers._lock` (`_adopt`) and the forkserver's `_lock` (`_forget_forkserver`). A lock held by another thread at the fork stays held in the child for ever unless replaced. The prestart thread that every `SolverExecutor` starts holds `_workers_lock` and then the forkserver's lock while it boots the forkserver, so "build a Verifier, then fork" (AUDIT-29's scenario) can fork with them held. AUDIT-29's script waits for prestart to finish precisely to avoid that.
- Removing any one replacement, or registering no hook in `solver.py`, passes the whole suite. The child then hangs on its first check (a pipe that never answers, bounded at 10 s by the test).
- K11: without closing the child's copy of the parent forkserver's alive pipe, the parent's forkserver cannot see its owner exit while the child lives.
- *Added:* `test_a_lock_held_at_the_fork_is_free_in_the_child[default_lock|workers_lock|pool_lock|forkserver_lock]` (a thread holds the lock across `os.fork`; the child must get VALID) and `test_the_child_closes_its_copy_of_the_forkservers_alive_pipe`.

**#6. Which checks SG34 stops is unpinned (Y03, Y04, Y05, Y09, Y11 survived; `solver.py` 836 uncovered; repros: `test_a_lent_newcomer_yields_only_as_documented_under[y03|y09|y11]`, `test_a_hard_check_in_a_slot_or_of_a_short_caller_is_never_stopped_under[y04|y05]`, strict xfails "AUDIT-36 #6").**
- The ADR amendment stops only "a newcomer's check on a lent place", once it runs `_HARD_SLICES` slices, or one slice while a short caller waits. SG34's tests reach `_yields` in two states only: a short caller waiting after exactly one slice, or nobody waiting after `_HARD_SLICES`.
- Y04 (`lent and` dropped): a newcomer's check in a *slot* is stopped too. With LangGraph's fresh task per node, every agent's first check is a newcomer's, so every hard check's first run would be killed after 4 slices and run again. Nothing notices.
- Y05 (`new and` dropped): a short caller on a lent place is stopped once its check turns hard.
- Y03: a waiting newcomer counts as a short caller, so newcomers stop each other after one slice. Y09: the one-slice minimum goes. Y11: `_yields` no longer needs an attached check.
- *Added:* `test_a_lent_newcomer_yields_only_as_documented` (a decision table over who waits, run time 0.5/1/`_HARD_SLICES` slices, check attached or not, on a fake clock) and `test_a_hard_check_in_a_slot_or_of_a_short_caller_is_never_stopped` (both kinds run a held check past `_HARD_SLICES` on a fake clock; each must be called once).

### Low

Each low finding with a mutant has a plain test that passes today, and a strict xfail `AUDIT-36 #n` that applies the mutant.
- **#3 (L02, L03, R06). Lent aging is read, not pinned.** `test_a_newcomer_overtaken_for_every_lent_place_twice_is_lent_the_next` and the property test take their bound from `_LENT_AGING`, so 3 or 1000 (no lent aging) passes; the direct test uses one slot, so aging by 2 places in all instead of per slot passes too. Slot aging still bounds every wait, so the effect is a longer wait, not starvation. *Added:* `test_a_newcomer_is_overtaken_for_two_lent_places_per_slot_at_most` (2 on one slot, 4 on two).
- **#4 (P04). A newcomer stopped while the executor shuts down.** `run` returns a stopped check's result when `_closed` is set, as `shutdown` promises callers in flight a result (AUDIT-6 #7). Without it the caller gets `RuntimeError: cannot schedule new futures after shutdown`; nothing reaches the branch. *Added:* `test_a_newcomer_stopped_as_shutdown_starts_gets_its_result`.
- **#5 (R03). Short callers before newcomers on a lent place is unpinned.** The ADR says short callers are lent places first. No SG34 test has both waiting when a place is lent. *Added:* `test_a_short_caller_is_lent_a_place_before_a_newcomer`, both arrival orders.
- **#7 (K07). A child re-adopts its pool on every call.** Without `_adopt` recording the pid, each `acquire` in the child adopts again and boots a fresh worker for every check. SG34's fork test (three verdicts, disjoint pids) passes either way. *Added:* `test_a_forked_child_reuses_its_own_worker` (one worker start for three checks in the child; 3 under K07).
- **#8. The xfail count is 87 or 88 because a teardown error is counted as an XFAIL.** No test was added; it is a property of the run, not of a function.
  - The duplicate is always `test_audit_6.py::test_reused_graph_memory_does_not_grow_per_run` (AUDIT-6 #8), listed twice in `-ra` (base coverage run: 88). Under `--runxfail` the same run shows it as `ERROR at teardown`: pytest's unraisable-exception hook found `ResourceWarning: unclosed event loop` and two `unclosed <socket.socket …>` (the loop's self-pipe), raised during the test's own `gc.collect()`. With `filterwarnings = error` that is a teardown failure; pytest reports any failing phase of a strict-xfail test as XFAIL, so the test is counted twice.
  - Whether it shows depends on whether the automatic collector already freed that garbage. With a plugin that collects after every test, 2 runs gave 87 and no teardown error anywhere, so I did not pin which earlier test leaks the loop.
  - Two consequences: the summary count is not a stable gate, and any teardown error in a strict-xfail test (79 in `tests/audit`) is reported as an expected failure. *Suggested:* close the leaking loop, or have the AUDIT-6 #8 test filter `ResourceWarning` around its `gc.collect()`.
- **#9. Two of the three retuned tests no longer guard what they did (D01, A01; no test added, both killed elsewhere).** See "SG34's retuned tests".

### Notes (no finding)

- **Carried:** coverage targets not enforced by `make test` or CI (AUDIT-8 M5); `state/checkpointer.py` at 80% (AUDIT-8 M6); `perf` tests in the default run (AUDIT-8 H1, AUDIT-20 #7), seen here as AUDIT-30 #1 failing once under mutant load.
- The at-fork hook and the pid checks duplicate each other (7 equivalent mutants). Only `test_a_pool_used_in_another_process_adopts_none_of_the_owners_workers` exercises the pid path alone, for `Workers.close`; `SolverExecutor.run`'s pid check (`solver.py` 772) is never reached.
- AUDIT-32's additions still hold on SG34: in the base coverage run `test_audit_32.py` gave 7 passed and 9 xfailed.

## SG34's retuned tests

Each was run alone against the mutant of what it used to guard, 2 of 2 runs.

| Test | Change | Mutant | Result alone | Verdict |
|---|---|---|---|---|
| test_solver_executor::test_solver_ms_excludes_queueing | wall ≥ 4× → ≥ 2× | Q01 (queueing counted in `solver_ms`) | **killed** | not weakened: `_SLOW_S` (0.2 s) equals `_HARD_SLICES` × `_SLICE_S`, so the lent check ends rather than being stopped, two run at once, and the queued ones still report over `_SLOW_S + _SLACK_S` under Q01 |
| test_solver_executor::test_slow_checks_are_bounded_by_slow_workers | `peak == 2`, `threads <= 2` → `peak <= 3`, `calls in (3, 4)`, `threads <= 3` | D01 (one slow check over the cap) | **passes** | weakened: slot + 2 slow checks is also 3 at once. Still guarded by `test_solver_scheduling::test_slow_checks_never_exceed_slow_workers` (fake clock). *Suggested:* assert the most `executor._slow` reached, which is 1 today |
| test_solver_scheduling::test_a_short_caller_overtakes_hard_checks_above_the_slow_cap | newcomer "lent none, slot before the hard run" → `await new() is False` | A01 (a newcomer is not lent at `acquire`), N01 | **passes** (A01 after 10.3 s) | weakened: nothing asserts the newcomer was lent a place. Under A01 it waits for the slot, the held check's `gate.wait(_WAIT_S)` times out silently after 10 s, and the assertion still holds. The slot order moved to `test_a_new_runs_first_check_is_served_before_hard_runs` (kills N01); A01 is killed by `test_runs_that_start_together_do_not_hold_up_a_later_newcomer`. *Suggested:* assert the newcomer's place was lent (or that `hard.gate` was not set when it ended) |

## Round-8 repros flipped by SG34, against the pre-fix code

HEAD's tests with `dcef66c`'s `solver.py` and `solver_workers.py`, `--runxfail`, twice; HEAD passed all four, twice.

| Repro | Result on pre-fix code | Verdict |
|---|---|---|
| AUDIT-30 #1 (together, `perf`) | fail 2/2: waited 2.8 and 2.8 hard checks (bound 1.5) | not weakened |
| AUDIT-30 #1 (fresh tasks, `perf`) | fail 2/2: 2.7 and 2.7 hard checks | not weakened |
| AUDIT-30 #2 (fork after build) | fail 2/2: every check "raised AssertionError: can only test a child process" | not weakened |
| AUDIT-29 #1 (child forked after build) | fail 2/2: `'AssertionError: can only test a child process' == 'VALID VALID'` | not weakened |

All four fail on their own assertion, not on setup. None of them exits its child normally, so none would catch #1.

## Added tests (`tests/audit/test_audit_36.py`)

| Test | Kills / reproduces |
|---|---|
| test_a_forked_child_that_ends_without_exiting_hard_never_kills_the_parents_workers (control) | — |
| test_a_forked_child_that_exits_normally_never_kills_the_parents_workers (xfail) | #1 |
| test_a_lock_held_at_the_fork_is_free_in_the_child[×4] (+ 4 xfails) | F02, F05, K06, K14, #2 |
| test_the_child_closes_its_copy_of_the_forkservers_alive_pipe (+ xfail) | K11, #2 |
| test_a_newcomer_is_overtaken_for_two_lent_places_per_slot_at_most (+ 3 xfails) | L02, L03, R06, #3 |
| test_a_short_caller_is_lent_a_place_before_a_newcomer (+ xfail) | R03, #5 |
| test_a_newcomer_stopped_as_shutdown_starts_gets_its_result (+ xfail) | P04, #4 |
| test_a_lent_newcomer_yields_only_as_documented (+ 3 xfails) | Y03, Y09, Y11, #6 |
| test_a_hard_check_in_a_slot_or_of_a_short_caller_is_never_stopped (+ 2 xfails) | Y04, Y05, #6 |
| test_a_forked_child_reuses_its_own_worker (+ xfail) | K07, #7 |

Waits are bounded by events, pipes or fake clocks; there is no wall-clock assertion. Fork scenarios run in a fresh interpreter (`-c`), fork there, and bound the child's answer at 10 s (a hung child is killed). Every xfail fails on its assertion under `--runxfail`, not on setup (17 of 17).

Verification:
- `make lint typecheck`: ruff clean, 159 files already formatted, mypy strict clean on 145 source files.
- `uv run pytest tests/audit/test_audit_36.py -q`: 12 passed, 17 xfailed.
- `--runxfail`: 17 failed (each on an `assert`), 12 passed.
- Full coverage run with this file (the audit's command): **1442 passed, 12 skipped, 105 xfailed** in 5 min 18 s, green; 97.5% overall, `verification/` 97.7% (`solver.py` 836 now covered). The 105 are 87 + this file's 17 + the AUDIT-6 #8 duplicate (#8), which recurred.
