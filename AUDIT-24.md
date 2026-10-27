# AUDIT-24: Test coverage and test quality (round 6)

Branch `claude/audit-24`, based on `origin/claude/sg27-real-timeouts` (`492980e`, SG24–SG27 merged). This is an independent audit, and no production code or existing test was changed.

**Bottom line:** coverage is still high: 98.1% line and branch overall, 99.4% on `verification/`, 99.6% on `agents/` and 100% on `orchestrator/`. I ran 74 one-line mutations by hand over the code SG24–SG27 changed:
- The existing suite killed 58 of them. One of those (L03) was killed only by `perf` wall-clock tests.
- 16 survived. Three are equivalent (F13, S16, S19).
- `tests/audit/test_audit_24.py` adds a plain test that kills each of the 13 non-equivalent survivors and the timing-only kill. For each one it also adds a strict xfail that applies the mutant (by `monkeypatch`) and runs that test. L10 is killed by the #4 test, alongside L01.

Severity counts: **0 critical, 1 high, 5 medium, 8 low.** The main problems:
- **The AUDIT-14 #1 repros no longer catch the bug they name (#1).** SG27 moved their 1600-premise pool onto the incremental loading path. There, SG19's assumption-based entailment check honours its timeout. With that check restored on today's solver, both repros and AUDIT-20 #3's pass, 3 of 3. Only the two tests that count `check` arguments still kill it.
- **SG27's loading has three unpinned details (#4, #5, #6).** Loading checks can run with no Z3 limit at all (L01, which also survives the `perf` repros). The limit bound is asserted against the constant it bounds (L02). The pool size that switches loading on is pinned only by `perf` tests (L03).
- **SG26's hardness and lending have two gaps (#2, #3).** Whether a run is hard is counted in event-loop wake-ups, so a hard run on a busy loop keeps the short-caller priority (a real bug, deterministic repro). And a check in a lent place may itself lend (S06).
- The recalibrated AUDIT-18 #3 ×3 and #5 repros and the AUDIT-6 #7 / AUDIT-20 #1 repros fail against the pre-fix code (2 of 2 each) and are deterministic or scaled to the machine. They are not weakened. The restored AUDIT-13 #2 helper makes those repros fail on their assertions again.

## How this was measured

- **Coverage:** `uv run pytest --cov=autolab --cov-branch --cov-report=term-missing --cov-report=json tests` on the base: 1212 passed, 12 skipped, 35 xfailed in 4 min 45 s. Package figures are (covered lines + covered branches) / (lines + branches), from the JSON report.
- **Mutations:** a scratchpad harness (not committed) copies `src/`, `tests/`, `scripts/` and `pyproject.toml` to a fresh directory per mutant. It applies one exact-match replacement, asserted to occur once, and runs pytest there with `PYTHONPATH` set to the copy. Before each run it checks that `autolab` is imported from the copy. The worktree was never edited.
  - Each mutant ran `pytest -x -m "not perf"` over the tests for its area. That is `tests/unit/verification` plus the formalizer or solver audit files, or `tests/unit/agents`, `tests/unit/orchestrator`, `tests/integration` and the Design audit files (AUDIT-5, 12, 13, 15, 16, 17, 19, 20).
  - Survivors in loading (L01, L03) were re-run against the AUDIT-18 #3 `perf` repros, twice each.
  - Two mutant streams ran in parallel on 4 CPUs, at a load average of 2–4.
- **Earlier repros:** each recalibrated or restored repro was run, twice, against the pre-fix production file:
  - `solver.py` from `b4af84f` (pre-SG27), `603e452` (pre-SG26) and `176d019` (pre-SG22);
  - `formalizer.py` from `855b108` (pre-SG20) and `af954a9` (pre-SG24).
  - SG19's assumption check was also restored as a one-line mutant on today's solver ("ASSUME"), 3 times.

## Coverage

| Module | Stmts | Miss | Branch | BrPart | Cover | Missing |
|---|---:|---:|---:|---:|---:|---|
| agents/common.py | 130 | 0 | 36 | 0 | 100% | |
| agents/design.py | 100 | 1 | 20 | 1 | 98.3% | 90 (`design is None`) |
| agents/* (other 5) | 177 | 0 | 26 | 0 | 100% | |
| cli.py | 79 | 6 | 22 | 0 | 92% | 75, 84-86, 115-117 |
| monitoring/* | 150 | 0 | 38 | 1 | 99.5% | tracer 77->76 |
| orchestrator/* (routing.py 28/6) | 288 | 0 | 64 | 0 | 100% | |
| state/checkpointer.py | 95 | 11 | 38 | 14 | **80%** | 55-73, 116->150, 162 |
| state/models.py, schema.py, store.py | 297 | 0 | 36 | 0 | 100% | |
| tools/* | 122 | 5 | 26 | 4 | 94% | simulator 89; literature 81, 107, 124, 130 |
| verification/formalizer.py | 191 | 0 | 76 | 1 | 99.6% | 327->335 |
| verification/solver.py | 393 | 0 | 120 | 1 | 99.8% | 648->exit |
| verification/llm.py | 69 | 1 | 16 | 2 | 96.5% | 189, 219->218 |
| verification/vericot.py | 128 | 1 | 50 | 1 | 98.9% | 244 |
| verification/ (other 3) | 71 | 0 | 18 | 0 | 100% | |
| **TOTAL** | 2322 | 25 | 586 | 25 | **98.1%** | |

| Package | Lines | Branches | Cover (line + branch) | Target |
|---|---:|---:|---:|---|
| verification/ | 99.8% | 98.2% | **99.4%** | ≥90% ✔ |
| agents/ | 99.8% | 98.8% | 99.6% | ≥70% ✔ |
| orchestrator/ | 100% | 100% | 100% | ≥70% ✔ |
| monitoring/ | 100% | 97.4% | 99.5% | ≥70% ✔ |
| state/ | 97.2% | 78.4% | 94.2% | ≥70% ✔ |
| tools/ | 95.9% | 84.6% | 93.9% | ≥70% ✔ |
| cli.py | 92.4% | 90.9% | 92.1% | ≥70% ✔ |

- Every line SG24–SG27 added is covered. The only uncovered SG25 line is `design.py` 90, the `design is None` early return (AUDIT-20 L6, carried).
- As in AUDIT-16 and AUDIT-20, coverage hides the problems: all 16 survivors are on fully covered lines.

## Mutation table

ID prefixes: F `formalizer.py` (SG24), A `agents/common.py` (SG25), D `agents/design.py` (SG25), R `orchestrator/routing.py` (SG25), S `solver.py` scheduling (SG26), L `solver.py` loading (SG27). In the last column, "→" names the `test_audit_24.py` test that now kills a survivor.

| ID | Mutation | Result | Killed by |
|---|---|---|---|
| F01 | no flipped variants (`for flip in (None,)`) | killed | test_formalizer::test_numeric_variants_are_every_combination_up_to_three_arguments |
| F02 | Int base only | killed | same |
| F03 | no de-duplication of variants | killed | same |
| F04 | no uniform base (flip from the declared sorts) | killed | same |
| F05 | cache key holds every declared sort | killed | test_formalizer::test_the_probe_cache_ignores_sorts_the_declaration_does_not_mention |
| F06 | no declared sorts in the probe | killed | test_formalizer::test_merge_rejects_builtins_applied_as_declared |
| F07 | no `#b`/`#x` literal tokens | killed | test_formalizer::test_tokens_split_where_z3_splits |
| F08 | no `#x` literal tokens | killed | same |
| F09 | numerals without a fraction | killed | same |
| F10 | `:` does not start a keyword token | killed | same |
| F11 | `#` is a symbol character | killed | same |
| F12 | `'` is a symbol character | killed | same |
| F13 | applied probe only at the declared sorts (no variants) | survived (**equivalent**) | as AUDIT-20 F15: Z3 coerces Int/Real itself, and the hypothesis test (`..._agrees_with_every_combination`) cannot tell them apart |
| **F14** | earlier-sort filter reads the whole batch, not the line | **survived** | → test_a_batch_with_a_new_sort_reuses_the_probe (L7) |
| F15 | no numeral token | killed | test_tokens_split_where_z3_splits |
| **A01** | confusables: only Cyrillic shha folded | **survived** | → test_look_alike_letters_invisible_characters_and_non_ages_are_flagged (L1) |
| **A02** | format characters: only U+00AD, U+200B dropped | **survived** | same (L2) |
| A03 | no NFKC | killed | test_audit_17::test_protocol_figures_see_through_unicode_spellings[④] |
| A04 | no age exemption | killed | test_design::…_allows_non_cost_non_duration_numbers[8 weeks old] |
| A05 | no `per cent` exemption | killed | same table [2 per cent] |
| A06 | time word needs no lead-in | killed | same table [day 14] |
| A07 | joined ISO code + digits not a price | killed | test_design::…_flags_currency_and_durations[USD500] |
| A08 | any ISO code + digits is a price | killed | test_design table [TOP10] |
| A09 | `m` is a time unit again | killed | test_design table [2 m] |
| A10 | lead-in always seen | killed | test_design table [day 14] |
| **A11** | age exemption: `old` anywhere in the window | **survived** | → same as A01 (L3) |
| D01 | ratio 4.9 | killed | test_design_estimate::test_figures_within_the_ratio_… |
| D02 | `>=` ratio | killed | same |
| D03 | cost floor 0.3 | killed | test_design_estimate::test_figures_below_the_floors_compare_as_the_floor |
| **D04** | duration floor 3 | **survived** | → test_figures_under_the_duration_floor_compare_as_ten_minutes (L4) |
| D05 | duration never "raised" | killed | test_design_estimate::test_the_larger_figure_drives_the_trigger |
| **D06** | `_at_least` takes the estimate's cost | **survived** | → test_a_blocked_trace_names_the_policy_on_the_larger_figures (L5) |
| D07 | policy run even when not raised | killed | test_audit_5::test_approving_a_verification_escalation_still_applies_the_cost_trigger |
| D08 | no skip when already paused on policy | killed | test_design_estimate::…_pauses_once |
| D09 | no `feedback` in the payload | killed | test_design::…_escalated_not_admitted |
| D10 | duration never compared | killed | test_design_estimate::…_names_the_gap[duration-under] |
| D11 | prompt without success criteria | killed | test_design_estimate::test_the_estimate_prompt_reads_… |
| D12 | no floors | killed | test_design_estimate::…_compare_as_the_floor |
| D13 | blocked trace reason not extended | killed | test_design_estimate::test_a_blocked_trace_names_… |
| R01–R05 | `PASS` retried without feedback; summary always added; one reason only; policy requests retried; every non-ESCALATE retried | killed | test_routing (SG25 block) |
| **S01** | `_Pool._claim` never hands a call to an idle thread | **survived** | → test_an_idle_thread_takes_the_next_call[2] (L6) |
| **S02** | a thread at the limit ends after its call | **survived** | → test_an_idle_thread_takes_the_next_call[1] (L6) |
| S03 | waiters woken FIFO | killed | test_solver_scheduling::test_a_free_slot_goes_to_a_short_caller_first |
| S04 | `lent <= stuck` | killed | same |
| S05 | lend to any caller | killed | test_solver_executor::test_slow_checks_are_bounded_by_slow_workers |
| **S06** | a lent check may stick (and lend) | **survived** | → test_a_lent_check_never_lends_a_place (#3) |
| S07 | `slices > _HARD_SLICES` | killed | test_solver_scheduling::test_a_caller_whose_check_ran_long_is_not_lent_a_place |
| S08 | `_HARD_SLICES = 3` | killed | test_audit_20::test_the_audit_6_7_setup_admits_… (2 of 2) |
| S09 | no thread for a lent place | killed | test_solver_scheduling::…_overtakes_hard_checks_above_the_slow_cap |
| S10 | lent thread never returned | killed | same |
| S11 | a stuck check that goes slow stays stuck | killed | test_solver_scheduling::test_slow_checks_never_exceed_slow_workers |
| S12 | no thread for the next caller when a check goes slow | killed | test_solver_executor::test_slow_checks_are_bounded_by_slow_workers |
| S13 | slow thread never returned | killed | test_slow_checks_never_exceed_slow_workers |
| S14 | a cancelled queued call still runs | killed | test_solver_scheduling::test_a_cancelled_queued_call_never_runs |
| S15 | a grant to a cancelled waiter is lost | killed | test_solver_scheduling::test_a_caller_cancelled_after_its_grant_… |
| S16 | a call with no Z3 context may stick | survived (**equivalent** in production) | every `SolverExecutor` call in `src/` is a `check_entailment`, which attaches before it parses |
| S17 | a call with no Z3 context may go slow | killed | test_solver_executor::test_only_a_running_z3_check_gives_up_its_slot |
| S19 | `unstick` also wakes waiters | survived (**equivalent**) | an unstick never frees a place, so the wake grants nothing |
| S20 | `stick` does not wake waiters | killed | test_solver_scheduling::test_a_stuck_slot_lends_one_place_… |
| S21 | one thread over the limit | killed | test_solver_scheduling::test_a_raised_limit_starts_a_thread_… |
| S22 | `SOLVER_SLOW_WORKERS` one short | killed | test_solver_scheduling::test_slots_and_slow_checks_do_not_outnumber_the_cpus |
| **L01** | no limit before the first loading check (`or not limit` dropped) | **survived** (also the `perf` repros, 2 of 2) | → test_every_loading_check_runs_under_a_limit (#4) |
| **L02** | `_RESET_SHARE = 0.1` | **survived** | → test_a_loading_check_never_gets_more_than_the_documented_share (#5) |
| L03 | `_LOAD_ABOVE = 2000` | killed (**perf 2/2** only) | AUDIT-18 #3 ×3 → test_the_audit_18_3_pool_is_loaded_one_formula_at_a_time (#6) |
| L04 | a loading check's unsat ignored | killed | test_solver_loading::test_loading_stops_at_the_first_inconsistent_prefix |
| **L06** | the guard's clock starts before loading | **survived** | → test_a_pool_that_loaded_slowly_gets_its_entailment_check (L8) |
| L07 | deadline not polled while loading | killed | test_solver_loading::test_loading_stops_once_the_deadline_passes |
| L08 | interrupt not polled while loading | killed | test_solver_loading::test_loading_stops_when_interrupted |
| L09 | no final consistency check on a loaded pool | killed | test_solver_loading::test_a_long_pool_is_checked_between_formulas_then_twice_… |
| **L10** | no limit ever set while loading | **survived** | → test_every_loading_check_runs_under_a_limit (#4) |

**Totals:** 74 mutations (R01–R05 are 5). The existing suite killed 58, one of them (L03) only by `perf` tests. 16 survived. Three are equivalent (F13, S16, S19). The 13 non-equivalent survivors are F14, A01, A02, A11, D04, D06, S01, S02, S06, L01, L02, L06 and L10. Each is killed by a plain test in `test_audit_24.py`, and a strict xfail applies the mutant and runs that test.

A separate repro-check mutant, ASSUME, restores SG19's `solver.check(claim!)` on today's solver. The suite kills it, but the repros named for it do not (#1).

## Findings

### Critical

None.

### High

**#1. The AUDIT-14 #1 repros no longer catch SG19's assumption check (weakened by SG27; repro: `test_the_audit_14_1_timeout_repro_fails_with_the_assumption_check`, `perf`, strict xfail "AUDIT-24 #1").**
- *What changed:* the repros' pool is `lia_chain(1600)`, above `_LOAD_ABOVE` (100). Since SG27 it is loaded one formula at a time into an incremental solver. On that path the entailment check under an assumption honours its timeout and interrupts.
- *Measured:* with SG19's `solver.check(claim!)` restored on today's `solver.py` (the ASSUME mutant), `test_a_check_returns_within_its_timeout`, `test_cancelling_a_check_frees_its_worker_promptly` and AUDIT-20's `test_the_audit_14_1_timeout_repro_reaches_the_entailment_check` all pass, 3 of 3 runs.
  - The whole pre-SG22 `solver.py` still fails both repros (2 of 2), so they catch a file revert but not the change they name.
  - S01 is still killed, but only by `test_the_negated_claim_is_asserted_and_checked_without_assumptions` and AUDIT-20's `…_instead_of_assuming_it`. Both count `check` arguments on a 1-premise pool and test the implementation, not the behaviour.
- *Why it matters:* the unpolled phase is on the one-call path. If `_LOAD_ABOVE` were raised (L03) and the assumption check restored, no `-m "not perf"` test of behaviour would notice. The AUDIT-14 repros would not notice either.
- *Added:* the repro above (it expects the AUDIT-14 #1 timeout repro to fail under ASSUME; today "DID NOT RAISE"). Also `test_on_the_one_call_path_the_audit_14_1_repro_catches_the_assumption_check` (`perf`, passes): with `_LOAD_ABOVE` patched above the pool, the same repro catches ASSUME.
- *Suggested:* in `test_audit_14.py`, run both #1 repros with `_LOAD_ABOVE` patched above `_POOL` (the one-call path), and add a loaded-path variant if that path should be guarded too.

### Medium

**#2. Whether a run is hard is counted in event-loop wake-ups, not time (repro: `test_a_check_that_ran_long_while_the_loop_was_busy_counts_as_hard`, strict xfail "AUDIT-24 #2").**
- `SolverExecutor.run` counts `slices` as the `asyncio.wait(timeout=_SLICE_S)` calls that time out. If the caller's loop is busy (formalizing, judging, other callers) while its check runs, a check of 10 slices counts as 0 or 1. The run keeps the short-caller priority and the lent places SG26 reserves for easy runs (AUDIT-18 #4), so under load hard runs are not told apart.
- The repro blocks the loop for 12 slices while a 10-slice check runs, and the caller ends in `_short` (10 of 10 runs).
- The SG26 test `test_a_caller_whose_check_ran_long_is_not_lent_a_place` sleeps 5 slices on an idle loop. It passes on a quiet machine and fails, in the failing direction, under load.
- *Suggested:* compare the check's measured run time (`run` already returns it in ms) with `_HARD_SLICES * _SLICE_S`.

**#3. A check in a lent place may itself stick and lend (S06 survived; repro: `…_never_lends_a_place_under_s06`, strict xfail "AUDIT-24 #3").** With `elif not stuck and …`, each lent check that runs past a slice lends one more place. Short callers then run beyond `workers + stuck` checks at once (the CPU oversubscription of AUDIT-18 #5). No SG26 test lets a lent check run past its slice while another short caller waits. *Added:* `test_a_lent_check_never_lends_a_place` (after three slices of the lent check, `(lent, stuck) == (1, 1)`; under S06 `(1, 2)`).

**#4. A loading check can run with no Z3 limit (L01, L10 survived, L01 also against the AUDIT-18 #3 `perf` repros; repro: `…_under_a_limit_under_l01`, strict xfail "AUDIT-24 #4").**
- `_load` starts with `limit = 0`, so only `or not limit` sets a timeout before the first loading check. Without it no loading check has one. Only the deadline poll between checks then bounds the pool, and a single long simplex run would be unbounded.
- `test_a_loading_check_never_gets_much_more_than_the_time_left` passes regardless: it compares the last limit set, which stays 0, against the time left.
- *Added:* `test_every_loading_check_runs_under_a_limit`. It requires a timeout in (0, 30 s] before the first loading check.

**#5. The bound on a loading check's limit is derived from the constant it bounds (L02 survived; repro: `…_documented_share_under_l02`, strict xfail "AUDIT-24 #5").** The SG27 test asserts each limit is at most `time left / _RESET_SHARE + 1 ms`. So any share passes: at `_RESET_SHARE = 0.1` a loading check may be given 10× the time left. *Added:* the same fake-clock test with SG27's documented 0.9 hard-coded (88 violations under L02, none today).

**#6. Which pools are loaded one formula at a time is pinned only relative to `_LOAD_ABOVE` (L03 killed by `perf` tests only; repro: `…_one_formula_at_a_time_under_l03`, strict xfail "AUDIT-24 #6").** Every SG27 unit test builds `_LOAD_ABOVE + 20` premises. Raising the threshold past AUDIT-18 #3's 999-premise pool puts that pool back on the one-call path, where Z3's simplex ignores timeouts and cancels. Only the AUDIT-18 #3 `perf` repros see that (2 of 2), and they are the tests that flake under load (AUDIT-20 #7). *Added:* `test_the_audit_18_3_pool_is_loaded_one_formula_at_a_time`, which counts loading checks on a 1000-step chain (999).

### Low

Each low finding has a plain test that passes today and a strict xfail, `AUDIT-24 L<n>`, that applies the mutant.
- **L1 (A01).** Only one of the 49 look-alike letters SG25 folds is tested (Cyrillic shha, AUDIT-17 #4). Cyrillic `о`, `а`, `е` and Greek `ο` in `hours`, `days`, `weeks` are now pinned.
- **L2 (A02).** Of the format characters SG25 drops, only U+00AD and U+200B are tested. U+2060, U+FEFF, U+200D and U+2063 between amount and unit are now pinned.
- **L3 (A11).** The `8-week-old` exemption must look at the word right after the unit only. With `"old" in words[1:]`, "incubate 48 hours with old medium" passes the scan.
- **L4 (D04).** The duration floor is pinned only from above: 10 → 3 min passes. A 1 min design against a 9.5 min estimate must be admitted.
- **L5 (D06).** On a blocked trace, the policy check runs on the larger of each figure. With the estimate's own cost, a design cost over the limit ($15 against a $6 estimate) is missing from the reason the reviewer reads. `approve` re-runs the trigger on the design, so no pause is lost.
- **L6 (S01, S02).** `_Pool` hands a call to an idle thread instead of starting one, and keeps a thread at its limit idle instead of ending it. Every SG26 pool test counts threads only while calls are held. Without either rule, a new thread is started for a call (one per check under S02).
- **L7 (F14).** The probe's cache key must hold the sorts the declaration line mentions, not those its whole batch mentions. Only the cost of re-probing is at stake.
- **L8 (L06).** On the loaded path the `_SETUP_SHARE` guard measures only the final consistency check, not the loading before it. So a pool that spent 0.7 s of a 1 s budget loading still gets its entailment check, and the AUDIT-14 #1 timeout repro relies on that. Which time the guard measures is pinned by no deterministic test. The repro pins today's choice. If the guard should count loading time, flip it.

### Notes (no finding)

- **Carried:** coverage targets are not enforced by `make test` or CI (AUDIT-8 M5). `state/checkpointer.py` is still at 80% (AUDIT-8 M6). The `perf` tests are still in the default run (AUDIT-8 H1, AUDIT-20 #7).
- AUDIT-20's `test_an_easy_check_overtakes_hard_ones_on_the_default_slow_workers` is now skipped below 4 CPUs (SG26). On a 2-CPU CI runner, S24/S25-style changes to the default slow lane are caught only by the sizing test.
- F13 (above) leaves `_numeric_variants` with no behaviour a test can observe. Its parse bound is pinned (AUDIT-18 #1), but whether the variants are needed at all is not.

## Earlier repros after their xfail was removed or their setup changed

| Repro | Change in SG24–SG27 | Against the pre-fix code | Verdict |
|---|---|---|---|
| AUDIT-18 #1 | marker removed | (parse count; SG24 unit tests) | not weakened |
| AUDIT-18 #3 ×3 | timeout and cancel at 1/5 and 1/10 of the check's own run time | pre-SG27 `solver.py`: all three fail, 2 of 2; L03: all three fail, 2 of 2 | not weakened; they miss L01 (#4) |
| AUDIT-18 #5 | timeout 4× one check alone | pre-SG26: fails, 2 of 2 | not weakened |
| AUDIT-6 #7, AUDIT-20 #1 | `slow_workers=0`, waits on the pool queue | pre-SG26: both fail, 2 of 2 | not weakened (deterministic now) |
| AUDIT-14 #1 ×2, AUDIT-20 #3 | pigeonhole claim, timeout 3× time to the entailment check | pre-SG22 `solver.py`: both #1 repros fail, 2 of 2; **ASSUME on today's solver: all three pass, 3 of 3** | **weakened by SG27 (#1)** |
| AUDIT-13 #2 ×2, AUDIT-20 #2 | helper uses a shadow `Formalizer` | pre-SG20 `formalizer.py`: both fail; with the ban off they fail with `AssertionError` (AUDIT-20 #2 passes) | not weakened |
| AUDIT-17 #1–#5, AUDIT-19 #1, #2 | markers (SG25) | not re-run against pre-SG25 code; their assertions are on the behaviour SG25 added | — |

## Added tests (`tests/audit/test_audit_24.py`)

| Test | Kills / reproduces |
|---|---|
| test_the_audit_14_1_timeout_repro_fails_with_the_assumption_check (`perf`, strict xfail) | AUDIT-24 #1 |
| test_on_the_one_call_path_the_audit_14_1_repro_catches_the_assumption_check (`perf`) | ASSUME, the #1 fix |
| test_a_check_that_ran_long_while_the_loop_was_busy_counts_as_hard (strict xfail) | AUDIT-24 #2 |
| test_a_lent_check_never_lends_a_place (+ xfail under S06) | S06, #3 |
| test_every_loading_check_runs_under_a_limit (+ xfail under L01) | L01, L10, #4 |
| test_a_loading_check_never_gets_more_than_the_documented_share (+ xfail under L02) | L02, #5 |
| test_the_audit_18_3_pool_is_loaded_one_formula_at_a_time (+ xfail under L03) | L03, #6 |
| test_look_alike_letters_invisible_characters_and_non_ages_are_flagged (+ 3 xfails) | A01, A02, A11 (L1–L3) |
| test_figures_under_the_duration_floor_compare_as_ten_minutes (+ xfail) | D04 (L4) |
| test_a_blocked_trace_names_the_policy_on_the_larger_figures (+ xfail) | D06 (L5) |
| test_an_idle_thread_takes_the_next_call ×2 (+ 2 xfails) | S01, S02 (L6) |
| test_a_batch_with_a_new_sort_reuses_the_probe (+ xfail) | F14 (L7) |
| test_a_pool_that_loaded_slowly_gets_its_entailment_check (+ xfail) | L06 (L8) |

`_mutant` recompiles one function from its source with one exact-match edit and binds it to the module's own globals. `monkeypatch` restores the original. No source file is edited.

Verification:
- `make lint typecheck`: ruff clean, 142 files already formatted, mypy strict clean on 131 source files.
- `uv run pytest tests/audit/test_audit_24.py -q`: 12 passed, 15 xfailed.
- `--runxfail`, 10 runs: every one gives 15 failed, 12 passed.
- `uv run pytest`: **1224 passed, 12 skipped, 50 xfailed** in 4 min 6 s. The base gave 1212 passed and 35 xfailed, so this adds 12 passing tests and 15 strict xfails.
