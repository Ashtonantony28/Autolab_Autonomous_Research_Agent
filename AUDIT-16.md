# AUDIT-16: Test coverage and test quality (round 4)

Branch `claude/audit-16`, based on `origin/claude/sg19-solver-pool` (`fee50eb`, SG15–SG19 merged). Independent audit; no production code was changed.

**Bottom line:** coverage is unchanged and high: 98% line and branch overall, 98.8% on `verification/`, 100% on `agents/` and `orchestrator/`. I ran 85 one-line mutations over the code that SG15–SG19 changed. The existing suite killed 60 and missed 25; 8 of the 25 are equivalent. `tests/audit/test_audit_16.py` now kills all 17 non-equivalent survivors. Each kill was confirmed by re-applying the mutant. Severity counts: **0 critical, 3 high, 5 medium, 6 low**. The main problems:
- **The formalizer's `;` refusal is load-bearing but untested (#3, F10).** A `;` comment can hide parens and split `(` from a command head. Without the refusal, an assertion that smuggles `set-option` or `declare-fun` passes the single-term check, so AUDIT-9 #2 is open again. The only `;` case in the SG15 table, `p ; comment`, is refused by other rules too.
- **`protocol_figures` misses the usual adjectival forms (#1, repro).** "a 240-minute period", "a 12-hour soak", "2 weeks" and "5k USD" all pass. With one hyphen, an honest trace about a 240-minute protocol verifies at 1.0 and the 30-minute field skips the duration pause. That is the AUDIT-12 #1 attack.
- **Parts of the SG19 interrupt guarantee are untested or tested only by timing (#4).** Four survivors: re-sending only once (S11), a re-send thread that never ends (S12), and a cancel before `attach` being lost (S07, S18). The re-send loop was guarded only by the AUDIT-12 #2 repro, and only because the cancel-to-check gap happened to be under 5 ms.
- **The SG19 spy does not hold the drop window it claims to hold (#2, repro).** It signals `interrupted` *before* it sends the real interrupt. With the re-send removed, `test_cancelling_a_caller_interrupts_its_z3_context` still passed in 1 of 15 runs.
- No earlier repro was weakened when its xfail was removed. SG19's edit to the AUDIT-2 starvation test still tests what AUDIT-2 meant.

## How this was measured

- **Coverage:** `uv run pytest --cov=autolab --cov-branch --cov-report=term-missing --cov-report=json tests`. Base: 913 passed, 12 skipped, 26 xfailed. With `test_audit_16.py`: 931 passed, 12 skipped, 32 xfailed. Same coverage both times. Package figures are (covered lines + covered branches) / (lines + branches), from the JSON report.
- **Mutations:** a harness in the scratchpad (not committed) ran each mutation in a fresh copy of `src/`, `tests/`, `scripts/` and `pyproject.toml`, with `PYTHONPATH` pointed at the copy. Before each run it imported `autolab` from the copy to check that the copy was the one in use. The worktree was never edited.
  - Each mutation is one exact-match replacement, asserted to occur exactly once.
  - **Pass 1:** `pytest -x -m "not perf"` with the per-test `pytest-timeout` (60 s), `test_audit_16.py` excluded. `test_audit_6::test_shutdown_does_not_cancel_callers_on_other_loops` was deselected because it XPASSes under load (#8).
  - **Pass 2:** every survivor, and every kill whose first failure could not plausibly be caused by its mutation, re-ran the full suite *including* `perf`, without `-x`. The table shows the pass-2 result.
  - **Kill check:** each non-equivalent survivor was re-applied against `tests/audit/test_audit_16.py` alone.
- **Load:** mutants ran 2 at a time on 4 cores (3 at a time in an aborted first attempt), while other agents' suites were running too. The container restarted once during pass 1, and the remaining mutants were resumed from the partial results.
- **Earlier repros:** `git diff 7173a09 fee50eb -- tests/audit/` for every flipped repro. AUDIT-9 #1–#3 were also run against the pre-SG15/16 `formalizer.py` and `vericot.py` (taken from `7173a09`) to confirm that they still fail there.

## Coverage

| Module | Stmts | Miss | Branch | BrPart | Cover | Missing |
|---|---:|---:|---:|---:|---:|---|
| agents/* (6 files) | 272 | 0 | 36 | 0 | 100% | |
| cli.py | 79 | 6 | 22 | 0 | 92% | 75, 84-86, 115-117 |
| config.py | 29 | 0 | 0 | 0 | 100% | |
| monitoring/dashboard.py | 39 | 0 | 6 | 0 | 100% | |
| monitoring/events.py | 26 | 0 | 0 | 0 | 100% | |
| monitoring/metrics.py | 34 | 0 | 4 | 0 | 100% | |
| monitoring/tracer.py | 51 | 0 | 28 | 1 | 99% | 77->76 |
| orchestrator/checkpoints.py | 96 | 0 | 24 | 0 | 100% | |
| orchestrator/graph.py | 91 | 0 | 18 | 0 | 100% | |
| orchestrator/recovery.py | 73 | 0 | 16 | 0 | 100% | |
| orchestrator/routing.py | 23 | 0 | 4 | 0 | 100% | |
| state/checkpointer.py | 95 | 11 | 38 | 14 | **80%** | 55-56, 60, 63, 66, 69-73, 116->118 … 148->150, 162 |
| state/models.py, schema.py, store.py | 280 | 0 | 32 | 0 | 100% | |
| tools/lab_hardware/simulator.py | 43 | 1 | 12 | 0 | 98% | 89 |
| tools/literature.py | 70 | 4 | 14 | 4 | 90% | 81, 107, 124, 130 |
| verification/confidence.py, faithfulness.py, premise_grounder.py | 71 | 0 | 18 | 0 | 100% | |
| verification/formalizer.py | 126 | 0 | 48 | 0 | 100% | |
| verification/llm.py | 69 | 1 | 16 | 2 | 96% | 189, 219->218 |
| verification/solver.py | 180 | 1 | 44 | 3 | 98% | 200, 250->248, 331->exit |
| verification/vericot.py | 128 | 1 | 50 | 1 | 99% | 244 |
| **TOTAL** | 1896 | 25 | 430 | 25 | **98%** | |

| Package | Lines | Branches | Cover (line + branch) | Target |
|---|---:|---:|---:|---|
| verification/ | 99.5% | 96.6% | **98.8%** | ≥90% ✔ |
| agents/ | 100% | 100% | 100% | ≥70% ✔ |
| orchestrator/ | 100% | 100% | 100% | ≥70% ✔ |
| monitoring/ | 100% | 97.4% | 99.5% | ≥70% ✔ |
| state/ | 97.1% | 77.1% | 93.9% | ≥70% ✔ |
| tools/ | 95.9% | 84.6% | 93.9% | ≥70% ✔ |
| cli.py | 92.4% | 90.9% | 92.1% | ≥70% ✔ |

- Every line SG15–SG19 added is covered. The only new partial branch is `solver.py` 250->248, where the re-send loop finds the context already detached. It is reachable only through a race.
- `solver.py` 200 is `_wrap_assert` on a body that starts with `(assert`. It is dead code: `_is_single_term` refuses `assert` heads, and facts are built in code. `vericot.py` 244 and `solver.py` 331->exit are as in AUDIT-8/12.
- Coverage hides most of what follows: 17 survivors sit on fully covered lines.

## Mutation table

The ID prefix gives the area: F `formalizer.py`, V `vericot._payload_overrides`, S `solver.py`, A `agents/common.py`, D `agents/design.py`, K `checkpoints.py`, G `graph._hitl_observer`, M `monitoring/*`, L `cli.py`.

Kills marked † were first attributed in pass 1 to `test_graph_observability::test_concurrent_traced_runs_share_one_store[30]`, which flakes under load (#8). The pass-2 result is shown.

| ID | Mutation | Result | Killed by |
|---|---|---|---|
| F01 | `_IDENTIFIER.fullmatch` → `.match` | killed | test_formalizer::test_merge_rejects_names_that_are_not_plain_identifiers[str.len] |
| F02 | identifier regex also allows `.` and `-` | killed† | same |
| F03 | builtin probe skipped | killed | test_audit_9::test_vocabulary_rejects_every_z3_builtin[bvult] |
| F04 | probe: "other error" means not builtin | killed | same |
| F05 | probe: a successful parse means not builtin | killed | test_formalizer::test_merge_rejects_z3_builtins_found_by_asking_z3[RNE] |
| F06 | sort probe removed | killed | …[Float32] |
| F07 | term probe removed | killed | test_audit_9::test_vocabulary_rejects_every_z3_builtin[bvult] |
| F08 | probe condition inverted | killed | test_audit_1::test_circular_premise_restating_payload_is_not_admitted |
| F09 | probe in a fresh `z3.Context()` | survived (**equivalent**) | performance only; results are `lru_cache`d |
| **F10** | `;` no longer refused | **survived**† | test_audit_16::test_a_command_hidden_by_comments_is_refused_before_any_parse ×2 |
| F11 | `\|` no longer refused | killed† | test_formalizer::test_assertion_must_be_exactly_one_term[\|p\|] |
| **F12** | `depth < 0` → `depth < -1` | **survived** | test_audit_16::test_a_term_closed_before_it_opens_is_refused_before_any_parse |
| F13 | command check only for declaration heads | killed | test_formalizer::test_assertion_must_be_exactly_one_term[(not (set-option …))] |
| F14 | command-head check removed | killed | same |
| F15 | tokens after the first term allowed | killed† | …[p q], …[(p) q] |
| F16 | `return done` → `return depth == 0` | survived (**equivalent**) | they differ only on an empty assertion, which `FormalizedStep` rejects (`min_length=1`, stripped) |
| F17 | string token without `""` escapes | survived (**equivalent**) | the quotes pair up the same way, so no code is exposed; they differ only on a top-level non-Bool string |
| F18 | single-term check removed | killed | test_audit_9::test_assertion_cannot_smuggle_a_builtin_redeclaration |
| F19 | `set-option` dropped from `_SMT_COMMANDS` | killed | test_formalizer::test_merge_rejects_reserved_symbols[set-option] |
| F20 | `len(text)` instead of `len(text.rstrip())` | survived† (**equivalent**) | the assertion is already stripped by the model |
| V01 | pass-1 non-VALID premise not kept | killed | test_audit_9::test_premise_inconsistent_in_pass_one_cannot_prove_the_claim_in_pass_two |
| V02 | only dropped premises returned | killed | same |
| V03 | kept verdicts returned even when nothing is dropped | survived (**equivalent**) | the re-check reproduces the same verdicts |
| V04 | dropped reason `timeout`, not `no_premises` | killed | test_payload_proof::test_premise_entailing_the_conclusion_alone_is_circular |
| V05 | inconclusive circularity check treated as not circular | killed | test_payload_proof::test_inconclusive_circularity_check_fails_closed |
| V06 | restated-claim test without normalisation | killed | test_payload_proof::test_premise_restating_the_claim_text_is_circular_whatever_its_formula |
| V07 | inconclusive tautology check passes | killed | test_payload_proof::test_inconclusive_tautology_check_fails_closed |
| **V08** | only INVALID (not UNGROUNDED) pass-1 premises kept | **survived** | test_audit_16::test_premise_ungrounded_in_pass_one_is_not_assumed_in_pass_two |
| S01 | `SOLVER_WORKERS = cpu_count` | killed (perf only) | test_audit_10::test_the_default_solver_pool_does_not_inflate_solver_ms_under_concurrency |
| S02 | `SOLVER_WORKERS = 1` | killed† (perf only) | test_latency::test_concurrent_traces_do_not_serialize |
| S03 | `from_string` → parse + per-assertion `add` | killed | test_solver_executor::test_a_parse_cut_short_by_a_cancel_is_not_a_formalization_failure |
| **S04** | negated claim named `claim_neg` (declarable) | **survived** | test_audit_16::test_the_negated_claim_name_cannot_be_declared_by_a_step |
| S05 | parse error after a request not reported as `interrupted` | killed | test_solver_executor::test_a_parse_cut_short_by_a_cancel… |
| S06 | `unknown` after a request not reported as `interrupted` | killed | test_solver_executor::test_cancelling_a_caller_interrupts_its_z3_context |
| **S07** | `attach` result ignored | **survived** | test_audit_16::test_a_cancel_before_the_context_is_attached_refuses_the_check |
| S08 | no `reset_params()` | killed | test_solver_executor::test_each_check_starts_from_default_global_parameters |
| S09 | `reset_params()` after the context is created | killed | same |
| S10 | no re-send thread | killed | test_audit_12::test_cancel_between_the_two_z3_checks_still_stops_the_second |
| **S11** | re-send once, not in a loop | **survived** | test_audit_16::test_interrupt_is_resent_until_a_check_that_started_late_stops |
| **S12** | `detach` does not end the re-send thread | **survived** | test_audit_16::test_the_resend_thread_ends_when_its_check_ends |
| S13 | request before attach not recorded | killed | test_solver_executor::test_interrupt_before_the_check_starts |
| S14 | `_REARM_S = 2.0` | killed | test_audit_12::test_cancel_between_the_two_z3_checks… |
| S15 | counter-model via z3py's printer | killed | test_audit_10::test_counter_models_are_never_printed_on_two_threads_at_once |
| S16 | `shutdown` sends no interrupts | killed | test_solver_executor::test_shutdown_interrupts_the_running_z3_context |
| S17 | cancel sends no interrupt | killed | test_audit_12::test_cancel_between_the_two_z3_checks… |
| **S18** | `attach` succeeds after a request | **survived** | test_audit_16::test_a_cancel_before_the_context_is_attached_refuses_the_check |
| S19 | `(= claim! (not C))` → `(=> claim! (not C))` | survived (**equivalent**) | the same under the `claim!` assumption, and both are satisfiable without it |
| S20 | `detach` keeps the context reference | survived (**equivalent**) | the re-send stops on the event, and a context is never reused |
| S21 | checks on asyncio's default executor | killed | test_solver_executor::test_callers_beyond_the_worker_count_wait_outside_the_pool; also test_audit_2 starvation (checked separately, 2/2) |
| A01 | only `$` as currency symbol | killed | test_design::test_protocol_figures_flags_currency_and_durations[£40 of antibody] |
| A02 | no "code before number" alternative | killed | …[reagent costs USD 1,200] |
| A03 | no "number before currency" alternative | killed | …[reagent costs 30 EUR] |
| A04 | no `s` / `h` units | killed | …[incubate for 2h] |
| **A05** | no `re.IGNORECASE` | **survived** | test_audit_16::test_protocol_figures_ignore_case ×3 |
| **A06** | `_NUMBER = \d+` | **survived** | test_audit_16::test_protocol_figures_report_the_whole_number |
| **A07** | no `\b` after currency words | **survived** (pass 2: false kill by the AUDIT-10 perf test) | test_audit_16::test_protocol_figures_need_a_whole_currency_word |
| A08 | no `\b` after duration units | killed | test_design::test_protocol_figures_allows_non_cost_non_duration_numbers[run 3 steps …], test_scenarios |
| A09 | `_param_symbol` not injective | killed | test_audit_11::test_distinct_parameters_get_distinct_fact_symbols |
| A10 | `paramx_` prefix → `param_` | survived (**equivalent**) | a collision would need a name that is literally `<sym>_<12-hex hash>` |
| **A11** | hash suffix 1 hex digit | **survived** | test_audit_16::test_parameter_symbols_stay_distinct_for_many_names_with_one_sanitized_form |
| **D01** | figures checked only on an admitted design | **survived** | test_audit_16::test_an_escalated_design_with_protocol_figures_names_them_in_the_request |
| D02 | design still merged when figures found | killed | test_audit_12::test_design_concluding_the_bound_claim_cannot_hide_its_protocol_cost |
| D03 | request trigger `policy` | killed | test_design::test_design_whose_protocol_states_figures_is_escalated_not_admitted |
| D04 | design not withheld in `hitl_payload` | killed | same |
| D05 | prompt rule dropped | killed | test_design::test_design_prompt_tells_the_model_to_keep_figures_out_of_the_protocol |
| K01 | observer never called | killed | test_audit_11::test_an_approved_analysis_report_is_persisted |
| **K02** | `latency_ms = 0.0` | **survived** | test_audit_16::test_hitl_outcome_times_the_resolution_including_the_policy |
| **K03** | timing starts after `_resolve` | **survived** | same |
| G01 | re-pause not counted | killed | test_hitl_tracing::test_a_policy_repause_is_traced |
| G02 | decision event has no trigger | killed | test_hitl_tracing::test_an_approved_analysis_report_is_persisted_and_traced |
| G03 | note dropped | killed | same |
| G04 | approved update not persisted | killed | test_audit_11::test_an_approved_analysis_report_is_persisted |
| G05 | re-pause event carries the old trigger | killed | test_hitl_tracing::test_a_policy_repause_is_traced |
| G06 | re-pause event carries the old reason | killed | same |
| G07 | metrics count only the first decision | killed | same |
| G08 | no observer without a tracer | killed | test_hitl_tracing::test_hitl_events_need_no_tracer |
| G09 | event has no agent | killed | test_hitl_tracing::test_an_approved_analysis_report_is_persisted_and_traced |
| **G10** | event `latency_ms = 0.0` | **survived** | test_audit_16::test_hitl_event_keeps_the_outcomes_timing |
| **G11** | event `started_at` defaulted | **survived** | same |
| G12 | event has no `hitl_reason` | killed | test_hitl_tracing::test_an_approved_analysis_report_is_persisted_and_traced |
| M01 | `observe_hitl` does not accumulate | killed | test_hitl_tracing::test_a_policy_repause_is_traced |
| M02 | `abort` dropped from `NodeDecision` | killed | test_audit_11::test_resume_abort_does_not_report_a_completed_run |
| L01 | `aborted` always False | killed | same |
| L02 | `aborted` set for `retry` too | killed | test_cli_resume::test_resume_retry_passes_the_note_as_feedback |
| L03 | abort message removed | killed | test_audit_11::test_resume_abort_does_not_report_a_completed_run |

**Totals:** 85 mutations. The existing suite killed 60 of them; S01 and S02 only through `perf` tests. 25 survived, 8 of them equivalent (F09, F16, F17, F20, V03, S19, S20, A10). The 17 non-equivalent survivors are F10, F12, V08, S04, S07, S11, S12, S18, A05, A06, A07, A11, D01, K02, K03, G10 and G11. `tests/audit/test_audit_16.py` kills every one of them. Re-applying each mutant against that file alone takes about 5 s per mutant, or up to 18 s for the interrupt tests.

**False kills under load:** in pass 1, 6 mutants (F02, F10, F11, F15, F20, S02) were first attributed to the store flake. A07 was killed by `test_audit_10`'s perf bound in pass 2. Pass 2 found F10, F20 and A07 to be survivors.

## Findings

### Critical

None.

### High

**#3. The formalizer's `;` refusal is load-bearing and untested (F10 survived). Without it, AUDIT-9 #2 is open again.**
- *Why it matters:* Z3 skips a `;` comment to the end of the line. `_is_single_term` instead reads `;` as an atom and keeps counting the parens after it. So a comment can do two things:
  - hide parens, so the scanner sees one balanced term;
  - put a newline between `(` and a command head, so the scanner never sees `(` directly before `set-option` or `declare-fun`.
- *Example:* `(and p ;((⏎)) (;⏎set-option :rlimit 1) (;⏎assert (and p) ;))⏎`. Under F10 it passes the scanner as one term. Checked against Z3: the parse yields 2 assertions, and `rlimit` becomes 1 for the process. The `declare-fun str.len` variant reaches the solver's `from_string` and shadows the builtin, which is AUDIT-9 #2's first repro. SG19's `reset_params()` only covers the `set-option` half.
- *Why the suite misses it:* the SG15 table's only `;` case is `"p ; comment"`, and the scanner also refuses that because a token follows the complete term `p`.
- *Added:* `test_a_command_hidden_by_comments_is_refused_before_any_parse` (×2, with a spy that asserts no parse runs). `test_the_comment_smuggled_command_really_runs_in_z3` pins the precondition. Both kill F10.

**#1. `protocol_figures` misses durations written the usual way (repro: `test_hyphenated_duration_in_the_protocol_cannot_skip_the_duration_pause`, strict xfail "AUDIT-16 #1").**
- *Gaps:* the duration alternative requires whitespace or nothing between the number and the unit, followed by a word boundary, and it has no week unit. So "a 240-minute period", "a 12-hour soak" (the standard adjectival form), "culture for 2 weeks" and "5k USD" all return `()`.
- *Repro:* the AUDIT-12 #1 setup with protocol "Incubate the plate for a 240-minute period" and fields $5 / 30 min. The trace is honest (`minutes = 240` ⇒ chosen ⇒ `design_claim`). Result: confidence 1.0 (asserted, so the repro has its precondition), `hitl_pending` None, and the design is merged without the > 1 h duration pause. SG17 lists "figures in words" as a limit, but these are digits.
- *Also:* `test_protocol_figures_flags_hyphenated_durations_and_weeks` (×4, strict xfail).
- *Suggested:* allow `[\s-]*` between the number and the unit; add `w`, `wk(s)`, `weeks?`; allow `k`/`m` multipliers before currency. Better still, refuse any number directly followed by a time or currency word, whatever the separator. Add the four forms to the rejected table.

**#4. Parts of the SG19 interrupt guarantee are untested, or tested only by timing (S07, S11, S12, S18 survived).**
- *S11 (one re-send instead of a loop):* survived the full suite, `perf` included. The loop is what makes "an interrupt that lands before `Solver.check()` starts is not lost" hold when the gap is longer than `_REARM_S` (5 ms). Two examples are a `from_string` parse of a 100-step trace and `reset_params` plus context creation. The AUDIT-12 #2 repro kills S10 (no re-send at all) but not S11, because its cancel-to-check gap is under 5 ms. So the loop was guarded only by that timing.
- *S12 (`detach` does not end the re-send thread):* every cancelled or shut-down check would leave a daemon thread waking every 5 ms for the life of the process. Nothing checked that the thread exits.
- *S07, S18 (a cancel before `attach` is lost):* if `check_entailment` ignores `attach()`'s result (S07), or `attach` succeeds after a request (S18), a cancel that lands while the worker is between `_call` and `attach` is lost. The worker then stays busy until the Z3 timeout, which is the AUDIT-12 #2 impact. `test_interrupt_before_the_check_starts` kills S13 (request not recorded) but neither of these.
- *Added (deterministic, no wall-clock bound on the code under test):*
  - `test_interrupt_is_resent_until_a_check_that_started_late_stops` holds the entailment check until 3 interrupts have been *sent* (counted after the real `Context.interrupt()` returns), so Z3 drops all of them. It then asserts that the check, with a 5 s Z3 timeout, ends in under 2.5 s. Kills S10 and S11.
  - `test_the_resend_thread_ends_when_its_check_ends` kills S12.
  - `test_a_cancel_before_the_context_is_attached_refuses_the_check` cancels while the worker is blocked before `check_entailment`, then asserts `interrupted` with no `Solver.check` call at all. Kills S07, S13 and S18.

### Medium

**#2. The SG19 `z3_spy` does not hold the window it claims to hold (repro: `test_z3_spy_signals_only_after_the_interrupt_is_sent`, strict xfail "AUDIT-16 #2").**
- The fixture's `interrupt` wrapper calls `spy.interrupted.set()` *before* `real_interrupt(self)`. The held check waits on that event, so it can start its real `Solver.check` before the first interrupt is sent. The interrupt then lands inside the check, and the test passes without the re-send.
- *Measured:* with S10 applied, 15 runs of the cancel and shutdown pair. `test_cancelling_a_caller_interrupts_its_z3_context` passed once (run 0); the other 14 runs failed both tests after the full 20 s `_HANG_S`.
- This is not "tested only by timing", because the AUDIT-12 #2 repro and now `test_audit_16` kill S10 deterministically. But the SG19 comment ("The first interrupt lands before the check starts, where Z3 drops it") is not guaranteed.
- *Suggested:* in `z3_spy`, call `real_interrupt(self)` first and then `spy.interrupted.set()`. Better, wait for a count of sent interrupts, as `_HeldCheck` in `test_audit_16.py` does.

**#5. A premise UNGROUNDED in pass 1 can be assumed in pass 2 (V08 survived).**
- SG16's contract: "A premise that was not VALID keeps its verdict". Both SG16 tests use INVALID premises. Changing the check to `== "INVALID"` lets a premise whose pass-1 consistency check was inconclusive drop its UNGROUNDED verdict.
- Then, once the circular premise is dropped, the pass-2 re-check assumes it and the conclusion becomes VALID/entailed from it.
- The trace still does not pass, because the dropped premise is `no_premises` and blocks. So this changes recorded verdicts, not admission. That is why it is Medium.
- *Added:* `test_premise_ungrounded_in_pass_one_is_not_assumed_in_pass_two` (exact verdict list, with a solver stub that times out only on the pass-1 pool).

**#6. An escalated Design whose protocol states figures loses the figure flag (D01 survived).**
- SG17 reads the design from `current_experiment` *or* `hitl_payload`, but every test of the check uses a trace that verifies at 1.0. If the `hitl_payload` branch were dropped, a design whose trace fails would still escalate, but with the plain verification reason.
- On approve, the cost policy reads only the fields ($5 / 30 min), and the priced protocol would be merged. The human never sees the warning.
- *Added:* `test_an_escalated_design_with_protocol_figures_names_them_in_the_request`.

**#7. `protocol_figures` details are unpinned (A05, A06, A07 survived).**
- Every rejected example is written in the regex's own case. Dropping `re.IGNORECASE` (A05) lets "500 usd", "2 Hours" and "5 MIN" through, which weakens the SG17 guard in the same way as #1.
- A06 only shortens the reported figure ("$1" for "$1,200.50") in the human's reason. A07 adds false positives ("10 Eurofins primers").
- *Added:* `test_protocol_figures_ignore_case` (×3), `test_protocol_figures_report_the_whole_number`, `test_protocol_figures_need_a_whole_currency_word`.

**#8. The suite is still not load-tolerant (AUDIT-12 M1, cause now captured).**
- `test_graph_observability::test_concurrent_traced_runs_share_one_store[30]` gave 6 false kills in pass 1. The log shows `OperationalError('(sqlite3.OperationalError) database is locked')`. That is AUDIT-6 #2, which is still open, so the flake will remain until the store retries or serialises SQLite writes.
- `test_audit_6::test_shutdown_does_not_cancel_callers_on_other_loops`, a **strict xfail** (AUDIT-6 #7), **XPASSed** in 2 of 2 mutant runs at 3 parallel suites, which fails the suite. Its 0.3 s sleep assumes that both callers have submitted by then. Under load they have not, so `shutdown` runs first and no `CancelledError` is seen. The repro is timing-dependent in the *passing* direction. I had to deselect it for the harness.
- `test_audit_10::test_the_default_solver_pool_does_not_inflate_solver_ms_under_concurrency` (`perf`) failed for A07, which cannot affect it.
- *Suggested:* AUDIT-6 #7 should wait for both submissions (for example on the pool's work queue) before calling `shutdown`. Run `perf` in a separate, retried CI job (AUDIT-8 H1, still open).
- **Resolution (SG23).** The store's SQLite lock is fixed by a 30 s busy timeout and WAL (AUDIT-6 #2). The AUDIT-6 #7 repro now waits until both checks are in the pool and one is running before it calls `shutdown`, using a semaphore on the pool's `submit` and an event in the worker. It gives `['CancelledError', 'timeout']` on every run. The two AUDIT-14 #1 repros, which also XPASSed under load, now set the timeout (or the time before the cancel) to 3x the measured consistency check. So the deadline always falls inside the assumption check. The AUDIT-10 #1 perf test now alternates serial and pooled measurements within a round and keeps the best of up to 3 rounds; the 2x bound is unchanged. AUDIT-6 #3 now records the requested retry waits instead of wall-clock starts. A 20-run sweep of `tests/audit` under load found no other XPASS. `make test` was green 5 times in a row with a second suite running alongside.

**#9. Carried over:** coverage targets are still not enforced in `make test` or CI (AUDIT-8 M5). `state/checkpointer.py` is still at 80% with no `RedisCheckpointer` test (AUDIT-8 M6).

### Low

- **L1. The negated-claim name is unpinned (S04).** Renaming `claim!` to a declarable identifier survives. A trace that declared the same name would then fail every check with a duplicate-declaration parse error. That fails closed (checked: Z3 refuses the redeclaration), so the only cost is liveness. *Added:* `test_the_negated_claim_name_cannot_be_declared_by_a_step`, which checks the name against the formalizer's own rules.
- **L2. `hitl` timing fields are unpinned (K02, K03, G10, G11).** No SG18 test reads `latency_ms` or `started_at` on an outcome or a `hitl` event. *Added:* `test_hitl_outcome_times_the_resolution_including_the_policy` (a 50 ms policy) and `test_hitl_event_keeps_the_outcomes_timing`.
- **L3. Parameter-symbol hash length is unpinned (A11).** The SG17 test uses 3 names with one sanitized form, so a 1-hex-digit suffix passes it. *Added:* a 20-name test.
- **L4. `")("` reaches the parser (F12).** It is harmless, because Z3 rejects it, but the "rejected before any parse" contract was not pinned for depth −1. *Added:* `test_a_term_closed_before_it_opens_is_refused_before_any_parse`.
- **L5. The AUDIT-12 #1 repro has no precondition.** It asserts `hitl_pending is not None` but not `confidence == 1.0`. It would also pass if the trace stopped verifying for an unrelated reason. Its assertions are unchanged from round 3, so it was not weakened. The new #1 repro asserts the precondition.
- **L6. `cli._make_llm` (84-86) is still never run** (AUDIT-12 L3).

## Earlier repros after their xfail was removed

`git diff 7173a09 fee50eb -- tests/audit/`:
- **AUDIT-9 #1 ×2, #2 ×2, #3:** only the markers were removed. Each repro's assertions are unchanged.
  - One helper line changed in `_register`: it now skips registering vocabulary for a step whose parse yields more than one assertion, as the formalizer now does.
  - I ran the current file against the pre-SG15 `formalizer.py` and pre-SG16 `vericot.py`. All 12 cases fail, so the repros still discriminate.
  - The #1 and #3 repros fail on their own assertions. The global-parameters #2 repro fails with a `FakeLLMClient` KeyError instead, because the helper no longer anticipates the poisoned vocabulary. The repros are **not weakened**.
  - Checked for #3 above: under F10, the `declare-fun str.len` smuggle makes `(= (str.len w) (- 1))` satisfiable.
  - With the old helper and the pre-SG15 formalizer, `test_a_trace_cannot_change_z3_global_parameters` *passes* on the SG19 solver. So `reset_params()` alone defends against the `set-option` half, and `test_each_check_starts_from_default_global_parameters` pins it (S08, S09 killed).
- **AUDIT-10 #1, #5; AUDIT-11 #1, #3, #6, #7; AUDIT-12 #1, #2:** marker removal, plus "Today" → "Before SG17" comments. Assertions unchanged. They kill S01, S15, K01, G04, L01, L03, M02, A09, D02, S10, S14 and S17. **Not weakened.**
- **AUDIT-12 #3** (kill tests): unchanged, and still green.
- **SG19's edit to `test_audit_2::test_slow_checks_do_not_starve_the_default_executor`:** it now installs `SolverExecutor(n)` as the default solver executor, with `n = default_executor_workers()`, asyncio's default pool size. Without this, the 2-worker default pool could never run `n` slow checks at once, and `started == n` would fail. AUDIT-2 #1 meant that slow Z3 checks must not occupy asyncio's default executor, and the test still checks exactly that.
  - A regression that routes checks back onto the loop's default executor (S21, `run_in_executor(None, …)`) fills that pool with `n` slow checks and starves the probe. The test failed 2/2 under S21 and passed 2/2 on the clean tree.
  - What it no longer covers is the production pool size, which is not AUDIT-2's concern. S01 and S02 are covered by `test_audit_10` and `test_latency`.
  - Deriving `n` from asyncio also resolves AUDIT-12 L4.
  - **Verdict: not weakened.**

## Added tests (`tests/audit/test_audit_16.py`)

| Test | Kills / reproduces |
|---|---|
| test_protocol_figures_flags_hyphenated_durations_and_weeks ×4 (strict xfail) | AUDIT-16 #1 |
| test_hyphenated_duration_in_the_protocol_cannot_skip_the_duration_pause (strict xfail) | AUDIT-16 #1 |
| test_a_term_closed_before_it_opens_is_refused_before_any_parse | F12 |
| test_a_command_hidden_by_comments_is_refused_before_any_parse ×2 | F10 |
| test_the_comment_smuggled_command_really_runs_in_z3 | F10 precondition |
| test_premise_ungrounded_in_pass_one_is_not_assumed_in_pass_two | V08 |
| test_interrupt_is_resent_until_a_check_that_started_late_stops | S10, S11 |
| test_the_resend_thread_ends_when_its_check_ends | S12 |
| test_a_cancel_before_the_context_is_attached_refuses_the_check | S07, S13, S18 |
| test_the_negated_claim_name_cannot_be_declared_by_a_step | S04 |
| test_protocol_figures_ignore_case ×3 | A05 |
| test_protocol_figures_report_the_whole_number | A06 |
| test_protocol_figures_need_a_whole_currency_word | A07 |
| test_parameter_symbols_stay_distinct_for_many_names_with_one_sanitized_form | A11 |
| test_an_escalated_design_with_protocol_figures_names_them_in_the_request | D01 |
| test_hitl_outcome_times_the_resolution_including_the_policy | K02, K03 |
| test_hitl_event_keeps_the_outcomes_timing | G10, G11 |
| test_z3_spy_signals_only_after_the_interrupt_is_sent (strict xfail) | AUDIT-16 #2 |

Verification: `make lint typecheck test` gives ruff clean, 129 files formatted, mypy strict clean on 120 files, and **931 passed, 12 skipped, 32 xfailed** (base: 913 passed, 26 xfailed; +18 passing tests and 6 strict xfails).
