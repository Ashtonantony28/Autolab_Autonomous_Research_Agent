# AUDIT-4: Test coverage and test quality

Branch `claude/audit-4`, based on `origin/claude/sg6-persistence` (`c1c4d15`). Independent audit; no production code was changed.

**Bottom line:** line and branch coverage are high (97% total, 99% on `verification/`), and the suite is mostly strong. FakeLLMClient keys on the exact prompt, and the golden traces assert verdict and reason for every step. Of 27 one-line mutations in critical code, 23 were killed. **None of the 4 survivors weakens soundness.** The biggest gap is structural. Every offline verifier test, including the headline catch rate, sends hand-written SMT to the verifier, so the step that turns natural language into SMT is never tested offline.

## How this was measured

- Coverage: `uv run pytest --cov=autolab --cov-branch --cov-report=term-missing tests`. Result: 436 passed, 12 skipped (live-gated).
- Mutations: a script (`/tmp/.../scratchpad/mutate.py`, not committed) did this for each mutation in turn:
  1. Copied the target file aside.
  2. Applied one exact-match string replacement (asserted unique).
  3. Ran `pytest -x tests`.
  4. Restored the file.
  - The tree was verified clean after every batch.
- Flakiness: the full suite was run 3 times. All 3 runs were green (28–30 s each). The third run included the new audit tests (441 passed).

## Coverage

| Module | Stmts | Miss | Branch | BrPart | Cover | Missing |
|---|---:|---:|---:|---:|---:|---|
| agents/analysis.py | 36 | 0 | 4 | 0 | 100% | |
| agents/base.py | 48 | 0 | 6 | 0 | 100% | |
| agents/common.py | 36 | 0 | 4 | 0 | 100% | |
| agents/design.py | 34 | 0 | 4 | 0 | 100% | |
| agents/execute.py | 29 | 0 | 2 | 0 | 100% | |
| agents/research.py | 29 | 0 | 0 | 0 | 100% | |
| agents/verification_monitor.py | 24 | 0 | 4 | 0 | 100% | |
| cli.py | 62 | 5 | 12 | 0 | 93% | 66, 79-81, 97-98 |
| config.py | 29 | 0 | 0 | 0 | 100% | |
| monitoring/dashboard.py | 39 | 0 | 6 | 0 | 100% | |
| monitoring/events.py | 22 | 0 | 0 | 0 | 100% | |
| monitoring/metrics.py | 30 | 0 | 4 | 0 | 100% | |
| monitoring/tracer.py | 51 | 0 | 28 | 1 | 99% | 77->76 |
| orchestrator/checkpoints.py | 52 | 0 | 12 | 0 | 100% | |
| orchestrator/graph.py | 65 | 0 | 6 | 0 | 100% | |
| orchestrator/recovery.py | 50 | 2 | 6 | 1 | 95% | 58-59 |
| orchestrator/routing.py | 23 | 0 | 4 | 0 | 100% | |
| state/checkpointer.py | 95 | 11 | 38 | 14 | **80%** | 55-56, 60, 63, 66, 69-73, 116->118 … 148->150 (13 partial branches), 162 |
| state/models.py | 140 | 0 | 18 | 0 | 100% | |
| state/schema.py | 21 | 0 | 0 | 0 | 100% | |
| state/store.py | 108 | 0 | 12 | 0 | 100% | |
| tools/lab_hardware/simulator.py | 43 | 1 | 12 | 0 | 98% | 89 |
| tools/literature.py | 70 | 4 | 14 | 4 | 90% | 81, 107, 124, 130 |
| verification/confidence.py | 32 | 0 | 12 | 0 | 100% | |
| verification/faithfulness.py | 21 | 0 | 2 | 0 | 100% | |
| verification/formalizer.py | 63 | 0 | 14 | 0 | 100% | |
| verification/llm.py | 65 | 1 | 14 | 2 | 96% | 177, 206->205 |
| verification/premise_grounder.py | 17 | 0 | 4 | 0 | 100% | |
| verification/solver.py | 73 | 1 | 22 | 1 | 98% | 164 |
| verification/vericot.py | 63 | 0 | 18 | 0 | 100% | |
| **TOTAL** | 1482 | 25 | 282 | 23 | **97%** | |

| Package | Cover (line + branch) | Target |
|---|---:|---|
| verification/ | 99% | ≥90% ✔ |
| orchestrator/ | 99% | ≥70% ✔ |
| agents/ | 100% | ≥70% ✔ |
| state/ | 94% | ≥70% ✔ |
| monitoring/ | 99% | ≥70% ✔ |
| cli.py | 93% | ≥70% ✔ |

Untested branches in core logic, by file:

- **state/checkpointer.py**
  - 55-73: all of `RedisCheckpointer` (`__init__`, `from_url`, `_key`, `put`, `get`). It has no test, not even a live-gated one.
  - 116->…->150: every "key absent" branch of `_loads`. No test round-trips a *partial* state.
  - 162: the `tuple` branch of `_to_jsonable`.
- **cli.py**
  - 79-81: `_make_llm`. Every CLI test monkeypatches it, so no test checks that `autolab run` builds `ClaudeLLMClient` from the key.
  - 66 and 97-98: reported as uncovered, but the CLI resume tests clearly run them (they assert on the output). This looks like a coverage-tracing artifact of running `cli.main` on a `ThreadPoolExecutor` worker, so it is not a real gap.
- **orchestrator/recovery.py** 58-59: the "unreachable" tail of `with_retries`. This is dead code.
- **monitoring/tracer.py** 77->76: the branch that skips a duplicate `AnalysisReport`.
- **verification/llm.py**
  - 177: `ClaudeLLMClient("")` raising `ValueError`.
  - 206->205: skipping a non-text content block.
- **verification/solver.py** 164: `_wrap_assert` on a body that already starts with `(assert`.
- No core-logic branch in agents, routing, checkpoints, graph or store is uncovered.

## Mutation survival

The "killed by" column names the first failing test under `-x`. M02 was also re-run without `-x` to find every test that kills it.

| ID | Mutation | Result | Killed by |
|---|---|---|---|
| M01 | solver: drop the premise-consistency check (`if consistency == z3.unsat` → `if False`) | killed | test_graph::test_contradicting_hypothesis_pauses |
| M02 | solver: entailment `unknown` → `VALID/entailed` | killed, **but only by wall-clock tests** | test_latency::test_pathological_formula_times_out_to_ungrounded and ::test_concurrent_traces_do_not_serialize (these 2 only; 439 others pass) |
| M03 | confidence: drop the INVALID score cap | killed | benchmark/test_error_injection |
| M04 | vericot: UNGROUNDED steps join the premise pool | killed | test_vericot_pipeline::test_ungrounded_step_is_excluded_from_later_pools |
| M05 | vericot: skip payload binding | killed | test_graph::test_unbound_conclusion_is_retried_not_escalated |
| M06 | vericot: binding ignores `claim_type == "conclusion"` | killed | test_payload_binding::test_last_step_must_be_a_conclusion |
| **M07** | base agent: `score < hitl_threshold` → `<=` | **survived** | killed by audit test |
| M08 | routing: no `retry_count` increment | killed after **6 min** (recursion limit, no per-test timeout) | test_graph_observability |
| M09 | routing: retry any trigger, not only `verification` | killed | test_routing::test_policy_and_error_requests_are_never_retried |
| M10 | premise_grounder: ignore `referenced_step_indices` | killed | test_error_injection_harness |
| M11 | formalizer: commit vocabulary before the Z3 parse check | killed | benchmark/test_error_injection |
| M12 | RetryingLLMClient: `retry_on=Exception` | killed | test_recovery::test_retrying_llm_client_retries_transient_errors_only |
| M13 | checkpointer: `_loads` drops `retry_count` | killed | test_interfaces::test_checkpointer_roundtrips_new_fields |
| **M14** | contradiction trigger: fire on any INVALID last step, not only `premises_inconsistent` | **survived** | killed by audit test |
| M15 | monitor: `no_premises` no longer counts as rework | killed | test_graph::test_unbound_conclusion_is_retried_not_escalated |
| **M16** | hitl_node: no `retry_count` reset | **survived** | killed by audit test |
| M17 | vericot: drop claim normalization | killed | test_research::…differing_in_case_and_whitespace |
| M18 | graph: `attempt` always 1 | killed | test_graph_observability::test_tracer_records_retries… |
| M19 | faithfulness: `RuntimeError` from the judge no longer fails closed | killed | test_faithfulness::test_judge_error_fails_closed[no_output] |
| M20 | execute: measurement facts not passed to the verifier | killed (by a prompt-key mismatch, not by a semantic assertion) | benchmark/test_scenarios |
| **M21** | hitl_node: `retry` without the `agent in NEXT_NODE` guard | **survived** | killed by audit test |
| M22 | tracer: dedupe disabled | killed | test_tracer::test_tracer_persists_each_entity_exactly_once |
| M23 | base agent: policy trigger leaks the payload into state | killed | test_graph::test_cost_trigger_pauses_and_approve_continues |
| M24 | confidence: UNGROUNDED weight 0.5 → 0.75 | killed | test_vericot_pipeline::test_malformed_formalization_yields_ungrounded |
| M25 | cli: drop the "not paused" resume guard | killed | test_cli_resume::…second_resume_is_rejected |
| M26 | graph: metrics count any pending request as an escalation | killed | test_graph_observability::test_metrics_count_retries_as_executions |
| M27 | vericot: treat `inference` steps as assumed | killed | benchmark/test_error_injection |

23 of 27 were killed by the existing suite. The 4 survivors (M07, M14, M16, M21) are now killed by `tests/audit/test_audit_4.py`. M02 is now also killed deterministically by that file. Each was re-checked by re-applying the mutation.

## Findings

### Critical

None. No mutation that weakens soundness survived the existing suite.

### High

**H1. The offline verifier corpus and the "catch rate" never test formalization.**
- *Impact:* there is no offline test of formalization, which is where real errors would get through.
  - All 50+ golden traces, the error-injection harness (`scripts/error_injection.py`, which reports 100% catch rate and 100% precision), and the property test feed the verifier hand-written SMT through `FakeLLMClient`. Many golden `claim` strings are placeholders such as `"premise"` and `"inference"`.
  - So the DoD metric ("verification catches ≥80% of injected reasoning errors") measures Z3 on correct formalizations. It says nothing about the NL→SMT step, where a dropped quantifier or a flipped inequality would let an invalid step verify.
  - The only check on that step is `tests/integration/verification/test_live_accuracy_harness.py` with `tests/live/`. It is gated on `RUN_LIVE_TESTS=1`, and per PLAN.md it has never been run ("live run pending API key").
  - The faithfulness judge is also covered only with a faked "faithful: true/false" reply.
- *Demonstration:* `tests/fixtures/golden_traces.py` `build()` registers `json.dumps({"declarations": gs.declarations, "assertion": gs.assertion})` for every step. Swap the formalizer for an oracle that returns the intended SMT, and every offline verification test passes unchanged.
- *Suggested tests:*
  1. Recorded-response fixtures from a real Claude run (prompt → `FormalizedStep`), replayed offline, so formalization regressions in prompts show up in CI.
  2. An offline "adversarial formalizer" suite. Fake replies that drop a quantifier, flip `<`/`>`, or swap constants, each asserting that the faithfulness check (with a recorded judge reply) or the solver rejects the step.
  3. A CI job (manual or nightly) that runs `tests/live` and publishes the live catch rate next to the offline one.

**H2. A soundness-critical solver branch was guarded only by wall-clock tests that depend on Z3 heuristics.**
- *Impact:*
  - Mutating `unknown` → `VALID` (M02) let through every step Z3 cannot decide. Only 2 tests in `test_latency.py` caught it.
  - Both depend on Z3 *failing* to decide `x³ + y³ ≠ z³` within 1 s, and one also asserts a concurrency speed-up (`elapsed < 0.75 * sequential_s`).
  - A faster Z3 release, a change to the timeout config, or a single-core CI runner could make these tests pass vacuously or fail for unrelated reasons. Then the soundness guard disappears without anyone noticing.
- *Demonstration:* M02 without `-x`: 2 failed, 439 passed. Both failures are in `test_latency.py`.
- *Added:* `test_solver_unknown_is_never_valid` (3 cases) monkeypatches `z3.Solver.check` to return `unknown` on the consistency check or the entailment check. It asserts `UNGROUNDED/timeout` for both a true and a false claim. It is deterministic and kills M02.

### Medium

**M1. Base agent and monitor disagree at the threshold boundary, and this was not tested (M07 survived).**
- *Impact:* `BaseAgent` and `VerificationMonitor` each compare against `pass_threshold` separately. Under the `<` → `<=` mutation, a trace scoring exactly 0.7 is withheld by the agent, while the monitor rates it PASS and sends it to a human instead of passing it.
  - Scores of exactly 0.7 do occur, for example 2 VALID + 3 UNGROUNDED out of 5 steps.
  - Only the monitor's boundary was tested (`test_verification_monitor.py`).
- *Added:*
  - `test_agent_score_equal_to_threshold_passes` (`hitl_threshold=1.0` with a score of 1.0).
  - `test_agent_and_monitor_agree_at_the_default_pass_threshold`, a real 5-step trace scoring exactly `PASS_THRESHOLD` that must pass both.

**M2. Contradiction trigger specificity was not tested (M14 survived).**
- *Impact:* the trigger is meant to fire only when the prior and new hypotheses are jointly unsatisfiable (`premises_inconsistent`). If it fired on any INVALID last step, a formalization drift (`not_entailed`) would raise a false "contradicts prior hypotheses" pause. The existing tests cover only "contradiction → pause" and "consistent and entailed → no pause".
- *Added:* `test_contradiction_trigger_ignores_non_contradiction_failures`.

**M3. The human `retry` path's reset of the retry budget was not tested (M16 survived).**
- *Impact:* if `hitl_node` stopped resetting `retry_count`, an agent that a human sends back after its automatic retries ran out would get zero automatic retries. It would escalate again on its first failure. The existing test only asserts `retry_count == 0` at the end of a run, and the monitor's own reset after a pass hides the bug.
- *Added:* `test_human_retry_restores_the_automatic_retry_budget`. After the human retry, it expects 3 more design attempts before the next pause.

**M4. The custom `Checkpointer` path is untested and drops payload types.**
- *Impact:*
  - `RedisCheckpointer` has 0% coverage and no test at all, even though it is the Phase 1 DoD deliverable.
  - `InMemoryCheckpointer` and `RedisCheckpointer` are not used by the graph or the CLI (they use LangGraph's `MemorySaver` / `AsyncSqliteSaver`). Only tests use them.
  - Their `_loads` restores `hitl_payload` as raw JSON. A withheld `{"current_experiment": ExperimentDesign}` comes back as `{"current_experiment": dict}`, so approving it after a restore would merge a dict where the state schema requires a model. The existing round-trip test uses a payload that is already a plain dict, which hides this.
- *Demonstration:* `put` then `get` of a state with `hitl_payload={"current_experiment": DESIGN}`. Then `type(restored["hitl_payload"]["current_experiment"]).__name__ == "dict"`.
- *Suggested:*
  1. Either delete the custom checkpointers or type `hitl_payload` on load.
  2. Add a round-trip test with a model-valued `hitl_payload` and with a partial state.
  3. Add a `fakeredis` (or live-gated) test for `RedisCheckpointer`.
- This is not committed as a test because it would assert buggy behaviour.

**M5. Coverage targets are not enforced.**
- *Impact:* CLAUDE.md requires ≥90% on `verification/` and ≥70% on core logic. But `make test` and CI run plain `pytest` with no `--cov` and no `fail_under`, so a drop in coverage would never fail CI.
- *Suggested:* a `make test-cov` target in CI with `--cov-fail-under`, and per-package checks (for example `coverage report --include='src/autolab/verification/*' --fail-under=90`).

**M6. The property test is strong but narrow and not reproducible.**
- *Settings:* `max_examples=500`, `deadline=None`, no seed or `derandomize`, no CI profile. Each CI run draws different examples, and a failure cannot be reproduced unless the local `.hypothesis` database still exists.
- *Strength:*
  - Good: the oracle is an independent truth table, and the test asserts the exact `(verdict, reason)` of every step.
  - Missing:
    - Quantifiers, arithmetic, or uninterpreted functions.
    - `context.facts`, `payload_claim`, or formalization failures.
    - Any check on the score for VALID-only traces (≥ pass) or on the exact `(V + 0.5U)/N` formula.
  - It is also the only Hypothesis test in the repo.
- *Suggested:*
  1. A registered `ci` profile with `derandomize=True` and `print_blob=True`.
  2. A second property test over linear integer arithmetic, with an LIA oracle via brute force over a small domain.
  3. A property test on `consistency_score` (monotonic in VALID, capped whenever any step is INVALID).
  4. A property test on `Vocabulary.merged` (atomic, idempotent, conflict-detecting).

### Low

**L1. No per-test timeout.** Under M08 (no `retry_count` increment) the suite only failed after 6 minutes, when it hit LangGraph's recursion limit. The CI job timeout is 10 minutes. Suggested: `pytest-timeout` with about 60 s per test.

**L2. Timing-sensitive assertions in `test_latency.py`.**
- The checks:
  - `elapsed < 0.75 * sequential_s` (needs at least 2 cores and Z3 releasing the GIL).
  - `ticks >= …/2`.
  - `report.within_budget(STEP_BUDGET_MS)`, a 50 ms p95 on the CI machine.
- All 3 local runs passed, but these are the most likely to flake on shared runners. Consider a `perf` marker that runs in a separate job.

**L3. Some HITL assertions are weak.**
- Several agent tests stop at `update["hitl_pending"] is not None`:
  - `tests/unit/agents/test_execute.py:123,141,154,162`
  - `test_design.py:104`
  - `test_analysis.py:120,138`
  - `test_research.py:64,95`
- They do not check `payload["trigger"] == "verification"` or that `hitl_payload` holds the withheld payload. The graph tests cover these indirectly; adding the two assertions makes the unit tests self-sufficient.

**L4. FakeLLMClient strictness has two holes.**
- Lookups key on `(purpose, prompt)` exactly. That is good: a wrong prompt raises `KeyError`, and that is how M20 was caught.
- But the `schema` passed to `call_structured` is not part of the key.
- The fake does not record which registered responses were consumed. A test can register a response that the code under test never requests, and nothing reports it.
- Suggested: an `assert_all_used()` helper for graph tests.

**L5. Semantic gaps hidden by prompt keying.** M20 (measurement facts dropped) was killed only because the facts change the formalize prompt's vocabulary. That makes the key mismatch, so the test fails with `KeyError`. No assertion says "without facts, a misreported measurement would pass". The `test_execute.py` INVALID cases fill that role only incidentally.

**L6. Minor uncovered lines:**
- `cli.py:79-81`: real `_make_llm` wiring.
- `llm.py:177`: empty key raises.
- `llm.py:206->205`: non-text block.
- `recovery.py:58-59`: dead code; remove it.
- `tracer.py:77->76`: duplicate report.
- `solver.py:164`.
- `literature.py:81,107,124,130`: skip paths.

**L7. M21 (low impact).** If the `agent in NEXT_NODE` guard is removed, LangGraph only *logs* "wrote to unknown channel branch:to:ghost, ignoring it" and the run ends. `filterwarnings=error` does not catch log messages. The only visible symptom is stale `feedback`. *Added:* `test_hitl_retry_for_unknown_agent_ends_the_run`.

## Added tests (`tests/audit/test_audit_4.py`, 8 tests, all pass on current code)

| Test | Kills |
|---|---|
| test_solver_unknown_is_never_valid ×3 | M02 (deterministically) |
| test_agent_score_equal_to_threshold_passes | M07 |
| test_agent_and_monitor_agree_at_the_default_pass_threshold | M07 |
| test_contradiction_trigger_ignores_non_contradiction_failures | M14 |
| test_human_retry_restores_the_automatic_retry_budget | M16 |
| test_hitl_retry_for_unknown_agent_ends_the_run | M21 |

## What held up

- **The golden traces cannot be passed by a stub.** Each one asserts the per-step verdict tuple, and often the per-step reasons, plus which side of the threshold the score lands on. A constant or score-only stub fails dozens of them.
- **FakeLLMClient is strict:**
  - Exact `(purpose, prompt)` keys and a `KeyError` on a miss.
  - The formalize prompt carries the growing vocabulary, so breaking vocabulary growth (Pattern C) or committing the vocabulary early (M11) breaks the keys immediately.
  - Agent tests key on the real prompt builders, feedback included.
- **Every soundness mutation except M02 was killed by meaningful assertions:** premise consistency, the score cap, pool exclusion, payload binding, citation scoping, fail-closed faithfulness, assumed-only premises and retry classification.
- **Orchestration is tested end-to-end on the real graph:** retry, exhaustion, approve, retry-with-note, abort, cost and duration triggers, contradiction, malformed output, transient vs permanent errors. The tests also cover resume across a process restart (SQLite) and from the CLI.
- **The suite is deterministic across 3 runs** (436/436/441 passed, about 29 s), and lint and mypy strict are clean.
