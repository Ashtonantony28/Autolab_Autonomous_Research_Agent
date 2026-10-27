# AUDIT-23: API and SDK consistency, round 6

- **Auditor:** an independent agent that wrote none of the audited code or earlier audits.
- **Base:** `origin/claude/sg27-real-timeouts` @ `492980e` (SG24 to SG27 merged). Branch `claude/audit-23`.
- **Installed (from `.venv`):** anthropic 0.102.0, pydantic 2.13.4, z3-solver 4.16.0. SDK claims below were checked against that source or by running it (`anthropic.lib._parse._transform.transform_schema(DesignEstimate)`).
- **Repros:** `tests/audit/test_audit_23.py`, 5 strict-xfail cases for findings #1 to #4. Each asserts the correct behaviour. Under `--runxfail` all 5 fail on their stated assertion, 10 of 10 runs (no timing, no network, `FakeLLMClient` only). #5 and #6 are docs-only and cite `file:line`.
- **Gate:** see the end of this file.

## Summary

| # | Severity | Title | Repro |
|---|---|---|---|
| 1 | Medium | Approving an escalated estimate disagreement merges the design with no cost pause, although the estimate is above the limit | test |
| 2 | Medium | The independent estimate is never recorded: not in state, the trace, the store or the dashboard (principle 8) | 2 tests |
| 3 | Medium | On a blocked trace, a disagreement or figure reason replaces the verifier's blocking reason | test |
| 4 | Low | `design.estimate` is sent the Design agent's system prompt, including the reasoning-trace rules | test |
| 5 | Low | SG25's new `payload["feedback"]` key and `verification`-on-a-passing-verdict are undocumented where the trigger contract lives | citations |
| 6 | Low | Stale docs, docstrings and audit records from SG24 to SG27 | citations |

**Count:** 0 critical, 0 high, 3 medium, 3 low.

No finding met the High bar. The closest is #1: an experiment the estimator prices at 10 times the design's figure goes on to `execute` after one approval. The approval was of a request whose reason names the estimate, so a human did see the figure. But they were not asked about cost, and the cost policy (principle 7b) does not run on it.

---

## 1. Medium: approving an estimate disagreement skips the cost pause

**Impact.**
- SG25: when a design's figures and the independent estimate are more than 3x apart, `DesignAgent.__call__` withholds the design as a `verification` request with the gap as `payload["feedback"]` (`agents/design.py:91-102`, `_disagreement` at `:180-202`). `_apply_policy`, which runs the cost trigger on the larger figures, is reached only when there is no disagreement (`:96-98`).
- The monitor retries the request (`orchestrator/routing.py:55-69`). When the retries are spent it goes to `hitl` with the trigger still `verification`.
- On `approve`, `_resolve` re-runs the design's policy on the merged payload (`orchestrator/checkpoints.py:152-162`). That is `cost_trigger` on the design's own `estimated_cost_usd` (`graph.py:117-119`), not on the estimate. The design says $5, so it merges and the run continues to `execute`.
- So a design the estimator prices at $50 runs after an approval of a request about a figure gap, with no `policy` pause. The same design with an estimate inside 3x ($14) does get a separate `independent estimate: estimated cost $14.00 > $10.00` pause (`design.py:125-134`). A bigger gap gets the weaker check.
- ADR-0008 says: "When the estimate alone would fire the trigger, the request's reason says so, so the reviewer sees it" (`docs/decisions/0008-…md:44-46`). That holds on the `_apply_policy` path (`design.py:129-132`). On the disagreement path the reason gives only the two figures and the ratio, not that the cost or duration limit would fire.
- Related, and accepted in ADR-0008: a blocked trace whose estimate raises the cost above the limit appends the policy to the `verification` reason (`design.py:129-132`). One `approve` then covers both the blocked trace and the cost, while AUDIT-5 #3 made a `verification` approval re-check policy separately.

**Repro.** `test_approving_a_disagreement_still_pauses_on_the_estimated_cost`: a $5 / 30 min design with a verified trace and an estimate of $50. The `hitl` node (built as `build_graph` builds it, with `interrupt` patched to return `approve`) returns `goto="execute"`, not `"hitl"`.

**Root cause.** `orchestrator/checkpoints.py:152-162` checks only the merged design's own figures, and `agents/design.py:96-102` withholds a disagreement without running the trigger on `_at_least(design, estimate)`.

**Fix.** On a disagreement, also run `hitl_trigger(_at_least(design, estimate), state)` and put its reason in the request, as `_apply_policy` does. Or carry the raised figures in the request payload so the `hitl` node's re-check uses them. Either way an approved disagreement whose estimate is above a limit pauses once more as `policy`.

## 2. Medium: the independent estimate is never recorded

**Impact.**
- Principle 8: "Every decision writes a structured trace: inputs, reasoning steps, verification result, confidence, downstream effect." The estimate decides whether a design is admitted, retried or paused. Since SG25 it is the guard against an understated cost or duration.
- `DesignAgent.__call__` discards the `DesignEstimate` once it has used it (`agents/design.py:93-98`). `AutoLabState` has no field for it (`state/schema.py`), `RunTracer.record_state` writes only the state's models (`monitoring/tracer.py:43`), and `CHECKPOINT_TYPES` does not list it (`checkpoints.py:51-60`).
- Two cases:
  - **Admitted** (the estimate is within 3x and does not raise a limit): the run has no record that a second opinion was taken, of what it said, or of its basis.
  - **Paused**: the figures appear only in the prose `reason` (`design.py:128`, `:192`), and the `rationale` only on the disagreement path.
- ADR-0008 records this as a consequence (`0008-…md:47`), but CLAUDE.md §4 principle 8 is non-negotiable. With AUDIT-19 #5 (designs are written but never loaded), the dashboard cannot show what a reviewer approved or why.

**Repro.** `test_the_estimate_is_recorded_with_the_decision[admitted-at-8|policy-pause-at-14]`: the JSON of the agent's update holds no trace of the estimate's rationale.

**Root cause.** `agents/design.py:93-98`. No state field or trace event carries `DesignEstimate`.

**Fix.** Move `DesignEstimate` to `state/models.py`, add it to the state (for example `design_estimates: Annotated[list[DesignEstimate], add]`, with the design id), persist it in `RunTracer` and the store, and add it to `CHECKPOINT_TYPES`. Put the structured figures in the `HITLRequest.payload` of any request it causes. Update ADR-0008's last consequence.

## 3. Medium: a blocked trace loses its blocking reason

**Impact.**
- When the trace is blocked, `BaseAgent.__call__` returns a `verification` request whose reason is the verifier's (`agents/base.py:92-98`), for example `step 1 is INVALID (not_entailed)`.
- `DesignAgent.__call__` then checks figures and the estimate on the withheld design anyway (`design.py:86-96`). If either fires, it builds a new request with only the figure or disagreement reason (`:99-102`), dropping the verifier's.
- The monitor's retry feedback still holds the verdict summary (`routing.py:60`). But once the retries are spent, the human sees a request whose `reason`, shown by the CLI, the `hitl` trace event and the dashboard, says the cost figures are off. It does not say the trace was rejected. Only `payload["verdict_summary"]` (counts like `1 valid, 1 invalid`) hints at it. An `approve` merges a design whose reasoning was INVALID.
- `_apply_policy` (`design.py:129-132`) shows the intended pattern: it appends to the existing reason instead of replacing it.

**Repro.** `test_a_blocked_trace_keeps_its_blocking_reason_beside_the_disagreement`: a failing trace plus a $50 estimate. `blocking_reason(verdict)` is `step 1 is INVALID (not_entailed)`, and the request's reason is the disagreement alone.

**Root cause.** `agents/design.py:99-102`.

**Fix.** When `update["hitl_pending"]` already exists, append the figure or disagreement reason to its `reason` and set `payload["feedback"]` on it, instead of replacing it.

## 4. Low: the estimator gets the Design agent's system prompt

**Impact.**
- `system_prompt` routes by the purpose's family, the text before the first `.` (`verification/llm.py:134-136`). `design.estimate` therefore gets `SYSTEM_PROMPTS["design"]` (`llm.py:84-88`, `:121`): "You are the Design agent of AutoLab. Turn the given hypothesis into one concrete, safe experiment ... honest cost (USD) and duration (minutes) estimates", followed by `_TRACE_RULES` ("When asked for a reasoning trace ... The final step is the conclusion you are asked to state, verbatim").
- ADR-0008 rests on the estimate being independent of the design's author (`0008-…md:19-22`). The prompt keeps the figures out of the user turn, but the system turn tells the estimator that it is that author, and gives it rules for a trace it is never asked for.
- No other `call_structured` purpose shares a family with a different task. `research.hypothesis`, `design.experiment` and `analysis.report` each produce their own agent's payload.
- The schema's description, which the SDK passes to the model through `output_config.format`, is the class docstring "An independent cost and duration estimate of a design (SG25)." (`design.py:33`). It includes the internal sub-goal id. Fields have no descriptions. The minimum constraints are moved into descriptions as `{minimum: 0.0}` by `transform_schema` and checked again by Pydantic after decoding, which is consistent with the other reply schemas.

**Repro.** `test_the_estimate_has_its_own_system_prompt`: `system_prompt("design.estimate") == system_prompt("design.experiment")`.

**Fix.** Add an `"estimate"` entry to `SYSTEM_PROMPTS` with a reviewer role and no trace rules, and name the purpose `estimate.design`. Or make `system_prompt` look up the full purpose before its family. Drop "(SG25)" from the model-facing docstring.

## 5. Low: the `feedback` key and the trigger contract are undocumented

- `agents/base.py:15-18` is the one place that documents `HITLRequest.payload`: "`trigger` names the cause: `verification` or `policy` ... Only `verification` requests are retried." SG25 changes both halves without updating it:
  - A Design request with `trigger="verification"` can now come with a passing verdict (a figure rejection or an estimate disagreement). This was already noted by AUDIT-19 #8.
  - `payload["feedback"]` is a new optional key, set only by `DesignAgent` (`design.py:101`) and read by `make_monitor_node` (`routing.py:56`), whose docstring describes it (`routing.py:5-12`).
- `HITLRequest.payload` is `dict[str, Any]` (`state/models.py:223`), so the keys `trigger`, `verdict_summary`, `payload_kind` and now `feedback` are stated nowhere as a type. `routing.py:56` reads `feedback` with `str(... .get("feedback", ""))`. `_resolve` copies `feedback` into the `policy` request it creates on a re-pause (`checkpoints.py:155-157`), where nothing reads it.
- **Fix:** document `feedback` in `base.py`'s docstring, and consider a `TypedDict` for the request payload, the way `AutoLabState` is typed.

## 6. Low: stale docs, docstrings and audit records

- **SG24 to SG27 records:**
  - PLAN.md marks SG24 to SG27 "done (PR pending)" (`PLAN.md:42-45`). But unlike SG0 to SG23, none of them has a **Result** paragraph (files changed, tests added, verify output, open issues), which is the report format PLAN.md's Rules require (`PLAN.md:8`).
  - AUDIT-17.md to AUDIT-20.md have no **Resolution** lines for the findings that were flipped. For example, `test_audit_17.py` has no xfail left, and AUDIT-19 #1 and #2 now pass. Earlier SGs added these lines (for example `AUDIT-15.md:47`, `AUDIT-16.md:224`). AUDIT-19.md still reads as if #1 and #2 were open.
  - The test headers `tests/audit/test_audit_17.py:3-4` and `test_audit_19.py:3-5` still say every test is a strict xfail.
- **SG25:**
  - ADR-0008:
    - `:44-46`: the claim about the reason holds on one path only (#1).
    - `:42-43`: "The estimate is keyed by the design's text" describes `FakeLLMClient`, not a live model.
    - `:47`: the estimate is not stored (#2).
  - `ESTIMATE_FLOOR_USD` and `ESTIMATE_FLOOR_MINUTES` are not in `design.__all__` (`design.py:248-255`), while `ESTIMATE_DISAGREEMENT_RATIO` is. Their shared docstring sits under `ESTIMATE_FLOOR_MINUTES` only (`:27-29`).
  - `DesignEstimate` is the only LLM reply schema outside `agents/common.py` (`DESIGN_REPLY`, `HYPOTHESIS_REPLY`, `REPORT_REPLY`). `scripts/run_benchmark.py:38` imports `register_estimate` from `tests.fixtures`, so a script depends on the test package.
  - The Design system prompt still asks for "parameters with units" (`llm.py:85-87`). `design_figures` still rejects any parameter whose `value unit` is a time: `incubation_time = 30 min` is returned as `('30 min',)` (checked by running it). ADR-0008 calls `protocol_figures` "a narrow check for figures stated outright" (`0008-…md:27`). A step time is not the run's duration, so the model cannot state an incubation or exposure as a parameter.
  - `docs/status.md:107` describes the Design agent without its third call.
- **SG27:**
  - `_load` calls the C API directly: `z3.Z3_solver_check`, `Z3_solver_assert` and `Z3_ast_vector_get`, on z3py internals `solver.solver`, `solver.ctx.ref()` and `AstVector.vector` (`verification/solver.py:244-256`). z3 has no type information (`pyproject.toml:117-119`, `ignore_missing_imports`), so `mypy --strict` checks none of these calls. A `Z3_L_UNDEF` from a prefix check is not handled. The loop goes on to the next assertion unless the deadline has passed or an interrupt was requested (`:251-254`). The code is correct as written and is covered by `tests/unit/verification/test_solver_loading.py`, which patches `z3.Z3_solver_check`. But `_REARM_S`'s and `_Interrupt`'s docstrings still speak only of `Solver.check()` (`solver.py:87`, `:292`). Every other test spy patches `z3.Solver.check` (`tests/audit/test_audit_12.py:342`, `test_audit_14.py:41`, `test_audit_20.py:268-351`), so on a pool of more than 100 premises those spies miss the load checks. `test_audit_14.py:32-33` still describes "the `claim!` assumption check", which SG22 replaced with an assertion.
  - ADR-0007: "the premises are added and checked, then the negated claim is added and checked again" (`docs/decisions/0007-…md:27`). Above `_LOAD_ABOVE` the premises are now checked once per prefix. No ADR records SG26's explicit worker threads or SG27's incremental loading. CLAUDE.md §10 asks for one per non-obvious choice.
- **Still open from earlier rounds**, unchanged: `README.md:7`, `:20-21`; `docs/architecture.md:3`; `docs/status.md:142` (`hitl_node`); `checkpoints.py:16-18` (the `Checkpointer` as "*external* checkpoint"); `llm.py:3-5`, `:21-23` (`commonsense`), `:169-171`; `.env.example` (`REDIS_URL`, `LOG_LEVEL`); `pyproject.toml` `anthropic>=0.40`; `docs/benchmarks/v1.md:11-14`, `:35` (it says every scenario clears the threshold, but each pauses once on policy); ADR-0004, ADR-0005 (`purpose` as `Literal["formalize", "commonsense"]`, `:24`), ADR-0006.

---

## Earlier findings re-checked

| Item | Holds? |
|---|---|
| AUDIT-19 #1 figure guard false positives | Fixed by SG25. Both repros pass. `TOP10`, `8-week-old`, `Day 1:`, `per cent` and `2 m` return no figure. Time-valued parameters are still rejected (#6). |
| AUDIT-19 #2 figure rejection retried with its reason | Fixed by SG25. `payload["feedback"]` is retried on PASS and RETRY (`routing.py:55-69`), and both repros pass. The `feedback` key is undocumented in `base.py` (#5). |
| AUDIT-19 #3 earlier-use rule names nothing visible | Still open. The repro fails under `--runxfail`. SG24 changed tokenizing, not `prompt_for` (`formalizer.py:163-190`). |
| AUDIT-19 #4 Analysis prompt withholds the data | Still open. The repro fails. `build_analysis_prompt` is unchanged. |
| AUDIT-19 #5 designs and results never loaded | Still open. The repro fails. It now also covers the estimate (#2). |
| AUDIT-19 #6 `result_claim` raw names | Still open. 3 repros fail. |
| AUDIT-19 #7 checkpoint SQLite busy timeout | Still open. The repro fails, and `checkpointer.py` is unchanged. |
| AUDIT-7 #4, #5; AUDIT-3 #6; AUDIT-11 #2, #4, #5; AUDIT-15 #6, #7 | Unchanged. Their xfails still xfail. |

## What held up

- **Structured outputs.** `design.estimate` goes through `LLMClient.call_structured` with a Pydantic class (`design.py:93-95`). It is wrapped by `RetryingLLMClient` in `build_graph` (`graph.py:83`, `:90-99`). On the live path it is `messages.parse(output_format=DesignEstimate)` (`llm.py:236-242`). `transform_schema` keeps `additionalProperties: false` and all three required fields. The figures are compared as numbers: no `json.loads`, and no regex reads them back. Every `call` purpose in `src/` still ends in `.trace`.
- **Independence of the user prompt.** `build_estimate_prompt` holds the protocol, parameters and success criteria and never the design's own figures (`design.py:220-235`), as ADR-0008 says (only the system prompt is shared, #4).
- **Routing.** `make_monitor_node` does what its docstring, ADR-0008 and the SG25 tests say. It retries a `feedback` request on PASS or RETRY with `summary; feedback` or `feedback`, escalates on ESCALATE, on exhausted retries and on `policy` requests, and leaves requests without `feedback` as before.
- **Constants.** `ESTIMATE_DISAGREEMENT_RATIO`, the floors, `_LOAD_ABOVE`, `_RESET_SHARE`, `_REARM_S` and `_HARD_SLICES` are module `Final`s with docstrings. No `print`, no bare `except Exception` and no `json.loads` were added in `src/` by SG24 to SG27. The one `except BaseException` (`solver.py:361`) hands the exception to the caller's future, with a comment.
- **SG27 equivalence.** `_load` returns `unsat` only when a prefix is unsat, so `premises_inconsistent` is sound. The final consistency and entailment checks are still z3py `Solver.check()` calls, and the counter-model and `claim!` handling are unchanged for both load paths.

## Gate

- `make lint typecheck`: ruff clean (142 files formatted), `mypy --strict` clean (131 source files).
- `uv run pytest tests/audit/test_audit_23.py -q`: 5 xfailed. With `--runxfail`, all 5 fail on their stated assertion, 10 of 10 runs.
- `uv run pytest -m "not perf" -q`: 1179 passed, 13 skipped, 36 deselected, 36 xfailed (the base's 31 xfails plus the 5 here). `make test` (with `perf`) was not run.
