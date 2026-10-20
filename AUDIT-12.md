# AUDIT-12: Test coverage and test quality (round 3)

Branch `claude/audit-12`, based on `origin/claude/sg14-timing-tests` (`7b8c452`, SG11–SG14 merged). Independent audit; no production code was changed.

**Bottom line:** coverage is unchanged and high: 98% line and branch overall, 99% on `verification/`, 100% on `orchestrator/` and `agents/`. Of 67 one-line mutations in the code SG11–SG13 changed, 49 were killed by the existing suite and 18 survived (1 equivalent). Two SG14 regression mutants (S07 and inline judges) were also killed, so the SG14 wall-clock rewrites still detect what they claim. Every non-equivalent survivor is now killed by `tests/audit/test_audit_12.py`. The main problems:
- **The AUDIT-5 #2 repro was weakened** when its xfail was removed. It now passes only because its trace concludes the wrong claim and never reaches the solver. The same attack with the bound claim is still admitted (#1).
- **10 of the 13 reserved-symbol removals survived.** The SG11 test is parametrized over `RESERVED_SYMBOLS` itself, so deleting `=`, `=>`, `and`, `or`, `ite`, `true`, … from the set deletes its test case too. Each such deletion re-opens AUDIT-5 #1 (critical) for that symbol (#3).
- **The SG14 deterministic cancel/shutdown tests pass when the interrupt is lost.** Z3 drops an interrupt that arrives before `Solver.check()` starts. The tests' own spy signals in that window, so they sometimes take 20 s and still pass (#2).

## How this was measured

- **Coverage:** `uv run pytest --cov=autolab --cov-branch --cov-report=term-missing tests`: 756 passed, 13 skipped, 18 xfailed.
- **Mutations:** a harness in the scratchpad (not committed) ran each mutation in a fresh copy of the worktree, with `PYTHONPATH` pointed at the copy (checked by importing `autolab` from the copy before each run):
  - Each mutation is one exact-match replacement, asserted to occur exactly once.
  - Pass 1: `pytest -x -m "not perf" tests`, with the per-test `pytest-timeout` (60 s) from `pyproject.toml`.
  - Pass 2: every survivor, and every kill whose failing test could not plausibly be affected, re-ran the full suite *including* `perf`, without `-x`. The table shows the pass-2 result.
  - The worktree was never edited by the harness. `tests/audit/test_audit_12.py` was excluded (`--ignore`) from the "existing suite" runs.
  - Then each survivor was re-applied against `tests/audit/test_audit_12.py` alone to confirm the new test kills it.
- **Load:** mutants ran 2 at a time on 4 cores, while other agents' suites were also running on the machine. That is where the false kills under M1 come from.

## Coverage

| Module | Stmts | Miss | Branch | BrPart | Cover | Missing |
|---|---:|---:|---:|---:|---:|---|
| agents/analysis.py | 34 | 0 | 2 | 0 | 100% | |
| agents/base.py | 49 | 0 | 6 | 0 | 100% | |
| agents/common.py | 53 | 0 | 4 | 0 | 100% | |
| agents/design.py | 32 | 0 | 2 | 0 | 100% | |
| agents/execute.py | 29 | 0 | 2 | 0 | 100% | |
| agents/research.py | 29 | 0 | 0 | 0 | 100% | |
| agents/verification_monitor.py | 36 | 0 | 16 | 0 | 100% | |
| cli.py | 74 | 6 | 20 | 0 | 91% | 75, 84-86, 114-116 |
| config.py | 29 | 0 | 0 | 0 | 100% | |
| monitoring/dashboard.py | 39 | 0 | 6 | 0 | 100% | |
| monitoring/events.py | 22 | 0 | 0 | 0 | 100% | |
| monitoring/metrics.py | 30 | 0 | 4 | 0 | 100% | |
| monitoring/tracer.py | 51 | 0 | 28 | 1 | 99% | 77->76 |
| orchestrator/checkpoints.py | 76 | 0 | 22 | 0 | 100% | |
| orchestrator/graph.py | 68 | 0 | 6 | 0 | 100% | |
| orchestrator/recovery.py | 73 | 0 | 16 | 0 | 100% | |
| orchestrator/routing.py | 23 | 0 | 4 | 0 | 100% | |
| state/checkpointer.py | 95 | 11 | 38 | 14 | **80%** | 55-56, 60, 63, 66, 69-73, 116->118 … 148->150, 162 |
| state/models.py | 140 | 0 | 18 | 0 | 100% | |
| state/schema.py | 21 | 0 | 0 | 0 | 100% | |
| state/store.py | 119 | 0 | 14 | 0 | 100% | |
| tools/lab_hardware/simulator.py | 43 | 1 | 12 | 0 | 98% | 89 |
| tools/literature.py | 70 | 4 | 14 | 4 | 90% | 81, 107, 124, 130 |
| verification/confidence.py | 33 | 0 | 12 | 0 | 100% | |
| verification/faithfulness.py | 21 | 0 | 2 | 0 | 100% | |
| verification/formalizer.py | 83 | 0 | 24 | 0 | 100% | |
| verification/llm.py | 69 | 1 | 16 | 2 | 96% | 189, 219->218 |
| verification/premise_grounder.py | 17 | 0 | 4 | 0 | 100% | |
| verification/solver.py | 151 | 1 | 34 | 2 | 98% | 180, 283->exit |
| verification/vericot.py | 126 | 1 | 48 | 1 | 99% | 242 |
| **TOTAL** | 1747 | 25 | 374 | 24 | **98%** | |

| Package | Cover (line + branch) | Target |
|---|---:|---|
| verification/ | 99% | ≥90% ✔ |
| orchestrator/ | 100% | ≥70% ✔ |
| agents/ | 100% | ≥70% ✔ |
| state/ | 94% | ≥70% ✔ |
| monitoring/ | 99% | ≥70% ✔ |
| tools/ | 94% | ≥70% ✔ |
| cli.py | 91% | ≥70% ✔ |

- Every line and branch that SG11–SG13 added is covered.
- `cli.py` 75 and 114-116 are run by the CLI tests (they assert on the output those lines produce). As in rounds 1 and 2, this is a tracing artifact of running `cli.main` on a worker thread. Lines 84-86 (`_make_llm`) are really untested: every CLI test patches it.
- The `state/checkpointer.py` gap (round-1 M4) and the `verification/` gaps from AUDIT-8 are unchanged.

## Mutation table

"Killed by" names the first failing test of the existing suite. Kills marked † were first attributed to an unrelated test that flaked under load (M1); the pass-2 full run without `-x` shows the real result.

| ID | Mutation | Result | Killed by |
|---|---|---|---|
| F01 | reserved-symbol check removed | killed | test_audit_5::test_shadowed_gt_hides_a_premise_that_contradicts_the_measurement |
| F02 | `>` not reserved | killed | same |
| F03 | `<` not reserved | killed | test_audit_5::test_shadowed_lt_in_the_ignored_final_step_defeats_the_contradiction_trigger |
| F04 | `not` not reserved | killed | test_audit_5::test_shadowed_not_lets_a_restated_payload_premise_prove_itself |
| **F05** | `=` not reserved | **survived**† | test_audit_12::test_each_core_builtin_is_reserved_by_name[=] |
| **F06** | `=>` not reserved | **survived** | …[=>] |
| **F07** | `and` not reserved | **survived** | …[and] |
| **F08** | `or` not reserved | **survived**† | …[or] |
| **F09** | `ite` not reserved | **survived**† | …[ite] |
| **F10** | `distinct` not reserved | **survived**† | …[distinct] |
| **F11** | `+` not reserved | **survived**† | …[+] |
| **F12** | `true` not reserved | **survived** | …[true] |
| **F13** | `implies` not reserved | **survived**† | …[implies] |
| **F14** | `to_real` not reserved | **survived**† | …[to_real] |
| F15 | parse context is `z3.main_ctx()` | killed | test_audit_6::test_verifies_on_several_threads_do_not_crash_the_interpreter, test_formalizer::test_parse_context_is_per_thread_and_never_the_global_one |
| F16 | `formalize` parses without `ctx=` | killed (by a race only) | test_audit_6::test_verifies_on_several_threads_do_not_crash_the_interpreter; now also test_audit_12::test_formalize_parses_in_the_per_thread_context |
| F17 | parse context shared across threads | killed | test_audit_6 stress test, test_formalizer::test_parse_context_is_per_thread… |
| F18 | fresh context per call (perf only) | killed | test_formalizer::test_parse_context_is_per_thread_and_never_the_global_one |
| F19 | vocabulary extended before the parse check | killed | test_audit_1::test_conclusion_with_no_premises_is_not_admitted |
| D01 | no cost fact | killed | test_design::test_design_facts_cover_cost_duration_and_numeric_parameters_only |
| D02 | no duration fact | killed | same |
| D03 | no parameter facts | killed | same |
| D04 | bool parameters treated as numeric | killed | same |
| D05 | claim omits cost | killed | test_design::test_design_claim_names_the_protocol_and_every_field |
| D06 | claim omits parameters | killed | same |
| D07 | claim omits units | killed | same |
| D08 | parameter symbols unprefixed | killed | test_design::test_design_facts_cover… |
| D09 | fact symbol differs from the claim's | killed | same |
| D10 | claim is the bare protocol (pre-SG12) | killed | test_audit_5::test_design_cost_fields_that_contradict_the_verified_protocol_skip_the_pause |
| D11 | negative reals written as `-x` | killed | test_common::test_measurement_facts_are_parseable_real_equalities |
| D12 | numeric parameters in the claim not normalised | killed | test_design::test_design_claim_names_the_protocol_and_every_field |
| G01 | Design facts not injected | killed | test_audit_4::test_human_retry_restores_the_automatic_retry_budget (+ test_design) |
| G02 | bound claim is the protocol | killed | same |
| G03 | prompt asks for the protocol | killed | test_design::test_design_trace_prompt_names_the_required_conclusion |
| G04 | first hypothesis targeted | killed | test_graph::test_consistent_hypothesis_does_not_pause |
| H01 | policy also re-run on `error` requests | survived (**equivalent**) | an `error` request has `hitl_payload=None`, so the policy sees no value and returns None |
| **H02** | missing policy not guarded (`and policy` dropped) | **survived** | test_audit_12::test_approving_an_agent_without_a_policy_merges_its_payload |
| H03 | re-pause condition inverted | killed | test_audit_5::test_approving_a_verification_escalation_still_applies_the_cost_trigger |
| H04 | re-pause keeps `trigger=verification` | killed | same |
| H05 | re-pause drops the withheld payload | killed | test_hitl_node::test_approved_verification_escalation_pauses_again_when_policy_fires |
| H06 | re-pause ends the run | killed | test_audit_5 #3 |
| H07 | `merged_policy` reads the first item | killed | test_hitl_node::test_merged_policy_reads_the_last_list_item_and_skips_a_missing_key |
| H08 | empty list not guarded | killed | same |
| H09 | missing key not skipped | killed | same |
| H10 | `cleared` overrides the re-pause | killed | test_audit_5 #3 |
| H11 | policies ignored | killed | test_audit_5 #3 |
| H12 | cost boundary `>=` | killed | test_recovery::test_cost_trigger_is_strictly_above_thresholds |
| H13 | duration boundary `>=` | killed | same |
| H14 | policy sees an empty payload | killed | test_audit_5 #3 |
| H15 | approve merges the request payload, not the withheld one | killed | test_audit_5 #3 |
| **P01** | no Research policy on approve | **survived** | test_audit_12::test_approving_a_research_escalation_still_runs_the_contradiction_trigger |
| P02 | no Design policy on approve | killed | test_audit_5 #3 |
| **P03** | Research policy reads the wrong key | **survived**† | test_audit_12::test_approving_a_research_escalation_still_runs_the_contradiction_trigger |
| P04 | Design policy reads the wrong key | killed | test_audit_5 #3 |
| **P05** | `hitl` not listed as its own destination | **survived** | test_audit_12::test_compiled_graph_draws_the_hitl_self_edge |
| **C01** | thread id not flushed before the run | **survived** | test_audit_12::test_thread_id_is_flushed_before_the_graph_runs |
| C02 | thread id not printed | killed | test_cli::test_run_prints_the_thread_id_before_a_crash |
| C03 | interrupt not detected | killed | test_cli_resume::test_run_then_resume_approve_completes |
| C04 | paused run without an action not refused | killed | test_cli::test_resume_of_a_paused_run_requires_an_action |
| C05 | resolution not sent | killed | test_cli_resume::test_run_then_resume_approve_completes |
| C06 | finished run treated as crashed | killed | test_cli_resume::test_resume_abort_ends_and_a_second_resume_is_rejected |
| C07 | finished/unknown run not refused | killed | same |
| **C08** | ignored action not reported on stderr | **survived** | test_audit_12::test_printed_thread_id_is_the_one_the_run_checkpoints_under |
| C09 | action required again (`nargs` dropped) | killed | test_cli::test_resume_without_action_continues_a_crashed_run |
| C10 | resume path skips the state checks | killed | test_cli_resume::test_run_then_resume_approve_completes |
| **C11** | printed id differs from the run's id | **survived** | test_audit_12::test_printed_thread_id_is_the_one_the_run_checkpoints_under |
| C12 | state read from the wrong thread | killed | test_audit_7::test_run_killed_by_a_transient_outage_can_be_resumed |
| T01 | SG14 check, S07: checks on asyncio's default executor | killed | test_audit_2::test_slow_checks_do_not_starve_the_default_executor, test_solver_executor::test_callers_beyond_the_worker_count_wait_outside_the_pool (+2) |
| T02 | SG14 check: judges awaited inline | killed | test_audit_2::test_ten_step_trace_overlaps_its_llm_calls, test_solver_executor::test_judge_runs_while_the_next_step_is_formalized (+2) |

Totals: 69 mutations (67 over the SG11–SG13 code, plus 2 SG14 regression checks). 51 were killed by the existing suite and 18 survived, 1 of them equivalent (H01). All 17 non-equivalent survivors are killed by `tests/audit/test_audit_12.py`, re-checked by re-applying each mutation (about 7 s per mutant).

## Findings

### Critical

None.

### High

**#1. The AUDIT-5 #2 repro was weakened when its xfail was removed (repro: `test_design_concluding_the_bound_claim_cannot_hide_its_protocol_cost`, strict xfail "AUDIT-12 #1").**
- *What changed:* SG12 replaced `assert update["confidence"] == 1.0` with `< 1.0`. The stated reason is accurate: the repro's trace concludes the bare protocol, which is no longer the payload claim, so the verifier marks every step `no_premises` ("trace does not conclude payload claim") and scores 0.0 without formalizing anything. The `hitl_pending` and withheld-payload assertions pass for that reason only.
- *Why that is not sound:* the original `== 1.0` assertion was the repro's precondition. It established that an *honest, fully verified* trace about a $500 / 240 min protocol is what slips past the $5 / 30 min fields. After the change, the repro tests the unbound-claim gate, which existed before SG12 (then bound to `protocol`). It no longer exercises the attack. The model is told to conclude `design_claim(design)`, and a trace that does so is not covered by any test.
- *Repro:* the same reply and the same two premises (`minutes = 240`, `usd = 500`, `=> chosen`), now concluding `design_claim(design)` with `facts=design_facts(design)` registered. Today it verifies at 1.0, `hitl_pending` is None and the $5 design is merged with no cost pause. That is exactly AUDIT-5 #2's impact. SG12 lists this gap under Open ("formalizes the protocol's own figures under unrelated symbols"), but the flipped repro reads as if #2 were closed.
- *Suggested:* restore a passing-precondition assertion in AUDIT-5 #2 (or keep this repro next to it). Reopen #2 as partially fixed: SG12 closed the "premise states another value for a bound symbol" case and the "conclude something else" case, but not the case where unrelated symbols carry the figures. One option is the partial fix AUDIT-5 suggested: run the policy trigger on any cost or duration figure that the verified trace formalizes. Another is to require the conclusion formula to mention every bound fact symbol.

- *Resolution (SG17):* the protocol text may not state its own cost or duration. `common.protocol_figures` flags currency amounts (a symbol or code next to a number) and durations (a number plus s, sec, min, h, hrs, hours, days). `DesignAgent.__call__` turns any match into a `verification` HITL request, withholds the design in `hitl_payload`, and does not admit it, even at confidence 1.0. The Design prompt tells the model to keep these figures out of the protocol. This is a rejection check: no value is read from the text into state (CLAUDE.md §9). The repro is flipped and its assertions are unchanged. Limit: figures spelled in words ("four hours") pass the check.

**#2. The SG14 deterministic cancel/shutdown tests pass when the interrupt is lost, and the interrupt is lost in production too (repro: `test_cancel_between_the_two_z3_checks_still_stops_the_second`, strict xfail "AUDIT-12 #2").**
- *Production race:* `_Interrupt.request()` calls `ctx.interrupt()` on the attached context. Z3 ignores an interrupt issued while no `Solver.check()` is running. Checked directly: `ctx.interrupt()` then `check()` runs to its full timeout (`unknown`, `reason_unknown == "timeout"`, 3.0 s). So a cancel or `shutdown()` that lands after `attach` but outside a check is dropped. That covers parsing, solver setup, and the gap between the consistency check and the entailment check. The worker stays busy until the Z3 timeout. The repro cancels in that gap and measures the entailment check: 3.0 s against a 3 s timeout (it should return at once).
- *Why the tests miss it:* `test_cancelling_a_caller_interrupts_its_z3_context` and `test_shutdown_interrupts_the_running_z3_context` (SG14's replacements for the wall-clock guards on S02/S04/S09) set their `entailing` event *before* calling the real `Solver.check`, which is the drop window. They assert only that `Context.interrupt()` was called, plus a final outcome (`UNGROUNDED/timeout`) that a natural timeout also produces. Running the pair 15 times: 12 runs took 0.7–1.3 s, 2 took 20 s (one lost interrupt, `_HANG_S`) and 1 took 41 s (both lost). Every run passed. "Cancel frees the worker before the Z3 timeout" is still guarded only by the wall-clock perf tests (`test_cancelled_verify_interrupts_its_check`, `test_shutdown_interrupts_running_checks`, `< 0.5 s`), which miss the window because they cancel 0.2 s into a running check.
- *Side effect:* at 20–40 s per lost interrupt, the pair runs close to the 60 s `pytest-timeout` under load. `test_cancelling_a_caller_interrupts_its_z3_context` failed in 2 unrelated mutant runs (F05, F14 in pass 1). Neither failure reproduced on re-run.
- *Suggested:* in `_Interrupt`, keep `_requested` and have the solver check it (or re-arm the interrupt) immediately before each `solver.check()`, or use a Z3 `rlimit`/cancel flag that survives. In the tests, assert that the interrupted check returns quickly (`details == "interrupted"`, or a check far shorter than its timeout), not just that `interrupt()` was called.

**#3. 10 of 13 reserved-symbol deletions survived (F05–F14). Each re-opens AUDIT-5 #1 (critical) for that symbol.**
- `test_merge_rejects_reserved_symbols` is parametrized over `sorted(RESERVED_SYMBOLS)`, so removing a name from the set removes its test case. Only `>`, `<` and `not` are pinned, by the three AUDIT-5 #1 repros. A trace could then redeclare `=`, `=>`, `and`, `or`, `ite`, `distinct`, `+`, `true`, `implies` or `to_real` and shadow the builtin in every check, which is the AUDIT-5 #1 attack.
- *Added:* `test_each_core_builtin_is_reserved_by_name` (35 cases), with the core, arithmetic, array, Z3-extension and binder names written out independently of the set. Kills F05–F14.

### Medium

**M1. The suite is still not load-tolerant (SG14's claim holds only partly).** With 2 mutant suites plus other agents' test runs on 4 cores:
- `test_graph_observability::test_concurrent_traced_runs_share_one_store[30]` (not `perf`, no timing assertion) failed in 6 mutant runs whose mutation cannot affect it (F08, F09, F10, F11, F13 in pass 1; F14 in pass 2). All 6 passed on re-run, and the test passed 6 of 6 times in isolation. Cause not captured (pass-1 logs were not kept); the likely suspects are SQLite `database is locked` (AUDIT-6 #2) or a premise check timing out under contention.
- `test_cancelling_a_caller_interrupts_its_z3_context` failed falsely twice (see #2).
- `test_solver_executor::test_judges_overlap_but_formalization_stays_ordered` (`perf`) failed falsely once (P03, pass 2).
- `test_default_pool_stays_responsive_under_fifty_slow_checks` (`perf`) failed once in 12 isolated runs of `test_solver_executor.py` under that load: `worst_ms` 2062 ≥ 1700. That is the absolute bound SG14 kept.
- The flakes are intermittent. Over about 85 full-suite mutant runs there were 9 false failures, and two concurrent runs of a no-op mutant were both green (756 passed each).
- *Suggested:* run `perf` in a separate, retried CI job (AUDIT-8 H1, still open), and find the cause of the concurrent-store flake.

**M2. The Research contradiction policy on `approve` is not tested end to end (P01, P03 survived).** SG12 wires `merged_policy(contradiction, "hypotheses")` into the `hitl` node, but only the Design entry is exercised (AUDIT-5 #3). If that entry were dropped, or read the wrong key, approving a Research verification escalation would merge a hypothesis that contradicts the state without the 7c pause, and the suite would stay green. *Added:* `test_approving_a_research_escalation_still_runs_the_contradiction_trigger`.

**M3. The CLI tests patch `uuid4` to a constant, so the printed id is never checked against the run's id (C11 survived).** SG13's core promise is "the id printed before the crash resumes the run". A run that checkpoints under a different id than it prints would pass every test. The note printed when `resume` ignores an action (C08) and the flush before the run (C01) are also untested. *Added:* `test_printed_thread_id_is_the_one_the_run_checkpoints_under` (distinct ids per call; resumes with the printed id and checks the stderr note), and `test_thread_id_is_flushed_before_the_graph_runs`.

**M4. Approving a verification escalation for an agent with no policy is untested (H02 survived).** Without the `and policy` guard, approving an Execute or Analysis escalation raises `TypeError: 'NoneType' object is not callable` inside the `hitl` node. No test approves either. *Added:* `test_approving_an_agent_without_a_policy_merges_its_payload`.

**M5. The per-thread parse context of `Formalizer.formalize` is guarded only by a race.** F16 (parse without `ctx=`) was killed only by `test_audit_6::test_verifies_on_several_threads_do_not_crash_the_interpreter`, a subprocess stress test that needs the SIGSEGV to happen. It did fire in all 3 runs here, but it is probabilistic, and `test_parse_context_is_per_thread_and_never_the_global_one` checks only the helper, not that `formalize` uses it. *Added:* `test_formalize_parses_in_the_per_thread_context` (spy on `z3.parse_smt2_string`).

**M6. Carried over:** coverage targets are still not enforced in `make test`/CI (AUDIT-8 M5). `state/checkpointer.py` is still at 80% with no `RedisCheckpointer` test (AUDIT-8 M6).

### Low

- **L1. `hitl` self-edge (P05 survived).** Dropping `"hitl"` from the node's `destinations` does not change routing, but `get_graph()` no longer draws the re-pause edge an auditor would look for. *Added:* `test_compiled_graph_draws_the_hitl_self_edge`.
- **L2. The AUDIT-7 #1 repro asserts only that `resume` does not exit.** It does not check that the run completes; `tests/unit/test_cli.py` covers completion. The assertions are unchanged from round 2 (only the marker was removed), so it was not weakened.
- **L3. `_make_llm` is never run** (`cli.py` 84-86): every CLI test patches it.
- **L4. SG14's starvation test assumes `SOLVER_WORKERS` equals asyncio's default pool size** (SG14 Open). With fewer solver workers than default-pool threads, S07 would leave free default threads and survive.

## Earlier repros after their xfail was removed

`git diff` of each flipped repro against the commit that added it:
- **AUDIT-5 #1 ×3, AUDIT-5 #3, AUDIT-6 #1, AUDIT-7 #1:** marker (and "Today"→"Before SG12" comment) removal only, plus the `_fail_expensive_design` fixture now concluding `design_claim` with `design_facts`, which a passing Design trace needs. Assertions unchanged. These repros kill F01–F04, F15–F17, H03–H15, P02, P04 and C12. **Not weakened.**
- **AUDIT-5 #2:** **weakened** (#1 above). It still kills D10, but only through the unbound-claim gate.
- **AUDIT-2 #1 (SG14 rewrite):** the 0.25 s probe bound became "no slow check finished before the probe ran", with every slow check counted as started. T01 (S07) still fails it. **Not weakened**; it is still wall-clock (`perf`) and assumes `SOLVER_WORKERS` matches the default pool (L4).
- **AUDIT-2 #5 (SG14 rewrite):** now `wall < 0.8 × serial baseline` instead of an absolute bound. T02 (inline judges) fails it. **Not weakened.**
- **AUDIT-2 timeout-under-concurrency:** the absolute bound was loosened from 1.0 s to 2.0 s, and `worst_ms < 0.6 × wall` was added. With 3 rounds of checks, the absolute bound alone no longer catches S05 (1.5 s < 2.0 s); the relative bound and `test_run_time_excludes_waiting_for_a_worker` do. **Acceptable.**

## Added tests (`tests/audit/test_audit_12.py`)

| Test | Kills / reproduces |
|---|---|
| test_each_core_builtin_is_reserved_by_name ×35 | F05–F14 |
| test_formalize_parses_in_the_per_thread_context | F16 (deterministically), F15, F18 |
| test_approving_an_agent_without_a_policy_merges_its_payload | H02 |
| test_approving_a_research_escalation_still_runs_the_contradiction_trigger | P01, P03 |
| test_compiled_graph_draws_the_hitl_self_edge | P05 |
| test_printed_thread_id_is_the_one_the_run_checkpoints_under | C11, C08 |
| test_thread_id_is_flushed_before_the_graph_runs | C01 |
| test_design_concluding_the_bound_claim_cannot_hide_its_protocol_cost (strict xfail) | AUDIT-12 #1 |
| test_cancel_between_the_two_z3_checks_still_stops_the_second (strict xfail) | AUDIT-12 #2 |
