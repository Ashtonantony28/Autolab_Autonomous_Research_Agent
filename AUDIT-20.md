# AUDIT-20: Test coverage and test quality (round 5)

Branch `claude/audit-20`, based on `origin/claude/sg22-solver-fairness` (`603e452`, SG20–SG23 merged). This is an independent audit, and no production code was changed.

**Bottom line:** coverage is still high: 97.8% line and branch overall, 99.1% on `verification/`, and 99.5–100% on `agents/` and `orchestrator/`. I ran 80 one-line mutations over the code that SG20–SG23 changed:
- The existing suite killed 65 of them. Five of those (S01, S05, S09, S24, S25) were killed only by `perf` wall-clock tests.
- 15 survived. Two are equivalent (F03, F15).
- `tests/audit/test_audit_20.py` kills all 13 non-equivalent survivors. It also kills, without wall-clock bounds, the 10 mutants that were caught only by timing. Each kill was confirmed by re-applying the mutant.

Severity counts: **0 critical, 5 high, 6 medium, 7 low.** The main problems:
- **Three earlier repros no longer test what they were written for.**
  - **AUDIT-6 #7 (#1):** SG23 made this repro deterministic, but SG22 made it timing-dependent again. It XPASSed (strict) in this audit's mutation pass under load.
  - **AUDIT-13 #2 (#2):** SG20's helper change means both repros fail on a `FakeLLMClient` KeyError, not on their assertions, when the annotation ban is gone.
  - **AUDIT-14 #1 timeout repro (#3):** it never reaches the entailment check. The `_SETUP_SHARE` guard returns first on every run, so the repro tests the guard by timing. It caught a removed guard in 1 of 3 runs.
- **The `!` half of SG20's annotation ban is load-bearing and untested (#4, F04).** Z3 reads `(! p:named false)` as `(! p :named false)`, but `_tokens` sees one token, `p:named`, so no token starts with `:`. With only the `:keyword` rule, a premise can redefine `false` again. That is AUDIT-13 #2, which is critical.
- **SG22's core fix is guarded only by `perf` tests (#5).** Restoring SG19's assumption check (S01), keeping a stale entailment timeout (S09), or a default `slow_workers` of 0 or 1 (S24, S25) passes everything under `-m "not perf"`.
- AUDIT-14 #1 still catches the original bug (S01: 3 of 3), and so does AUDIT-14 #2 against the pre-SG22 solver. No other repro was weakened.

## How this was measured

- **Coverage:** `uv run pytest --cov=autolab --cov-branch --cov-report=term-missing --cov-report=json tests` on the base gave 1026 passed, 12 skipped and 27 xfailed. The package figures are (covered lines + covered branches) / (lines + branches), taken from the JSON report.
- **Mutations:** a scratchpad harness (not committed) ran each mutant in a fresh copy of `src/`, `tests/`, `scripts/` and `pyproject.toml`, with `PYTHONPATH` set to the copy. It checked by importing `autolab` from the copy before each run. The worktree was never edited.
  - Each mutation is one exact-match replacement, asserted to occur exactly once. S01 removes `solver.add` and passes the claim to `check()` as an assumption, which is SG19's code.
  - **Pass 1:** `pytest -x -m "not perf"` with the per-test timeout (60 s), and `test_audit_20.py` excluded. `test_audit_6::test_shutdown_does_not_cancel_callers_on_other_loops` was deselected after it XPASSed on an unrelated mutant (#1).
  - **Pass 2:** the `perf` tests on every pass-1 survivor.
  - **Perf re-check:** each `perf` kill and the S03 guard mutant were re-run 3 times serially. A mutant counts as killed if it failed 3 of 3.
  - **Kill check:** every survivor and every timing-only kill was re-applied against `tests/audit/test_audit_20.py` alone.
- **Load:** pass 1 ran 2 mutants at a time on 4 CPUs while other agents' suites were also running, at a load average of 5–10. That load explains the false kills under #7.
- **Earlier repros:** `git diff 855b108 603e452 -- tests/audit/` for every flipped or changed repro. Each was then run against the pre-fix production file from `855b108`: `formalizer.py`, `common.py` + `design.py`, `solver.py` and `store.py`, plus the pre-SG22 `z3_spy` fixture for AUDIT-16 #2.

## Coverage

| Module | Stmts | Miss | Branch | BrPart | Cover | Missing |
|---|---:|---:|---:|---:|---:|---|
| agents/common.py | 119 | 0 | 32 | 0 | 100% | |
| agents/design.py | 46 | 1 | 6 | 1 | 96% | 66 (`design is None`) |
| agents/* (other 5) | 177 | 0 | 26 | 0 | 100% | |
| cli.py | 79 | 6 | 22 | 0 | 92% | 75, 84-86, 115-117 |
| monitoring/* | 150 | 0 | 38 | 1 | 99.5% | tracer 77->76 |
| orchestrator/* | 283 | 0 | 62 | 0 | 100% | |
| state/checkpointer.py | 95 | 11 | 38 | 14 | **80%** | 55-73, 116->150, 162 |
| state/models.py, schema.py, store.py | 297 | 0 | 36 | 0 | 100% | |
| tools/* | 122 | 5 | 26 | 4 | 94% | simulator 89; literature 81, 107, 124, 130 |
| verification/formalizer.py | 179 | 0 | 72 | 1 | 99.6% | 302->310 |
| verification/solver.py | 212 | 0 | 52 | 2 | 99.2% | 268->266, 381->exit |
| verification/llm.py | 69 | 1 | 16 | 2 | 96% | 189, 219->218 |
| verification/vericot.py | 128 | 1 | 50 | 1 | 99% | 244 |
| verification/ (other 3) | 71 | 0 | 18 | 0 | 100% | |
| **TOTAL** | 2059 | 25 | 494 | 26 | **97.8%** | |

| Package | Lines | Branches | Cover (line + branch) | Target |
|---|---:|---:|---:|---|
| verification/ | 99.7% | 97.1% | **99.1%** | ≥90% ✔ |
| agents/ | 99.7% | 98.4% | 99.5% | ≥70% ✔ |
| orchestrator/ | 100% | 100% | 100% | ≥70% ✔ |
| monitoring/ | 100% | 97.4% | 99.5% | ≥70% ✔ |
| state/ | 97.2% | 78.4% | 94.2% | ≥70% ✔ |
| tools/ | 95.9% | 84.6% | 93.9% | ≥70% ✔ |
| cli.py | 92.4% | 90.9% | 92.1% | ≥70% ✔ |

- Every line SG20–SG23 added is covered except `design.py` 66: no test runs a Design update that has no design in either `current_experiment` or `hitl_payload`.
- The new partial branches are `formalizer.py` 302->310 (`_sexprs` reaching the end of its tokens without an unmatched `)`) and `solver.py` 268->266 (the re-send loop finding the context already detached, a race).
- As in AUDIT-16, coverage hides the problems: all 15 survivors sit on fully covered lines.

## Mutation table

ID prefixes: F `formalizer.py` (SG20), S `solver.py` (SG22), A `agents/common.py` (SG21), D `agents/design.py` (SG21), T `state/store.py` (SG23). "perf 3/3" means the kill came only from a `perf` test, in 3 of 3 serial re-runs. In the last column, "→" names the `test_audit_20.py` test that now kills a mutant that survived or was killed only by timing.

| ID | Mutation | Result | Killed by |
|---|---|---|---|
| F01 | applied-builtin probe removed | killed | test_formalizer::test_merge_rejects_builtins_applied_as_declared |
| F02 | applied probe without the user sorts | killed | same |
| F03 | annotation ban: `:keyword` tokens allowed (`!` still refused) | survived (**equivalent**) | an annotation needs a separate `!` token, which `_tokens` always yields |
| **F04** | annotation ban: bare `!` allowed (`:keyword` still refused) | **survived** | → test_a_glued_named_annotation_is_refused |
| F05 | annotation ban removed | killed | test_formalizer::test_assertions_with_annotations_are_rejected |
| **F06** | earlier-use: vocabulary-declaration tokens not counted | **survived** | → test_a_name_used_only_in_a_declaration_cannot_be_declared_later |
| F07 | earlier-use: assertion tokens not counted | killed | test_formalizer::test_a_step_cannot_declare_a_name_an_earlier_step_used |
| F08 | earlier-use rule removed | killed | same |
| **F09** | earlier-use remembers only the last step | **survived** | → test_the_earlier_use_rule_remembers_every_earlier_step |
| F10 | earlier-use never records tokens | killed | test_formalizer::test_a_step_cannot_declare_a_name_an_earlier_step_used |
| F11 | earlier-use also applied to re-declarations | killed | test_analysis::test_analysis_agent_emits_report |
| **F12** | parametric `declare-sort` arity forced to 0 | **survived** | → test_the_applied_probe_refuses_a_parametric_builtin_sort_alone ×2 |
| **F13** | `declare-sort` probe use dropped | **survived** | same |
| F14 | `declare-fun` applied probe dropped | killed | test_formalizer::test_merge_rejects_builtins_applied_as_declared |
| F15 | no Int/Real coercion in the probe | survived (**equivalent**) | Z3 reports a builtin applied at the wrong sort with a sort or arity error, never "unknown constant", so the uncoerced probe already refuses it |
| F17 | applied probe: "unknown sort" counts as builtin | killed | test_formalizer::test_merge_accepts_names_z3_does_not_resolve_at_the_declared_arity |
| F18 | applied probe: "unknown constant" counts as builtin | killed | test_formalizer::test_merge_accepts_single_declaration_forms |
| F19 | applied probe ignores declared sorts | killed | test_formalizer::test_merge_rejects_builtins_applied_as_declared |
| F21 | `_sexprs` does not stop at the closing paren | killed | same |
| F22 | bare builtin probe removed | killed | test_formalizer::test_merge_rejects_z3_builtins_found_by_asking_z3[Float32] |
| **F23** | prompt: earlier-use rule dropped | **survived** | → test_the_formalize_prompt_states_the_earlier_use_rule |
| **S01** | SG19 assumption-based entailment check restored | killed (**perf 3/3** only) | test_audit_14 #1 ×2 → test_the_entailment_check_asserts_the_negated_claim_instead_of_assuming_it |
| S02 | negated claim never asserted | killed | test_analysis::test_analysis_agent_emits_report |
| S03 | `_SETUP_SHARE` guard removed | killed (sleep-based) | test_solver_executor::test_no_entailment_check_starts_…; test_audit_14 #1 only 1/3 → test_the_setup_guard_stops_a_check_with_less_than_half_its_consistency_time_left |
| S04 | `_SETUP_SHARE` = 0.25 | killed (12.5 ms margin) | same → same |
| **S05** | `_SETUP_SHARE` = 5.0 | killed (**perf 3/3** only) | test_audit_2::test_both_checks_share_one_deadline → test_an_entailment_check_with_enough_time_left_runs |
| S06 | consistency time not measured (`took = 0`) | killed (sleep-based) | test_solver_executor::test_no_entailment_check_starts_… → test_the_setup_guard_stops_… |
| **S07** | `took` includes the parse | **survived** | → test_a_slow_parse_does_not_count_as_consistency_time |
| **S09** | entailment check keeps the stale timeout | killed (**perf 3/3** only) | test_audit_2::test_both_checks_share_one_deadline → test_the_entailment_check_gets_the_time_left_not_the_initial_budget |
| S10 | `_SLICE_S` = 5 s | killed | test_solver_executor::test_slow_checks_are_bounded_by_slow_workers |
| S12 | no check ever becomes slow | killed | same |
| S13 | non-Z3 calls also give up their slot | killed (sleep-based) | test_solver_executor::test_only_a_running_z3_check_gives_up_its_slot → test_a_call_without_a_z3_context_never_leaves_its_slot |
| S14 | slow checks unbounded | killed | test_slow_checks_are_bounded_by_slow_workers |
| S15 | `>=` → `>` on `slow_workers` | killed | same |
| S16 | no extra thread for a slow check | killed | same |
| **S17** | pool thread limit never shrinks | **survived** (a `perf` "kill" 1/3 was noise) | → test_slow_checks_restore_the_pools_thread_limit |
| S18 | slow count never decremented | killed | test_slow_checks_are_bounded_by_slow_workers |
| S19 | a slow check keeps its slot (leak) | killed | same |
| S20 | slot never released for normal checks | killed | test_analysis::test_analysis_agent_emits_report (hang → timeout) |
| **S21** | queued pool future not cancelled | **survived** (a `perf` "kill" 1/3 was noise) | → test_a_caller_cancelled_while_its_check_is_queued_never_runs_it |
| S22 | a slow check is re-counted every slice | killed | test_slow_checks_are_bounded_by_slow_workers |
| S23 | `_wrap_assert` skip restored | killed | test_audit_15::test_a_premise_whose_predicate_starts_with_assert_is_kept |
| **S24** | default `SOLVER_SLOW_WORKERS` = 0 | killed (**perf 3/3** only) | test_audit_14 #2, …_on_one_worker → test_an_easy_check_overtakes_hard_ones_on_the_default_slow_workers |
| **S25** | default `SOLVER_SLOW_WORKERS` = 1 | killed (**perf 3/3** only) | …_on_one_worker → same |
| S26 | `attached()` always True | killed (sleep-based) | test_only_a_running_z3_check_gives_up_its_slot → test_a_call_without_a_z3_context_never_leaves_its_slot |
| S27 | `attached()` inverted | killed | test_slow_checks_are_bounded_by_slow_workers |
| A01 | only `$€£¥` as currency symbols | killed | test_design::test_protocol_figures_flags_currency_and_durations[₹40000] |
| A03 | no k/M multipliers | killed | same table |
| A04 | amount + currency word missed | killed | same table |
| A05 | amount + time unit missed | killed | same table |
| A06 | second-word unit (`500 US dollars`) missed | killed | same table |
| A08 | currency code before the amount missed | killed | same table |
| A09 | time word before the amount missed (`day 14`) | killed | same table |
| A10 | lead-in punctuation not skipped | killed | same table [`minutes: 30`] |
| A11 | dotted abbreviations not joined | killed | same table [`U.S.D.`] |
| A13 | time words case-sensitive | killed | test_audit_16::test_protocol_figures_ignore_case |
| A14 | short units case-insensitive | killed | test_design::test_protocol_figures_allows_non_cost_non_duration_numbers[16S] |
| A15 | currency words case-sensitive | killed | test_design table [`Rs.`] |
| **A17** | number words case-sensitive | **survived** | → test_protocol_figures_flag_capitalised_number_words_and_vulgar_numerals |
| A18 | number words are not amounts | killed | test_design table [`five`] |
| **A19** | superscripts and fractions are not numerals | **survived** | same → same ×3 |
| A20 | hyphen and `_` are not separators | killed | test_design table |
| A22 | parameter names not scanned | killed | test_design::test_design_figures_cover_parameter_names_values_and_units |
| A23 | parameter values and units not scanned | killed | same |
| A24 | parameter unit not scanned | killed | same |
| A25 | `_param_symbol` prefix match | killed | test_design::test_parameter_fact_symbols_are_plain_identifiers_that_stay_distinct |
| A26 | `_PLAIN` = `\w+` (Unicode) | killed | test_common::test_measurement_fact_symbols_merge_into_a_vocabulary_and_stay_distinct |
| **A27** | measurement names starting `m_` not hashed | **survived** | → test_a_measurement_named_like_a_hashed_symbol_keeps_its_own_symbol |
| A28 | digit-leading measurement names kept | killed | test_common::test_measurement_facts_are_parseable_real_equalities[600nm] |
| A29 | `_hashed` keeps Unicode letters | killed | test_common::test_measurement_fact_symbols_merge_… |
| A30 | hashed measurement without `m_` | killed | test_common::test_measurement_facts_are_parseable_real_equalities[OD 600] |
| D01 | parameter figures ignored | killed | test_design::test_design_whose_parameter_states_a_cost_is_escalated_not_admitted |
| D02 | protocol figures ignored | killed | test_design::test_design_whose_protocol_states_figures_is_escalated_not_admitted |
| D03 | only protocol figures escalate | killed | test_design::test_design_whose_parameter_states_a_cost_… |
| D05 | prompt rule omits parameters | killed | test_design::test_design_prompt_tells_the_model_to_keep_figures_out_of_the_protocol |
| D06 | flagged design still merged | killed | test_design::test_design_whose_protocol_states_figures_… |
| T01 | no busy timeout | killed | test_audit_6::test_store_append_waits_out_a_concurrent_writer |
| T02 | busy timeout 5 s | killed | same |
| **T03** | no WAL | **survived** | → test_a_file_store_uses_wal_and_the_busy_timeout |
| T05 | SQLite engine set up like any other | killed | test_audit_6::test_store_append_waits_out_a_concurrent_writer |

**Totals:** 80 mutations. The existing suite killed 65:
- 60 in pass 1;
- 5 only by `perf` tests (S01, S05, S09, S24, S25);
- 5 more of the 60 (S03, S04, S06, S13, S26) only by sleep-based unit tests.

15 survived. Two are equivalent (F03, F15). The 13 non-equivalent survivors are F04, F06, F09, F12, F13, F23, S07, S17, S21, A17, A19, A27 and T03. `tests/audit/test_audit_20.py` kills those 13 and the 10 timing-only kills, each confirmed by re-applying the mutant against that file alone (about 10 s per mutant, or 65 s for the slot tests).

**False kills under load:** in an aborted first run, 4 mutants (F03, F04, F06, F09) were attributed to `perf` tests or to the AUDIT-6 #7 XPASS. In pass 2, F04, F23, A27 and T03 were "killed" by `test_hard_runs_do_not_starve_an_easy_verify_on_one_worker` or by the AUDIT-14 #1 cancel repro, and none of those mutants can affect those tests. S17 and S21 were "killed" by `perf` tests in 1 of 3 re-runs, which is noise.

## Findings

### Critical

None.

### High

**#1. The AUDIT-6 #7 repro is timing-dependent again after SG22 (repro: `test_shutdown_does_not_cancel_a_queued_caller_on_another_loop`, strict xfail "AUDIT-20 #1").**
- *Why:* SG23 made the repro wait until both callers have submitted and one check is running. SG22, merged after it, raises the pool's thread limit when a check stays in Z3 past `_SLICE_S` (50 ms). If the second caller's thread submits after that, `ThreadPoolExecutor.submit` starts a second thread for it. Nothing is queued, `shutdown` cancels nothing, both callers get `timeout`, and the strict xfail XPASSes.
- *Observed:* the repro XPASSed once in this audit's mutation pass, on F03, which cannot affect it, at a load average of about 7. It passed as an xfail on a quiet machine.
- *Added:*
  - `test_the_audit_6_7_setup_admits_a_schedule_where_nothing_is_queued` (plain, deterministic): the repro's executor, with the second caller started once `_slow == 1`. It gives `["timeout", "timeout"]`, which is the XPASS schedule.
  - The repro above, with `slow_workers=0`, so the second check always waits in the pool. It fails on its assertion every run (`['timeout', 'CancelledError']`).
- *Suggested:* in `test_audit_6.py`, give `_ObservedExecutor` `slow_workers=0` (or wait for `_work_queue.qsize() == 1` before calling `shutdown`). The bug itself (AUDIT-6 #7) is still open.

**#2. The AUDIT-13 #2 repros no longer exercise the attack (weakened by SG20; repro: `test_audit_13_2_repros_fail_on_their_assertion_without_the_ban` ×2, strict xfail "AUDIT-20 #2").**
- *What changed:* SG20 changed `_register_steps` in `test_audit_13.py` to `continue` on any assertion containing `:named`. This skips the step's judge registration and does not extend the helper's vocabulary. The skip is hard-coded in the test, not derived from the formalizer.
- *Effect:* against the pre-SG20 `formalizer.py`, or with the annotation ban removed (F05), the formalizer accepts the `:named` step and extends its vocabulary. The next step's prompt then differs from the one the helper registered, so both repros fail with `KeyError: FakeLLMClient: no response registered`. They never reach `assert not _passes(result)` or `trigger(...) is not None`. They still go red on a regression, but only because of a fake mismatch. A change that made the verifier treat a missing reply as an ungrounded step would turn them green with the attack live.
- *Contrast:* the AUDIT-13 #1 repros use the production `Vocabulary.merged` in the helper, so they follow the code. Against the pre-SG20 formalizer they fail on their assertion (`'assumed' == 'premises_inconsistent'`, DID NOT RAISE ×3). They are not weakened.
- *The repro:* it switches the ban off (by filtering `!` and `:keyword` tokens out of `formalizer._tokens`) and requires each AUDIT-13 #2 repro to fail with `AssertionError`. Today both fail with `KeyError`.
- *Suggested:* in `_register_steps`, decide the skip by calling the formalizer's own checks (or `Formalizer.formalize` itself), as the `merged`/parse branch already does.

**#3. The AUDIT-14 #1 timeout repro tests the `_SETUP_SHARE` guard, by timing, and never times the entailment check (repro: `test_the_audit_14_1_timeout_repro_reaches_the_entailment_check`, strict xfail "AUDIT-20 #3").**
- *Setup:* SG22 sets the timeout to the time until the entailment check starts, plus 25% of the measured entailment check. On the add-based path, the 1600-premise entailment check takes about 0.25 s after a 0.45 s consistency check. That leaves about 0.06 s, which is less than `_SETUP_SHARE × 0.45 s` (0.22 s).
- *Measured:* in 4 of 4 runs the check returned `timeout` with details "too little time left for the entailment check" at a ratio of 0.85–0.98, after one `Solver.check` call.
- *What it still catches:* S01 (SG19's assumption check) fails both AUDIT-14 #1 repros 3 of 3. The pre-SG22 solver fails all three AUDIT-14 repros on their assertions (ratio 3.1, worker freed after 4.0 s and 4.4 s).
- *What it misses:* a regression of the guard half of SG22's fix. With the guard removed (S03), the timeout repro failed 1 of 3 runs. The cancel repro does reach the entailment check (it waits for the second `check` call), so it does test the add-based interrupt.
- *The guard's own unit test:* `test_no_entailment_check_starts_without_the_time_the_consistency_check_took` sleeps 0.15 s of a 0.2 s budget. Against a 0.25 share (S04), its margin is 12.5 ms of wall clock.
- *Added:* fake-clock tests that pin the guard's arithmetic without sleeps (S03, S04, S05, S06, S07): `test_the_setup_guard_stops_…`, `test_an_entailment_check_with_enough_time_left_runs` and `test_a_slow_parse_does_not_count_as_consistency_time`.
- *Suggested:* put the AUDIT-14 #1 deadline past the guard's threshold (consistency + `_SETUP_SHARE` × consistency + a fraction of the entailment check), so the entailment check runs and must stop at its timeout. Or assert that two `check` calls happened.

**#4. The `!` half of the annotation ban is load-bearing and untested (F04 survived). Without it, AUDIT-13 #2 (critical) is open again.**
- *How:* Z3's lexer ends a symbol at `:`, so `(! p:named false)` is `(! p :named false)`. `_tokens` splits only on whitespace, parens and quotes, so it sees one token, `p:named`. With only the `:keyword` rule (F04), no token starts with `:` and the assertion is admitted. Checked in Z3: after `(assert (! p:named false))`, `(assert (= false true))` parses to `p == True`, which is satisfiable.
- *Why the suite misses it:* every SG20 annotation case writes ` :named` with a space, which the `:` rule also refuses.
- *Added:* `test_a_glued_named_annotation_really_redefines_false_in_z3` (the precondition) and `test_a_glued_named_annotation_is_refused`.
- F03 (the reverse) is equivalent. Z3 needs `!` as a separate symbol for an annotation, and `_tokens` always yields it as one (`(!p :named …)` is a Z3 parse error).

**#5. SG22's entailment and fairness fixes are guarded only by `perf` tests (S01, S09, S24, S25; also S05).**
- Restoring SG19's assumption check (S01), keeping the first timeout for the entailment check (S09), or a default `SOLVER_SLOW_WORKERS` of 0 or 1 (S24, S25) passes the whole suite under `-m "not perf"`. Each is caught only by wall-clock tests: the AUDIT-14 #1 and #2 repros, `test_hard_runs_do_not_starve_an_easy_verify_on_one_worker` and `test_audit_2::test_both_checks_share_one_deadline`. They fail 3 of 3 serially, but they are the tests that flake under load (#7), and AUDIT-8 H1 suggests running `perf` separately.
- *Added (no wall-clock bound on the code under test):*
  - `test_the_entailment_check_asserts_the_negated_claim_instead_of_assuming_it` (S01: no `check` call takes assumptions);
  - `test_the_entailment_check_gets_the_time_left_not_the_initial_budget` (S09);
  - `test_an_easy_check_overtakes_hard_ones_on_the_default_slow_workers` (S12, S24, S25: completion order, with both hard checks confirmed slow first);
  - `test_an_entailment_check_with_enough_time_left_runs` (S05).

### Medium

**#6. Two SG22 unit tests decide by sleeping.**
- `test_only_a_running_z3_check_gives_up_its_slot` sleeps 4 slices (0.2 s), then asserts that a queued call has not run. Under load, the queued call may not have run yet even when the guard is broken (S13, S26), so it fails in the passing direction.
- `test_no_entailment_check_starts_…`: see #3.
- *Added:* `test_a_call_without_a_z3_context_never_leaves_its_slot`, which records every `_to_slow` decision and waits on an event for three of them. It kills S13 and S26.

**#7. The `perf` tests still fail under load (AUDIT-8 H1 and AUDIT-16 #8, carried).**
- These tests failed on mutants that cannot affect them:
  - `test_hard_runs_do_not_starve_an_easy_verify_on_one_worker` (F04, F23, T03);
  - the AUDIT-14 #1 cancel repro (A27, T03);
  - `test_default_pool_stays_responsive_under_fifty_slow_checks` (F03, F04, F09, in the aborted first run);
  - `test_audit_14::test_hard_runs_do_not_starve_an_easy_verify` (S17, S21, 1 of 3 each).
- So `make test` is not reliably green on a loaded machine. SG23's "green 5 times under load" sweep ran before SG22 added these tests and changed scheduling.

**#8. Two SG22 executor details had no test (S17, S21 survived).**
- *S17:* the pool's `_max_workers` never shrinks back after slow checks. The pool could then grow up to the total number of slow checks ever run, not `workers + slow_workers`.
- *S21:* without `future.cancel()`, a caller cancelled while its check waits in the pool queue still runs the check later. It is never attached, so it stays in the `asyncio.wait` loop, where only that line drops it.
- *Added:* `test_slow_checks_restore_the_pools_thread_limit` (also kills S18, S22) and `test_a_caller_cancelled_while_its_check_is_queued_never_runs_it`.

**#9. Half of SG20's structural earlier-use rule is untested (F06, F09 survived).**
- The SG20 test declares each name in the step right after it was used, so remembering only the last step's tokens (F09) passes it.
- Nothing covers names that appear only in the vocabulary's declarations (F06).
- With the probes in place, both are defense in depth. But this rule is what SG20 relies on for builtins the probes miss (AUDIT-13 #1, critical).
- *Added:* `test_the_earlier_use_rule_remembers_every_earlier_step` and `test_a_name_used_only_in_a_declaration_cannot_be_declared_later`, both with the probes patched out.

**#10. `protocol_figures` details are unpinned (A17, A19 survived).**
- "Incubate for Four hours", "½ hour", "³ days" and "¼ h" all depend on code that no test reaches. Every number word in the SG21 table is lower case, and every amount is in ASCII digits.
- If either piece were removed, these durations would pass the SG17 guard, as with AUDIT-16 A05.
- *Added:* `test_protocol_figures_flag_capitalised_number_words_and_vulgar_numerals` ×4.

**#11. Carried:** coverage targets are still not enforced by `make test` or CI (AUDIT-8 M5). `state/checkpointer.py` is still at 80%, with no `RedisCheckpointer` test (AUDIT-8 M6).

### Low

- **L1. The applied `declare-sort` probe was tested only alongside the bare one (F12, F13).** Every parametric builtin sort tried (`Set`, `List`, `Seq`, `Array`, `BitVec`, `FloatingPoint`, `RegEx`) is also refused by the bare probe, which hits an arity error. *Added:* `test_the_applied_probe_refuses_a_parametric_builtin_sort_alone` ×2, with the bare probe patched out.
- **L2. The prompt's earlier-use rule is unpinned (F23).** *Added:* `test_the_formalize_prompt_states_the_earlier_use_rule`.
- **L3. `took` includes the parse (S07) survived.** This is liveness only: more `timeout`s, which fails closed. *Added:* `test_a_slow_parse_does_not_count_as_consistency_time` (fake clock).
- **L4. `m_` hashing is unpinned (A27).** Without it, a measurement literally named `m_OD_600_f098d37748c9` takes the fact symbol of `OD 600`. *Added:* `test_a_measurement_named_like_a_hashed_symbol_keeps_its_own_symbol`.
- **L5. WAL is unpinned (T03).** The busy timeout alone passes every test. *Added:* `test_a_file_store_uses_wal_and_the_busy_timeout`, which also checks `PRAGMA busy_timeout` = 30000.
- **L6. `design.py` 66 is never run.** No test has a Design update without a design.
- **L7. `_tokens` and Z3's lexer disagree on `:` inside a symbol.** That is the cause of #4. It does not affect the earlier-use rule today, because Z3 rejects a keyword outside an annotation. But any future token rule built on `_tokens` should be checked against Z3's lexing.

## Earlier repros after their xfail was removed or their setup changed

Diffs: `git diff 855b108 603e452 -- tests/audit/`. Each repro was run against the pre-fix production file.

| Repro | Change in SG20–SG23 | Against the pre-fix code | Verdict |
|---|---|---|---|
| AUDIT-6 #2 | marker removed | fails: `database is locked` (pre-SG23 `store.py`) | not weakened; kills T01, T02, T05 |
| AUDIT-6 #3 | waits recorded instead of wall-clock starts; `perf` dropped | `--runxfail`: `assert 0.0 >= 20.0` | not weakened (stronger: no scheduling noise) |
| AUDIT-6 #7 | waits on submissions and a running check (SG23) | xfail on a quiet machine | **weakened by SG22's scheduling: XPASSes under load (#1)** |
| AUDIT-10 #1 | serial and pooled alternate; best of 3 rounds | `SOLVER_WORKERS = 8` fails (min ratio 6.2, bound 2) | not weakened |
| AUDIT-13 #1 ×2 (+ ×3 vocab cases) | markers; helper skips steps `merged`/parse refuses | all fail on their assertions (pre-SG20 `formalizer.py`) | not weakened |
| AUDIT-13 #2 ×2 | markers; helper skips any `:named` step | both fail with `KeyError`, not on their assertions | **weakened (#2)** |
| AUDIT-13 #3 ×6 | marker | all fail (`hitl_pending is None`), pre-SG21 `common.py`/`design.py` | not weakened |
| AUDIT-14 #1 timeout | deadline at 25% into the measured entailment check | pre-SG22 `solver.py`: ratio 3.1 > 1.5; S01: 3/3 | catches the original bug; **tests only the guard, by timing; S03 caught 1/3 (#3)** |
| AUDIT-14 #1 cancel | cancel 25% into the entailment check, after the 2nd `check` | pre-SG22: freed after 4.0 s; S01: 3/3 | not weakened (it does reach the entailment check); flakes under load (#7) |
| AUDIT-14 #2 | marker | pre-SG22: 4.4 s > 0.5 s; S24: 3/3 | not weakened |
| AUDIT-15 #1 ×2, #3 ×3, #4 | markers | all fail on their assertions (pre-SG21) | not weakened |
| AUDIT-15 #2 | marker | fails (`'identifier' in …`), pre-SG20 | not weakened |
| AUDIT-15 #5 | marker | fails (`not_entailed`), pre-SG22 `solver.py`; kills S23 | not weakened |
| AUDIT-16 #1 ×5 | markers | all fail on their assertions (pre-SG21) | not weakened |
| AUDIT-16 #2 | marker; `z3_spy` sends then signals | pre-SG22 fixture: `[True] == [False]` | not weakened |

## Added tests (`tests/audit/test_audit_20.py`)

| Test | Kills / reproduces |
|---|---|
| test_the_audit_6_7_setup_admits_a_schedule_where_nothing_is_queued | #1 (the XPASS schedule) |
| test_shutdown_does_not_cancel_a_queued_caller_on_another_loop (strict xfail) | AUDIT-20 #1 |
| test_audit_13_2_repros_fail_on_their_assertion_without_the_ban ×2 (strict xfail) | AUDIT-20 #2 |
| test_the_audit_14_1_timeout_repro_reaches_the_entailment_check (strict xfail) | AUDIT-20 #3 |
| test_an_easy_check_overtakes_hard_ones_on_the_default_slow_workers | S12, S24, S25 |
| test_a_call_without_a_z3_context_never_leaves_its_slot | S13, S26 |
| test_slow_checks_restore_the_pools_thread_limit | S17 (S18, S22) |
| test_a_caller_cancelled_while_its_check_is_queued_never_runs_it | S21 |
| test_the_entailment_check_gets_the_time_left_not_the_initial_budget | S09 |
| test_the_entailment_check_asserts_the_negated_claim_instead_of_assuming_it | S01 |
| test_the_setup_guard_stops_a_check_with_less_than_half_its_consistency_time_left | S03, S04, S06 |
| test_an_entailment_check_with_enough_time_left_runs | S05 |
| test_a_slow_parse_does_not_count_as_consistency_time | S07 |
| test_a_file_store_uses_wal_and_the_busy_timeout | T03 |
| test_a_glued_named_annotation_really_redefines_false_in_z3 | F04 precondition |
| test_a_glued_named_annotation_is_refused | F04 |
| test_the_earlier_use_rule_remembers_every_earlier_step | F09 |
| test_a_name_used_only_in_a_declaration_cannot_be_declared_later | F06 |
| test_the_applied_probe_refuses_a_parametric_builtin_sort_alone ×2 | F12, F13 |
| test_the_formalize_prompt_states_the_earlier_use_rule | F23 |
| test_protocol_figures_flag_capitalised_number_words_and_vulgar_numerals ×4 | A17, A19 |
| test_a_measurement_named_like_a_hashed_symbol_keeps_its_own_symbol | A27 |

Verification: `make lint typecheck test` gives ruff clean, 134 files already formatted, mypy strict clean on 124 source files, and **1049 passed, 12 skipped, 31 xfailed**. The base gave 1026 passed and 27 xfailed, so this adds 23 passing tests and 4 strict xfails.
