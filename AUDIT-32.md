# AUDIT-32: Test coverage and test quality (round 8)

Branch `claude/audit-32`, based on `origin/claude/sg32-newcomers` (`f09ea60`, SG31–SG33 merged). This is an independent audit. No production code or existing test was changed.

**Bottom line:** coverage is high and the suite is green: 98.0% line and branch overall, 98.7% on `verification/` (target ≥90%). Every round-7 repro that SG31–SG33 flipped fails against the pre-fix code (`ebfbbb1`) on its own assertion, 2 of 2 runs, except the two AUDIT-28 #1 repros, which now fail there with `AttributeError` (#7). The soundness paths SG31 added are well pinned: every mutant that lets a stale or foreign reply through (S01, S03, S07, S19, S20) is killed. I ran 63 one-line mutations by hand over the code SG31–SG33 changed:
- The existing suite killed 50.
- 4 are equivalent.
- 9 survived, none killed by a `perf` test either.

`tests/audit/test_audit_32.py` adds a plain test that kills each non-equivalent survivor, and a strict xfail that applies the mutant.

Severity counts: **0 critical, 0 high, 2 medium, 6 low.** The main problems:
- **A worker that hangs at boot is still reported as "killed past the timeout" under a one-line slip (#1, medium).** The `_BOOT_S` branch of `Worker.ready` is uncovered (`solver_workers.py` 109-110), and removing its `fault = FAILED_TO_START` passes the suite. That is AUDIT-27 #1's misreport, for the hang case.
- **Nothing pins the forkserver preload (#2, medium).** With `preload=()`, or without `set_forkserver_preload`, every test passes, and each worker boot costs 170–190 ms instead of 5 ms (35×). SG32 removed the W33 test, the only test near the preload list.
- **SG32's two judged changes hold.** The AUDIT-26 #1 harness change narrows the repro to the case the finding names. The new harness still fails the pre-fix code (3.1 and 2.9 hard checks). The start-together case it dropped is unchanged by SG32 (3.0 hard checks before and after) and is only documented (#8). The W33 test was deleted along with `_main_imports`. Its replacement tests are stronger: they kill all four main-module mutants (N13, N14, N15, N21).

## How this was measured

- **Coverage:** I ran `uv run pytest --cov=autolab --cov-branch --cov-report=term-missing --cov-report=json tests` on the base. It gave **1409 passed, 13 skipped, 70 xfailed** in 4 min 22 s, all green, at a load average of 3–5 (mutant runs in parallel). The AUDIT-10 #4 XPASS from round 7 (AUDIT-28 #8) did not recur. Package figures are (covered lines + covered branches) / (lines + branches), from the JSON report.
- **Worker processes:** as in AUDIT-28, pytest-cov cannot see code that runs only in a worker (`_die_with_parent`'s body, `_serve` in the child), because workers end by `SIGKILL`. Child-only code was measured by mutants instead. The mutated copy is first on `PYTHONPATH`, and the forkserver and its workers inherit it, so S08 (in `_serve`) ran in the workers.
- **Mutations:**
  - A scratchpad harness (`a32/harness.py`, not committed) copies `src/`, `tests/`, `scripts/` and `pyproject.toml` to a fresh directory per mutant. It applies one exact-match replacement, asserted to occur once, and runs pytest there with `PYTHONPATH` set to the copy. Before each run it checks that `autolab` is imported from the copy. The worktree was never edited, and every copy was deleted after its run.
  - Each mutant ran `pytest -x -m "not perf" tests/unit/verification tests/audit`, about 910 tests. `test_audit_32.py` was left out.
  - The 9 survivors were then run against `-m perf` on the same paths (46 tests): all 9 survived.
  - Finally they were run against `test_audit_32.py` alone in fresh copies, and each one was killed by its plain test.
  - Two streams ran in parallel on 4 CPUs.
- **Earlier repros:** `src/autolab/verification/{solver,solver_workers,formalizer,vericot,_forkserver_init}.py` and `state/models.py` were taken from `ebfbbb1` (origin/claude/audit-round-7) into a fresh copy. HEAD's tests were run there with `--runxfail`, twice. For the AUDIT-26 #1 harness question I also ran the `ebfbbb1` `scripts/audit_load_7.py` against HEAD's code, twice.
- **Preload cost (#2):** a scratchpad script started a `Workers(_solve)` pool with the production preload and with none, in separate interpreters (the forkserver is per process). It timed 6 boots each, twice: median 4.9 and 5.0 ms preloaded, against 170 and 188 ms without.

## Coverage

| Module | Stmts | Miss | Branch | BrPart | Cover | Missing |
|---|---:|---:|---:|---:|---:|---|
| agents/* | 407 | 1 | 82 | 1 | 99.6% | design 90 (AUDIT-20 L6, carried) |
| cli.py | 79 | 6 | 22 | 0 | 92% | 75, 84-86, 115-117 |
| monitoring/* | 150 | 0 | 38 | 1 | 99.5% | tracer 77->76 |
| orchestrator/* | 288 | 0 | 64 | 0 | 100% | |
| state/checkpointer.py | 95 | 11 | 38 | 14 | **80%** | 55-73, 116->150, 162 (AUDIT-8 M6, carried) |
| state/ (other 3) | 298 | 0 | 36 | 0 | 100% | |
| tools/* | 122 | 5 | 26 | 4 | 94% | simulator 89; literature 81, 107, 124, 130 |
| verification/formalizer.py | 241 | 0 | 98 | 1 | 99% | 419->427 |
| verification/solver.py | 522 | 0 | 156 | 1 | 99% | 858->860 |
| verification/solver_workers.py | 171 | 8 | 34 | 1 | **94%** | 44-48 (child only), **109-110** (#1), 262-263 (atexit) |
| verification/llm.py | 69 | 1 | 16 | 2 | 96% | 189, 219->218 |
| verification/vericot.py | 130 | 1 | 52 | 1 | 99% | 247 |
| verification/ (other 4) | 71 | 0 | 18 | 0 | 100% | |
| **TOTAL** | 2674 | 33 | 680 | 26 | **98.0%** | |

| Package | Lines | Branches | Cover (line + branch) | With `test_audit_32.py` | Target |
|---|---:|---:|---:|---:|---|
| verification/ | 99.2% | 97.3% | **98.7%** | 98.9% | ≥90% ✔ |
| agents/ | 99.8% | 98.8% | 99.6% | 99.6% | ≥70% ✔ |
| orchestrator/ | 100% | 100% | 100% | 100% | ≥70% ✔ |
| monitoring/ | 100% | 97.4% | 99.5% | 99.5% | ≥70% ✔ |
| state/ | 97.2% | 78.4% | 94.2% | 94.2% | ≥70% ✔ |
| tools/ | 95.9% | 84.6% | 93.9% | 93.9% | ≥70% ✔ |
| cli.py | 92.4% | 90.9% | 92.1% | 92.1% | ≥70% ✔ |

- SG31–SG33 added 32 statements and 10 branches to `verification/`. All of them are covered except `Worker.ready`'s `_BOOT_S` branch (109-110, #1).
- `solver.py` is now fully covered by line. The round-7 gaps (231-232 and 238-241, AUDIT-28 #1 and #4) are closed by SG31's unit tests.
- `_forkserver_init.py` (0% in round 7) is gone.

## Mutation table

ID prefixes:
- S: SG31 (`solver_workers.Worker` protocol, `fault`, `_lost`, `TIMEOUT_MAX_S`).
- F: SG33 (`formalizer.py`: `MAX_NESTING`, the `MAX_PROBE_CHARS` charge, order in `Vocabulary.merged`).
- N: SG32 (newcomer ranking, `SolverExecutor._hard`, `_without_main`, preload, `Verifier()` start-up).

In the last column, "→" names the `test_audit_32.py` test that now kills a survivor.

| ID | Mutation | Result | Killed by |
|---|---|---|---|
| S01 | reply's seq not compared (`case (_, …)`) | killed | test_solver_workers::test_a_message_that_is_not_this_requests_reply_is_never_returned[earlier] |
| S02 | out-of-turn reply leaves `fault` empty | killed | same |
| S03 | `_send` never increments `_seq` | killed | same |
| **S04** | `_BOOT_S` passed: no `FAILED_TO_START` | **survived** | → test_a_worker_that_never_says_ready_failed_to_start (#1) |
| S05 | pipe closed at boot: no `FAILED_TO_START` | killed | test_solver_workers::test_a_worker_that_never_starts_failed_to_start |
| S06 | died mid-call: no `DIED` | killed | test_solver_executor::test_a_check_whose_worker_dies_is_never_valid |
| S07 | reply shape not checked (`case (seq, _)`) | killed | …_is_never_returned[untagged] |
| S08 | worker echoes seq 0 (child only) | killed | test_faithfulness (every check is out of turn) |
| S09 | `_seq` starts at 5 | survived (**equivalent**) | the worker echoes whatever it is sent |
| S10 | a lost worker always reported `_KILLED` | killed | test_a_check_whose_worker_dies_is_never_valid |
| S11 | `fault` checked before `interrupt.requested()` | killed | test_solver_executor::test_cancelling_a_caller_kills_its_worker |
| **S12** | `_lost` drops the exit-code branch | **survived** | → test_a_worker_that_exits_is_reported_with_its_exit_code (#5) |
| S13 | exit status appended for any fault | killed | test_solver_workers::test_a_stale_valid_reply_never_answers_a_later_check |
| S14 | no `TIMEOUT_MAX_S` clamp | killed | …_huge_timeout_is_clamped_and_decided_as_here[2250000.0] |
| **S15** | `TIMEOUT_MAX_S = 1e5` | **survived** | → test_the_timeout_cap_is_the_documented_one (#6) |
| S16 | `TIMEOUT_MAX_S = 2e6` | killed | same as S14 (2^31 ms bound) |
| S17 | budget from unclamped `self.timeout_seconds` | killed | same |
| S18 | `worker.ready()` back outside the `try` | killed | test_solver_workers::test_an_error_while_a_worker_boots_discards_it |
| S19 | reply arrived but cancel requested: released | killed (**audit test only**) | test_audit_28::test_a_worker_killed_after_its_reply_is_replaced |
| S20 | `_lost` details always "worker died" | killed | test_a_stale_valid_reply_never_answers_a_later_check |
| S21 | `call` skips `ready()` | killed | test_solver_workers::test_a_worker_answers_calls_and_is_reused |
| **F01** | `MAX_NESTING = 16` | **survived** (`perf` too) | → test_the_nesting_cap_is_the_documented_one (#6) |
| F02 | `MAX_NESTING = 64` | killed (**incidentally**) | …_within_the_probe_budget_is_accepted: the 7-deep accept case goes over the probe budget, not over a nesting test |
| F03 | depth cap at `> MAX_NESTING` | killed | …_within_the_probe_budget_is_accepted |
| F04 | depth cap at `> MAX_NESTING + 2` | killed | …_nested_over_the_cap_is_refused_before_any_parse |
| F05 | `MAX_PROBE_CHARS` doubled | killed | …_over_the_probe_budget_is_refused_before_any_parse |
| F06 | `MAX_PROBE_CHARS` halved | killed | test_a_wide_function_is_probed_with_a_bounded_number_of_parses[10] |
| F07 | `_PARSE_OVERHEAD = 0` | killed | …_refused_before_any_parse |
| F08 | charge ignores the carried sort declarations | killed | test_the_charge_counts_the_sort_declarations_each_probe_carries |
| F09 | 2 × len(decl) per parse | killed | …_refused_before_any_parse |
| F10 | no per-argument term | killed | same |
| **F11** | parametric sort's applied probe not charged | **survived** (`perf` too) | → test_the_charge_covers_every_parse_calls_fixed_cost (#3) |
| F12 | numeric variants counted as `arity` | killed | …_refused_before_any_parse |
| F13 | base parses 0 | killed | same |
| F14 | cost per line, not summed | killed | same |
| F15 | the first line is never refused | killed | same |
| F16 | builtin probe inline (pre-SG33 order) | killed | …_refused_before_any_parse (`parses.count == 0`) |
| F17 | depth checked after `_decl_name` | killed | …_nested_over_the_cap_is_refused_before_any_parse (unbalanced case) |
| F18 | a sort's probe carries its own declaration | killed | test_merge_accepts_names_z3_does_not_resolve_at_the_declared_arity |
| F19 | no conflict check | killed | test_formalizer_rejects_conflicting_redeclaration |
| F20 | last line never probed | killed | test_merge_rejects_z3_builtins_found_by_asking_z3[sin] |
| F21 | `)` does not lower the depth | killed | …_over_the_arity_cap_is_refused_before_any_parse |
| N01 | newcomers rank as hard (pre-SG32) | killed | test_solver_scheduling::test_a_new_runs_first_check_is_served_before_hard_runs |
| N02 | `new = False` | killed | same |
| N03 | `new = True` | killed | same |
| N04 | newcomers lent places in `_wake` | killed | test_solver_executor::test_slow_checks_are_bounded_by_slow_workers |
| N05 | newcomers lent places in `acquire` | killed | test_a_short_caller_overtakes_hard_checks_above_the_slow_cap |
| N06 | `_hard.add` dropped | killed | test_a_new_runs_first_check_is_served_before_hard_runs |
| N07 | `_hard.discard` dropped | survived (**equivalent**) | a task in both sets is short, and `new` needs `not short` |
| N08 | `new` ignores `short` | survived (**equivalent**) | `_rank` reads `short or new`; `_grant` reads `short` only |
| **N09** | newcomers rank between short and hard | **survived** | → test_a_newcomer_and_a_short_caller_are_served_in_arrival_order (#4) |
| **N10** | newcomers rank above short (and with aged) | **survived** | → same (#4) |
| N11 | `Verifier()` does not start workers | killed | test_a_verifier_starts_the_default_workers_when_built |
| N12 | `Verifier()` always starts the default pool | killed | test_verifier_runs_every_check_on_the_injected_executor |
| N13 | every process prepared without its main | killed | test_a_worker_is_prepared_without_the_main_module[path] |
| N14 | only the path key dropped | killed | …[dash-m] |
| N15 | wrapper not installed | killed | …[path] |
| **N16** | shared pool `preload=()` | **survived** (`perf` too) | → test_the_shared_pool_boots_workers_from_a_preloaded_forkserver (#2) |
| N17 | `"__main__"` back in the preload | survived (**equivalent**) | the 3.11 forkserver ignores it (it reads only `main_path`, which `get_preparation_data` never emits) |
| **N18** | `set_forkserver_preload` never called | **survived** (`perf` too) | → same (#2) |
| N19 | prestart only `workers` | killed | test_a_verifier_starts_the_default_workers_when_built |
| N20 | hard at `>` | killed | test_a_new_runs_first_check_is_served_before_hard_runs |
| N21 | worker process renamed (wrapper misses it) | killed (**audit test only**) | test_audit_25::test_an_unguarded_script_is_not_rerun_in_the_workers |

**Totals:** 63 mutations.
- The existing suite killed 50 (S 18, F 19, N 13).
- 4 are equivalent: S09, N07, N08 and N17.
- 9 non-equivalent survived: S04, S12, S15, F01, F11, N09, N10, N16 and N18. No `perf` test kills any of them.
- Each survivor is killed by a plain test in `test_audit_32.py`, checked in fresh mutant copies, and a strict xfail applies the mutant and runs that test.

F11's first form (the line deleted) left an empty `if` body and failed collection. It was re-run as `parses += 0`, and only that form is counted.

## Findings

### Critical

None.

### High

None. The SG31 code that guards soundness is fully covered by line and pinned by mutants. A stale reply (S01, S03, S07, S20), a release in place of a discard (S19, also AUDIT-28 #1's W06, which now gives UNGROUNDED) and a cancel (S11) are all killed.

### Medium

**#1. A worker hung at boot is reported as killed past the timeout under a one-line slip (S04 survived; `solver_workers.py` 109-110 uncovered; repro: `test_a_worker_that_never_says_ready_failed_to_start_under_s04`, strict xfail "AUDIT-32 #1").**
- `Worker.ready` returns False on two paths. `test_a_worker_that_never_starts_failed_to_start` covers the closed pipe (EOF). Nothing covers `_BOOT_S` passing with the pipe open: a worker stuck importing, or a forkserver stalled under memory pressure.
- With that branch's `self.fault = FAILED_TO_START` removed, the whole suite passes, `perf` included. `check_entailment` then returns `_KILLED`: reason `timeout`, "worker killed past the timeout". That is the misreport AUDIT-27 #1 named, and SG31's amendment says it fixed it for "a worker that … fails to start".
- *Added:* `test_a_worker_that_never_says_ready_failed_to_start`.
  - A `Worker` whose pipe stays open and silent, with `_BOOT_S` at 50 ms. Nothing ever arrives, so this is not a timing assertion.
  - It goes through `check_entailment` and must give `UNGROUNDED`/`worker_died`/"worker failed to start", with the worker discarded.

**#2. Nothing pins the forkserver preload (N16, N18 survived, `perf` too; repro: `test_the_shared_pool_boots_workers_from_a_preloaded_forkserver_under[n16|n18]`, strict xfails "AUDIT-32 #2", `perf`).**
- `_workers()` builds the shared pool with `preload=(solver_workers, solver)`, and `Workers._context` passes it to `set_forkserver_preload`.
- Both lines can go and every test passes. With no preload each boot imports z3 and the solver itself: a median of 170–190 ms against 5 ms here (35×). That cost is paid at start-up and after every kill or replacement, which SG29–SG32 worked to keep off the hot path.
- SG32 deleted the W33 test with `_main_imports`. That was right, since the function is gone (see "SG32's judged changes"). But it was the only test that touched what the forkserver preloads, and nothing replaced that part.
- *Added (`perf`):* `test_the_shared_pool_boots_workers_from_a_preloaded_forkserver`.
  - In fresh interpreters, the production pool's median boot (5 boots) must be over 4× faster than the same pool with `set_forkserver_preload` disabled.
  - It is calibrated, with no absolute seconds. Today the ratio is about 35×; under N16 and N18 it is 1.0–1.1×.

### Low

Each low finding with a mutant has a plain test that passes today, and a strict xfail `AUDIT-32 #n` that applies the mutant.
- **#3 (F11). The charge test bounds characters, not parse calls.**
  - `_PARSE_OVERHEAD` is documented as each parse's fixed cost. But `test_the_charge_bounds_the_characters_the_probe_parses` compares the charge with parsed characters only, and `3 × len(decl)` per parse already covers those. So a charge that omits a parametric sort's applied probe (one parse in three) passes.
  - The test's oracle `_charge` also re-runs `_probe_chars` itself, so `…_within_the_probe_budget_is_accepted`'s `_charge(decls) <= _BUDGET` moves with any mutant of `_probe_chars`.
  - *Added:* `test_the_charge_covers_every_parse_calls_fixed_cost`. For five declarations, the charge must be at least calls × `_PARSE_OVERHEAD` + characters, counted at `Z3_parse_smtlib2_string`. Under F11 it fails for `(declare-sort Box 2)`.
- **#4 (N09, N10). Newcomer against short caller is unpinned.** The ADR says a newcomer "ranks with short callers". Ranking it above them (N10) or between short and hard callers (N09) passes everything. *Added:* `test_a_newcomer_and_a_short_caller_are_served_in_arrival_order`, run on `_Slots(1)` in both arrival orders. Under N10 the newcomer always wins, and under N09 the short caller does. N10 does not break SG30's aging bound: a tie at rank 0 falls back to arrival order, and an aged caller arrived first.
- **#5 (S12). `_lost`'s exit-code branch is untested.** A worker that exits on its own (a native `abort`/`_exit`) would read "signal -3". *Added:* `test_a_worker_that_exits_is_reported_with_its_exit_code`, with a real worker whose target is `os._exit(3)`. It must read "worker died (exit code 3)".
- **#6 (S15, F01). Documented constants are read, not pinned.**
  - Every SG31/SG33 test reads `TIMEOUT_MAX_S` and `MAX_NESTING` from the module. A 10^5 s cap (a configured 5 days silently becomes 28 h) passes, and so does a nesting cap of 16 (4× the quadratic parse cost the cap bounds).
  - The AUDIT-26 #2 nested-sort repro refuses depths 400 and 800, so it passes any cap below 400.
  - F02 (cap 64) is killed only incidentally, by the probe budget.
  - *Added:* the ADR's values hard-coded. 5e5/1e6/5e6 s clamp to 5e5/1e6/1e6, and depths 6–8 are accepted while 9–11 are refused.
- **#7. The AUDIT-28 #1 repros can no longer run against pre-SG31 code.**
  - `test_a_check_cut_short_never_answers_the_next_one` and its `_under_w06` twin now call `Worker._send`. Against `ebfbbb1`'s files they fail with `AttributeError` (2 of 2), not on their assertions, as AUDIT-28 L9 found for AUDIT-14 #1.
  - They still catch W06, and SG31's own `test_a_stale_valid_reply_never_answers_a_later_check` pins the sequence check. But they cannot confirm a file-level revert. No test was added.
  - *Suggested:* send through `worker.conn.send((worker._seq + 1, args))` behind a `hasattr` check, or accept this as the cost of the protocol change.
- **#8. The AUDIT-26 #1 repro was narrowed (see "SG32's judged changes").** The start-together case is unchanged by SG32 and has no test. No test was added: no ranking can fix that case without knowing hardness in advance, and ADR 0009 documents it.

### Notes (no finding)

- **Carried:** coverage targets are not enforced by `make test` or CI (AUDIT-8 M5). `state/checkpointer.py` is still at 80% (AUDIT-8 M6). The `perf` tests are still in the default run (AUDIT-8 H1, AUDIT-20 #7).
- AUDIT-28 #8 (the AUDIT-10 #4 XPASS under `--cov`) did not recur in this audit's coverage run. AUDIT-28 #3 was addressed: SG32 replaced `test_judges_overlap_but_formalization_stays_ordered` with a calibrated twin.
- Two kills come only from audit tests: S19 (by `test_audit_28`) and N21 (by `test_audit_25`'s subprocess script). The unit suite alone would not catch a renamed worker process escaping `_without_main`.
- `test_audit_27::test_no_worker_is_left_running_after_shutting_down_the_default_executor` is still a strict xfail (AUDIT-27 #3, open). SG31–SG33 did not touch it.

## SG32's judged changes

**AUDIT-26 #1 harness (`newcomer_first_check_s`): narrowed, not weakened against the revert.**
- *Before:* 12 hogs were started and the newcomer was sent `timeout/4` later. At that point 4 hogs were in their first check and 8 were queued, all of them newcomers too.
- *Now:* the newcomer is sent once every hog has completed one hard check, so the 8 queued callers are known hard.
- *Measured:*

  | Harness | Code | Result |
  |---|---|---|
  | new | pre-fix (`ebfbbb1`) | **fails**, 2 of 2: 3.1 and 2.9 hard checks against 1.5 |
  | new | HEAD | passes, 3 of 3 |
  | old | HEAD | **fails**, 2 of 2: 3.0 hard checks |

- *Judgement:*
  - The finding's own text is about a newcomer "queued FIFO behind every hard caller already waiting". The old harness's queued callers were not yet hard, so the new setup is the case named, and it still detects the pre-fix code.
  - What it drops is the start-together case. A fair queue cannot reorder callers that are equally unknown, and SG32 leaves that case exactly as it was (3.0 hard checks). ADR 0009's SG32 amendment states this under "What this does not bound". It is no longer kept visible by any test (#8).

**AUDIT-28 W33 test and xfail (deleted): not weakened.**
- W33 pinned `_main_imports`, which preloaded a `-m` main in the forkserver. SG32 removed the function and stopped workers from re-running the main module at all.
- The replacement tests are strictly stronger:
  - `test_a_worker_is_prepared_without_the_main_module[path|dash-m]` (unit, no subprocess);
  - `test_an_unguarded_dash_m_main_runs_once_and_gets_valid` (a real `-m` main, one body run);
  - AUDIT-25 #1 and AUDIT-26 #4/#5 (now plain tests).
- They kill every mutant of the new mechanism: N13 (all processes stripped), N14 (only the path key), N15 (wrapper not installed) and N21 (worker name changed).
- The one gap is adjacent: the preload list itself (#2).

## Earlier repros flipped by SG31–SG33, against the pre-fix code

Run with HEAD's tests and the `ebfbbb1` production files, using `--runxfail`, twice each.

| Repro | Result on pre-fix code | Verdict |
|---|---|---|
| AUDIT-25 #1 (unguarded script) | fail 2/2 ("UNGROUNDED \| worker killed past the timeout") | not weakened |
| AUDIT-25 #2 (crash not a timeout) | fail 2/2 ("past the timeout" in details) | not weakened |
| AUDIT-25 #3 (error while booting leaks) | fail 2/2 (worker idle and alive) | not weakened |
| AUDIT-26 #1 (`perf`, new harness) | fail 2/2 (3.1, 2.9 hard checks) | narrowed (#8), not weakened |
| AUDIT-26 #2 ×2 (`perf`) | fail 2/2 each (1988 and 1806 ms = 249 and 261 parses; depth ×2 costs 4.5× and 4.6×) | not weakened |
| AUDIT-26 #3 (cold start) | fail 2/2 (`start_before_check: False`) | not weakened |
| AUDIT-26 #4 (main re-imported per boot) | fail 2/2 (4 > 2) | not weakened |
| AUDIT-26 #5 (unguarded main a timeout) | fail 2/2 | not weakened |
| AUDIT-27 #1 ×2 | fail 2/2 each | not weakened |
| AUDIT-27 #2 (long timeout) | fail 2/2 (`OverflowError: timeout is too large`) | not weakened |
| AUDIT-28 #1 (plain and `_under_w06`) | **`AttributeError: … '_send'`** 2/2 | cannot confirm a revert (#7) |

## Tests that bound wall time

| Test | Bound | Assessment |
|---|---|---|
| test_audit_26 #1 (`perf`) | ≤ 1.5 × one hard check, measured in the same run | calibrated; passed 3/3 alone |
| test_audit_26 #2 ×2 (`perf`) | 50 parses; depth ×2 ≤ 2.5× | calibrated |
| test_solver_executor::test_judges_overlap_… (`perf`) | baseline with no latency | calibrated (AUDIT-28 #3 adopted) |
| test_solver_executor::test_solver_ms_excludes_worker_start_up | fake clock | deterministic (AUDIT-28 #5 adopted) |
| test_solver_workers::…_killed_mid_call_died_… | 50 ms timer inside a 10 s budget | generous; fine |
| test_audit_32::…_preloaded_forkserver (`perf`, new) | ratio > 4 against the same pool without preload | calibrated; about 35× today |

## Added tests (`tests/audit/test_audit_32.py`)

| Test | Kills / reproduces |
|---|---|
| test_a_worker_that_never_says_ready_failed_to_start (+ xfail) | S04, #1 |
| test_the_shared_pool_boots_workers_from_a_preloaded_forkserver (`perf`, + 2 xfails) | N16, N18, #2 |
| test_the_charge_covers_every_parse_calls_fixed_cost (+ xfail) | F11, #3 |
| test_a_newcomer_and_a_short_caller_are_served_in_arrival_order (+ 2 xfails) | N09, N10, #4 |
| test_a_worker_that_exits_is_reported_with_its_exit_code (+ xfail) | S12, #5 |
| test_the_timeout_cap_is_the_documented_one (+ xfail) | S15, #6 |
| test_the_nesting_cap_is_the_documented_one (+ xfail) | F01, #6 |

`_mutant` (from `test_audit_24.py`) recompiles one function from its source with one exact-match edit and binds it to the module's own globals. `monkeypatch` restores the original. No source file is edited. For #2 the mutant is applied inside the child interpreter it measures. Every xfail fails on its assertion under `--runxfail`, not on setup (9 of 9).

Verification:
- `make lint typecheck`: ruff clean, 154 files already formatted, mypy strict clean on 141 source files.
- `uv run pytest tests/audit/test_audit_32.py -q`: 7 passed, 9 xfailed.
- `--runxfail`: 9 failed, 7 passed.
- Full coverage run with this file (the audit's command): **1416 passed, 13 skipped, 79 xfailed** in 4 min 1 s, green; 98.1% overall, `verification/` 98.9%, and `solver_workers.py` 109-110 now covered (95%, left with child-only 44-48 and the atexit hook).
