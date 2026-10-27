# AUDIT-8: Test coverage and test quality (round 2)

Branch `claude/audit-8`, based on `origin/claude/sg9-entity-identity` (`8a1c1de`, SG7–SG10 merged). Independent audit; no production code was changed.

**Bottom line:** coverage stays high: 98% line and branch overall, 99% on `verification/`. Of 56 one-line mutations in the code that SG7–SG10 changed, 48 were killed by the existing suite. **None of the 8 survivors weakens verification soundness**, and every survivor is now killed by `tests/audit/test_audit_8.py`. The main problem is the test harness, not coverage:
- The wall-clock tests that SG10 relies on fail falsely when the CPU is busy.
- There is no per-test timeout, so one solver mutant hangs the suite instead of failing it.

## How this was measured

- **Coverage:** `uv run pytest --cov=autolab --cov-branch --cov-report=term-missing tests`, giving 616 passed, 12 skipped (live-gated) and 1 xfailed (AUDIT-3 #6).
- **Mutations:** a harness script (in the scratchpad, not committed) ran each mutation in a fresh copy of the worktree, with `PYTHONPATH` pointed at the copy:
  - It applied exact-match replacements, each asserted to occur exactly once.
  - It ran `pytest -x tests` and recorded the first failure.
  - It deleted the copy.
  - The worktree itself was never edited. `git status` was clean before the new test file was added.
- **Parallel runs and re-runs:** mutants ran 2 at a time on 4 cores. Every kill attributed to a wall-clock test was re-run serially without `-x`; the table shows the serial result. The new tests were then re-checked against each survivor (all 9 rows below are killed).
- **Flakiness:** the full suite ran 3 times on the clean tree:
  - Run 1 (coverage): 616 passed in 100 s.
  - Run 2 (`--durations`): 616 passed in 78 s.
  - Run 3 (`make test`, with the new tests): 630 passed in 64 s.
  - A baseline copy run under parallel load also passed (616 passed).
  - All runs were green on an idle machine. The false kills described under H1 appeared only under load.

## Coverage

| Module | Stmts | Miss | Branch | BrPart | Cover | Missing |
|---|---:|---:|---:|---:|---:|---|
| agents/analysis.py | 34 | 0 | 2 | 0 | 100% | |
| agents/base.py | 49 | 0 | 6 | 0 | 100% | |
| agents/common.py | 43 | 0 | 4 | 0 | 100% | |
| agents/design.py | 32 | 0 | 2 | 0 | 100% | |
| agents/execute.py | 29 | 0 | 2 | 0 | 100% | |
| agents/research.py | 29 | 0 | 0 | 0 | 100% | |
| agents/verification_monitor.py | 36 | 0 | 16 | 0 | 100% | |
| cli.py | 62 | 5 | 12 | 0 | 93% | 66, 79-81, 97-98 |
| config.py | 29 | 0 | 0 | 0 | 100% | |
| monitoring/dashboard.py | 39 | 0 | 6 | 0 | 100% | |
| monitoring/events.py | 22 | 0 | 0 | 0 | 100% | |
| monitoring/metrics.py | 30 | 0 | 4 | 0 | 100% | |
| monitoring/tracer.py | 51 | 0 | 28 | 1 | 99% | 77->76 |
| orchestrator/checkpoints.py | 55 | 0 | 16 | 0 | 100% | |
| orchestrator/graph.py | 65 | 0 | 6 | 0 | 100% | |
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
| verification/formalizer.py | 72 | 0 | 20 | 0 | 100% | |
| verification/llm.py | 69 | 1 | 16 | 2 | 96% | 189, 219->218 |
| verification/premise_grounder.py | 17 | 0 | 4 | 0 | 100% | |
| verification/solver.py | 151 | 1 | 34 | 2 | 98% | 180, 283->exit |
| verification/vericot.py | 126 | 1 | 48 | 1 | 99% | 242 |
| **TOTAL** | 1690 | 24 | 356 | 24 | **98%** | |

| Package | Cover (line + branch) | Target |
|---|---:|---|
| verification/ | 99% | ≥90% ✔ |
| orchestrator/ | 100% | ≥70% ✔ |
| agents/ | 100% | ≥70% ✔ |
| state/ | 94% | ≥70% ✔ |
| monitoring/ | 99% | ≥70% ✔ |
| tools/ | 94% | ≥70% ✔ |
| cli.py | 93% | ≥70% ✔ |

Every line and branch that SG7–SG10 added is covered except these three:
- **`vericot.py:242`:** the `_payload_overrides` early return when the conclusion is not an `SmtFormula`. It is unreachable, because the caller only calls it when the last verdict is VALID.
- **`solver.py:180`:** `_wrap_assert` on a body that already starts with `(assert`. This line was already uncovered in round 1.
- **`solver.py:283->exit`:** `shutdown_default_executor()` when no default executor exists.

The `state/checkpointer.py` gap from round 1 is unchanged (round-1 M4).

## Mutation table

Kills marked † were first attributed to an unrelated wall-clock test in the parallel run. The real killer comes from the serial re-run without `-x`.

| ID | Mutation | Result | Killed by |
|---|---|---|---|
| V01 | circular check ignores a premise that restates the payload text | killed | test_payload_proof::test_premise_restating_the_claim_text_is_circular_whatever_its_formula |
| V02 | inconclusive "premise alone entails" check fails open | killed | test_payload_proof::test_inconclusive_circularity_check_fails_closed |
| **V03** | circularity scan also strips VALID inference steps | **survived** | test_audit_8::test_bound_trace_with_an_entailed_intermediate_inference_passes |
| V04 | circular premises overridden, but the trace is not re-checked | killed | test_payload_proof::test_premise_entailing_the_conclusion_alone_is_circular |
| V05 | inconclusive tautology check fails open | killed | test_payload_proof::test_inconclusive_tautology_check_fails_closed |
| V06 | premise consistency ignores `context.facts` | killed | test_audit_1::test_premise_contradicting_a_measurement_is_not_valid |
| V07 | premise consistency ignores prior VALID steps | killed | test_audit_1::test_contradiction_trigger_fires_when_reformalization_fails |
| **V08** | score no longer requires a VALID final step for a bound trace | **survived** | test_audit_8::test_unproven_bound_conclusion_is_capped_in_the_score |
| V09 | verifier ignores an injected `solver_executor` | killed† | test_solver_executor::test_solver_ms_excludes_queueing, ::test_shutdown_interrupts_running_checks (timing) |
| C01 | cap margin 0.01 → 0 | killed† | test_confidence::test_any_invalid_caps_score_below_both_thresholds (+7) |
| C02 | cap derived from the pass threshold only | killed† | test_audit_3::test_refuted_trace_never_passes_under_configured_pass_threshold (+4) |
| C03 | `require_final_valid` caps only an INVALID final step | killed | test_confidence::test_require_final_valid_caps_an_unproven_conclusion |
| M01 | monitor: score shortcut before the blocking check | killed | test_verification_monitor::test_unproven_final_step_never_passes_and_is_retried |
| M02 | monitor: `retry_rework=False` no longer routes by score | killed | same |
| M03 | monitor: a `no_premises` step no longer blocks | killed | test_verification_monitor::test_blocking_reason_names_the_first_blocking_step |
| M04 | monitor: final step need only be not INVALID | killed | test_verification_monitor::test_unproven_final_step_never_passes_and_is_retried |
| **B01** | agent gate is score-only (pre-SG7), not `monitor.decide` | **survived** | test_audit_8::test_agent_withholds_a_refuted_trace_whose_capped_score_clears_its_gate |
| **F01** | `;` allowed inside a declaration form | **survived** | test_audit_8::test_comment_inside_a_declaration_form_is_rejected ×2 |
| F02 | trailing command after the declaration form allowed | killed | test_formalizer::test_merge_rejects_anything_but_one_declaration_form |
| F03 | `define-fun` accepted as a declaration head | killed | test_formalizer::test_merge_rejects_malformed_declarations |
| F04 | unbalanced declaration accepted | killed | test_formalizer::test_merge_rejects_anything_but_one_declaration_form |
| F05 | refusal (`RuntimeError`) not mapped to `FormalizationError` | killed | test_audit_3::test_formalizer_turns_a_refusal_into_formalization_error |
| **S01** | executor semaphore unbounded (no backpressure) | **survived** | test_audit_8::test_at_most_workers_checks_are_ever_submitted_to_the_pool |
| S02 | cancelling a caller does not interrupt its check | killed | test_solver_executor::test_cancelled_verify_interrupts_its_check (timing) |
| S03 | `attach` ignores an interrupt requested before the check | killed | test_solver_executor::test_interrupt_before_the_check_starts |
| S04 | `shutdown` does not interrupt running checks | killed | test_solver_executor::test_shutdown_interrupts_running_checks (timing) |
| S05 | `solver_ms` timed on the caller (includes queueing) | killed† | test_solver_executor::test_solver_ms_excludes_queueing, test_backpressure_bounds_work_in_the_pool, +2 (timing) |
| S06 | entailment check gets the full timeout, not the remaining budget | killed | test_audit_2::test_both_checks_share_one_deadline |
| S07 | checks run on asyncio's default executor (pre-SG10) | killed | test_audit_2::test_slow_checks_do_not_starve_the_default_executor |
| **S08** | `_remaining_ms` floor 1 → 0 (Z3: 0 = no limit) | **hang only** (suite > 900 s, serial, with and without `-x`) | test_audit_8::test_exhausted_deadline_never_becomes_an_unlimited_z3_timeout |
| S09 | worker never registers its interrupt handle | killed | test_solver_executor::test_cancelled_verify_interrupts_its_check (timing) |
| R01 | 409 not transient | killed | test_retry_policy::test_transient_status_errors_are_retried[409] |
| R02 | 500 not transient | killed | test_retry_policy::…[500] |
| R03 | `x-should-retry` ignored | killed | test_retry_policy::test_x_should_retry_header_overrides_the_status |
| R04 | negative `retry-after` not clamped | killed | test_retry_policy::test_retry_after_parsing |
| R05 | `retry-after` preferred over `retry-after-ms` | killed | test_retry_policy::test_retry_after_parsing |
| R06 | no jitter | killed | test_audit_2::test_concurrent_retries_are_jittered |
| R07 | `retry-after` ignored | killed | test_audit_2::test_rate_limit_retry_after_is_honoured |
| **R08** | backoff does not grow (`delay *= 1.0`) | **survived** | test_audit_8::test_backoff_range_doubles_each_attempt |
| R09 | `RuntimeError` not escalated | killed | test_audit_3::test_llm_refusal_pauses_at_hitl_instead_of_crashing_the_graph |
| R10 | `ValueError` not escalated | killed | test_graph_observability::test_metrics_record_permanent_errors_as_escalations |
| R11 | transient API errors escalated | killed | test_retry_policy::test_exhausted_transient_api_errors_propagate |
| R12 | catch list widened to `Exception` | killed | test_graph::test_transient_error_exhausting_retries_propagates |
| R13 | SDK keeps `max_retries=2` | killed | test_audit_2::test_one_logical_call_makes_at_most_three_http_attempts |
| R14 | `TimeoutError` not transient | killed | test_recovery::test_retrying_llm_client_retries_transient_errors_only |
| K01 | `HYPOTHESIS_REPLY` exposes `created_at` | killed | test_audit_3::test_structured_output_schema_does_not_let_the_model_pick_ids |
| K02 | `REPORT_REPLY` exposes `created_at` | killed | same |
| K03 | reply schemas forbid extra keys | killed | test_audit_1::test_circular_premise_restating_payload_is_not_admitted (and test_identity) |
| K04 | design targets the first hypothesis, not the latest | killed† | test_graph::test_consistent_hypothesis_does_not_pause |
| **K05** | analysis `result_id` taken from the first result | **survived** | test_audit_8::test_analysis_report_points_at_the_latest_result |
| K06 | `HYPOTHESIS_REPLY` exposes `question` | killed | test_audit_1::test_circular_premise_restating_payload_is_not_admitted |
| D01 | entity key `id` only (pre-SG9 schema and ON CONFLICT) | killed | test_audit_2::test_same_entity_id_in_two_runs_stays_in_both |
| D02 | run upsert overwrites `research_question` | killed | test_store::test_create_run_with_given_id_and_ensure_run_is_idempotent |
| D03 | entity upsert `DO NOTHING` | killed† | test_store::test_rewriting_an_id_in_one_run_leaves_the_other_run_untouched |
| D04 | entity upsert does not update `payload` | killed | same |
| D05 | `VerificationResult` keyed by a fresh uuid, not `trace_id` | killed | test_resume::test_traces_across_restart_are_persisted_once |
| **G01** | `build_graph` does not pass Settings thresholds to `Verifier` | **survived** | test_audit_8::test_graph_caps_refuted_traces_below_the_configured_thresholds |
| CP01 | contradiction trigger fails open on UNGROUNDED premises | killed | test_audit_1::test_contradiction_trigger_fails_closed_when_prior_cannot_be_formalized |
| CP02 | contradiction detected only on the last step (pre-SG7) | killed | test_graph::test_contradicting_hypothesis_pauses |

Totals: 59 mutations (56 in the main batch plus G01, CP01 and CP02). 50 were killed by the existing suite, 8 survived, and 1 (S08) was "killed" only by a hang. All 9 are killed in about 1.5 s by `tests/audit/test_audit_8.py`, re-checked by re-applying each mutation.

## Findings

### Critical

None. Every mutation that would let an unproven, circular, inconsistent or refuted trace pass was killed by a meaningful assertion:
- V01, V02, V04–V07 (circular premises, tautologies, premise consistency).
- C01–C03 (the threshold-derived cap).
- M01–M04 (blocked-before-score ordering).
- F02–F05 (declaration validation and refusals).
- CP01 and CP02 (the contradiction trigger).

### High

**H1. Under CPU load the SG10 timing tests fail falsely, and nothing bounds a hung test.**
- **False kills under load.** With two suites running at once on 4 cores, wall-clock tests failed for mutants that cannot affect them:
  - `test_audit_2::test_ten_step_trace_overlaps_its_llm_calls` failed for C02 (score cap), S08 and D03 (a store upsert).
  - `test_audit_2::test_timeout_bounds_each_check_under_concurrency` failed for C01.
  - A shared CI runner sees the same contention, so these tests will flake.
- **Executor guards are wall-clock only.** These SG10 executor properties are guarded only by wall-clock assertions:
  - S02, S04, S09: cancel and shutdown interrupt the check (`< 0.5 s`).
  - S05: `solver_ms` excludes queueing (`wall_ms >= 4 × 0.2 s`).
  - V09: the injected executor is used.
  - So a slow runner can produce false failures, and a fast one can make a regression look like noise.
- **A hang instead of a failure.** Under S08 (`_remaining_ms` returns 0, which Z3 reads as "no limit"), the suite does not fail; it hangs. Two serial runs, with and without `-x`, were stopped by the harness's 900 s limit. CI would only stop at its 10-minute job timeout, with no test named. Round-1 L1 (no `pytest-timeout`) is still open, and this is the concrete cost.
- **Suggested:**
  - Add `pytest-timeout` (for example 60 s per test).
  - Add a `perf` marker so the wall-clock tests in `test_latency.py`, `test_solver_executor.py` and `test_audit_2.py` run in a separate, retried job.
  - Add deterministic counterparts:
    - S01 and S08: added here.
    - S02/S09: assert that `interrupt.request()` reaches `ctx.interrupt()` with a spy.
    - V09: assert the injected executor's `run` is the one called.

### Medium

**M1. Executor backpressure was untested (S01 survived).**
- *Impact:* SG10's stated mechanism is that "at most `workers` checks are in the pool, and other callers wait as coroutines". `test_backpressure_bounds_work_in_the_pool` checks the pool's thread count and running peak, which the `ThreadPoolExecutor` bounds by itself. It then checks that `_work_queue.empty()` *after* `gather` finished, which is always true.
- *Result:* with an unbounded semaphore, every check is queued inside the pool, and the test still passes.
- *Added:* `test_at_most_workers_checks_are_ever_submitted_to_the_pool`, which spies on `submit` and asserts no more than `workers` futures are ever outstanding.

**M2. The agent gate was not tested independently of the score cap (B01 survived).**
- *Impact:* `BaseAgent` gates on `VerificationMonitor(pass_threshold=hitl_threshold).decide`. If the gate reverted to the pre-SG7 `score < hitl_threshold`, no test would notice, because every test's capped score (0.39) falls below the agent's threshold. The agent's `hitl_threshold` and the verifier's thresholds are configured separately. With `hitl_threshold` = 0.3 and default verifier thresholds, a score-only gate would admit a refuted hypothesis. `build_graph` keeps them equal, which hides this.
- *Added:* `test_agent_withholds_a_refuted_trace_whose_capped_score_clears_its_gate`.

**M3. The capped score that is persisted and shown is not pinned end to end (V08 and G01 survived).**
- *Impact:* the monitor blocks any unproven or refuted trace, so routing never depends on the cap. As a result, no test noticed when:
  - the verifier dropped `require_final_valid` (V08), so an unproven conclusion scored 0.95;
  - `build_graph` stopped passing `Settings` thresholds to the `Verifier` (G01), so the cap stayed at 0.39 even with `pass_threshold=0.3`.
- Either regression would show a blocked trace on the dashboard, in `confidence` and in the `TraceEvent` with a score at or above its pass threshold. That re-opens AUDIT-3 #1 for the audit trail, though not for routing.
- *Added:* `test_unproven_bound_conclusion_is_capped_in_the_score`, `test_graph_caps_refuted_traces_below_the_configured_thresholds`.

**M4. The circularity filter's restriction to premises was not pinned (V03 survived).**
- *Impact:* no bound trace in the suite has a VALID *inference* that alone entails the conclusion, which is the usual "P; P ⇒ C; so C; therefore C" shape. If the scan also stripped inferences, every such legitimate trace would be blocked as `no_premises`, and the suite would stay green.
- *Added:* `test_bound_trace_with_an_entailed_intermediate_inference_passes`.

**M5. Coverage targets are still not enforced (round-1 M5, open).** `make test` and CI run plain `pytest`, with no `--cov-fail-under` and no per-package check.

**M6. The custom checkpointer gap is still open (round-1 M4).**
- `state/checkpointer.py` is at 80%.
- `RedisCheckpointer` has no test.
- AUDIT-3 #6 stays xfail.

### Low

**L1. Declaration validation: `;` was accepted inside a form (F01 survived).**
- `(declare-const k Bool ; )` passes the balance check. Z3 then drops the rest of the line, including the closing parenthesis, so the next line of the SMT document becomes part of the declaration.
- The existing `; (assert k)` case was rejected by the depth scan, not by the `;` check, so the `;` check had no test. In practice the smuggled document fails to parse and fails closed, so this is defense in depth.
- *Added:* `test_comment_inside_a_declaration_form_is_rejected` ×2.

**L2. Exponential growth of the backoff was untested (R08 survived).** `test_backoff_is_full_jitter_within_bounds` checks only upper bounds and spread. A constant backoff passes it. *Added:* `test_backoff_range_doubles_each_attempt`, which pins the jitter at its upper bound.

**L3. `result_id` provenance with several results was untested (K05 survived).** SG9 sets `result_id` in code, but every analysis test has exactly one result. *Added:* `test_analysis_report_points_at_the_latest_result`. The design counterpart (K04) is killed by `test_design_agent_uses_latest_hypothesis` and by the graph tests.

**L4. The escalation boundary had only indirect coverage (R12).** Widening the catch list to `Exception` was killed only through the graph, by `test_transient_error_exhausting_retries_propagates`. *Added (hardening):* `test_non_permanent_errors_propagate_out_of_the_escalation_wrapper`, covering `ConnectionError`, `TimeoutError`, `TypeError` and `KeyError`.

**L5. Weak HITL assertions are still present (round-1 L3).** 9 agent unit tests stop at `update["hitl_pending"] is not None`, without checking `trigger` or `hitl_payload`:
- `test_execute.py:123,141,155,163`
- `test_design.py:103`
- `test_analysis.py:131,149`
- `test_research.py:70,121`

**L6. The suite is 2–3× slower than in round 1.**
- Round 1: about 29 s. Now: 64–100 s on an idle machine.
- The property test alone takes 17.8 s.
- `test_concurrent_traced_runs_share_one_store[30]` takes 7.7 s.
- Round-1 M6 is still open: no Hypothesis profile and no `derandomize`.

## Round-1 repros now plain tests: still meaningful?

Each un-xfailed repro was compared with its round-1 version (`git diff c660710 HEAD -- tests/audit`):

- **AUDIT-1 #1** (×2): moved from `register_trivial_trace` to `register_circular_trace`. The new helper is byte-for-byte the old trivial shape (premise C, conclusion C citing it). It had to move because `register_trivial_trace` is now a genuinely entailed P, P ⇒ C, C. The laundering repro now registers the circular trace on the same fake, which the verifier needs. Assertions unchanged. **Not weakened.**
- **AUDIT-1 #2–#6:** only the marker and "Today" comments changed. Assertions unchanged. Mutants V06, V07 and CP01 are killed by these repros. **Not weakened.**
- **AUDIT-2 #1:** the probe sleep went from 0.05 s to 0.2 s. It still discriminates: S07 (route checks back onto asyncio's default executor, the pre-SG10 behaviour) is killed by `test_slow_checks_do_not_starve_the_default_executor`. At 0.2 s every slow check has been queued ahead of the probe. **Not weakened**, but it is wall-clock (H1).
- **AUDIT-2 #2–#7, AUDIT-3 #1, #2, #4, #8:** marker removal only. These repros kill D01, C02, F05, R09, R06, R07 and R13.
- **AUDIT-3 #3 schema repro:** now parametrized over `HYPOTHESIS_REPLY` / `DESIGN_REPLY` / `REPORT_REPLY` instead of the domain models. On its own it now checks constants, not what the agents send. The link is covered by `test_identity::test_every_agent_payload_schema_sent_to_the_model_omits_code_owned_fields`, which records the schema each agent sends through a real graph run. K01 and K02 are killed. **Acceptable**, with the caveat that the repro's meaning now depends on `test_identity`.
- **AUDIT-4 M07 test:** rebuilt from 5 to 10 steps, so the trace still scores exactly 0.7 but is no longer circular. It still asserts that the agent and the monitor agree at the threshold. **Not weakened.**

## What held up

- **SG7's soundness changes are well guarded.** `test_payload_proof.py` asserts exact `(verdict, reason)` tuples and checks fail-closed behaviour with solver stubs: a tautology or circularity check that times out still blocks. Every soundness mutant was killed on its first failing test.
- **Declaration validation** has an adversarial table (`test_merge_rejects_anything_but_one_declaration_form`) that killed 3 of 4 mutants.
- **The SG8 retry policy** is tested against the real SDK through `httpx.MockTransport`, so "one HTTP request per attempt" and `max_retries=0` are checked end to end. 13 of 14 recovery mutants were killed.
- **The SG9 store keys** are checked by behaviour on SQLite (concurrent upserts, cross-run isolation) and by compiled SQL for both dialects. All 5 store mutants were killed.
- **The round-1 audit repros** kept their assertions when un-xfailed (see above).
- **On an idle machine the suite is deterministic.** 3 of 3 full runs were green, and `make lint typecheck test` is green with the new tests (630 passed, 12 skipped, 1 xfailed).

## Added tests (`tests/audit/test_audit_8.py`, 14 tests, all pass on current code)

| Test | Kills |
|---|---|
| test_bound_trace_with_an_entailed_intermediate_inference_passes | V03 |
| test_unproven_bound_conclusion_is_capped_in_the_score | V08 |
| test_graph_caps_refuted_traces_below_the_configured_thresholds | G01 |
| test_comment_inside_a_declaration_form_is_rejected ×2 | F01 |
| test_at_most_workers_checks_are_ever_submitted_to_the_pool | S01 |
| test_exhausted_deadline_never_becomes_an_unlimited_z3_timeout | S08 (deterministically) |
| test_backoff_range_doubles_each_attempt | R08 |
| test_non_permanent_errors_propagate_out_of_the_escalation_wrapper ×4 | R12 (hardening) |
| test_analysis_report_points_at_the_latest_result | K05 |
| test_agent_withholds_a_refuted_trace_whose_capped_score_clears_its_gate | B01 |
