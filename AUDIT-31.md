# AUDIT-31: API and SDK consistency, round 8

- **Auditor:** an independent agent that wrote none of the audited code or earlier audits.
- **Base:** `origin/claude/sg32-newcomers` @ `f09ea60` (SG31, SG32 and SG33 merged). Branch `claude/audit-31`.
- **Scope:** what changed since round 7 (`git diff 7123eed..f09ea60`): `state/models.py` (new `StepReason` value `worker_died`), `verification/solver.py`, `solver_workers.py`, `formalizer.py`, `vericot.py`, the deleted `_forkserver_init.py`, ADR-0009 and PLAN.md. No agent, orchestrator, CLI, config or LLM-client code changed. Every consumer of `StepReason`, the monitor's routing, the dashboard, the contradiction trigger, the bench script and the docs were re-read against the new value.
- **Repros:** `tests/audit/test_audit_31.py`, 4 strict-xfail tests for findings #1 to #4, plus one plain test that pins the stdlib patch #4 is about. Each xfail asserts the correct behaviour. Under `--runxfail` all 4 fail on their stated assertion, 5 of 5 runs. No timing assertions, no network, no LLM. #5 and #6 are docs-only and cite `file:line`.

## Summary

| # | Severity | Title | Repro |
|---|---|---|---|
| 1 | Medium | A worker that dies on an intermediate step is admitted with PASS. ADR-0009's reason for not retrying ("the monitor already re-runs the step") is false | test |
| 2 | Low | `Settings.solver_timeout_s` accepts any value; above `TIMEOUT_MAX_S` the solver clamps it silently. The cap lives in `solver.py`, not `config.py`, and is not exported | test |
| 3 | Low | SG33: `MAX_PROBE_CHARS` admits less than its docstring states, and the prompt gives no countable bound for it | test |
| 4 | Low | SG32 patches `multiprocessing.spawn.get_preparation_data` process-wide at import. `Workers(target)` can no longer run a target defined in `__main__`, and that is not documented | test |
| 5 | Low | `Verifier()` now starts processes in its constructor, on CLI paths that never check. This makes the open AUDIT-27 #3 shutdown race reachable from `Verifier` | citations |
| 6 | Low | Records and API docs: SG0 `StepReason` contract, SG31 to SG33 have no Result paragraph, new public constants missing from `__all__`, dead `or "timeout"` fallback, dashboard hides `reason`, bench counts lost workers as latency | citations |

**Count:** 0 critical, 0 high, 1 new medium, 5 low. One medium is carried over: AUDIT-21 #1 / AUDIT-23 #1 is still open, unchanged since round 5.

No finding met the High bar. Every failure mode found here fails closed on the step concerned: a lost worker is never VALID. #1 is Medium because ADR-0009's decision not to retry rests on a claim the routing does not keep, and the result is a passed trace containing a step that was never checked (CLAUDE.md §8 "No silent failures", principles 7 and 8).

---

## 1. Medium: a check whose worker died is admitted, not re-run

**Impact.**
- SG31 added `StepReason` `"worker_died"` (`state/models.py:161`). A worker that dies, fails to start or answers out of turn gives UNGROUNDED/`worker_died` (`solver.py:293-299`).
- ADR-0009 says (`docs/decisions/0009-…md:51`): "**No retry on a fresh worker.** A death fails closed, as a timeout does. … The verification monitor's retry or escalation already re-runs the step."
- The monitor does not treat `worker_died` specially. `blocking_reason` blocks only INVALID, `no_premises` or a non-VALID final step (`agents/verification_monitor.py:58-67`). `consistency_score` weighs any UNGROUNDED step as 0.5 (`confidence.py:57-63`). So a lost worker on any step other than the last is routed by score alone. In a 4-step trace that is 0.875, which is PASS. No retry and no escalation happen, and nothing re-runs the step.
- This is the gap ADR-0007 leaves open for timeouts ("3 VALID + 1 UNGROUNDED = 0.875", `0007-…md:42`). A worker death is different: it is an infrastructure fault (the OOM killer, a Z3 abort, a `kill`), not a property of the reasoning. ADR-0009 explicitly relies on a re-run that does not happen.
- When the lost step is the last one, the trace is blocked and RETRY re-runs the *agent*, which gets a new LLM trace with the verifier's summary as feedback. It does not re-run the step. The feedback tells the agent its reasoning was ungrounded when a process died.

**Repro.** `test_a_check_whose_worker_died_is_rerun_or_escalated_not_passed`:
- Trace: premises `p ∧ (p ⇒ r)` and `q`, an inference `r` citing step 0, and a conclusion `q` citing step 1.
- The worker of the third check (step 2) is killed as its request is sent.
- Observed: step 2 is UNGROUNDED/`worker_died`, "worker died (signal 9)". `consistency_score == 0.875`. `VerificationMonitor().decide(result)` is `AgentDecision.PASS`.

**Fix.** Choose one and make the ADR say what the code does:
- Treat `worker_died` as blocking in `blocking_reason`, so the trace is retried and then escalated.
- Or re-run a died check once on a fresh worker before failing closed. The ADR's cost argument covers a crash that recurs, and a second death is then reported.
- Or reword the ADR to say a death on a non-final step is admitted like a timeout (ADR-0007's open gap), and record that it was chosen.

## 2. Low: Settings accepts a timeout the solver silently clamps

- SG31 clamps the timeout in `Z3Solver._args`: `min(self.timeout_seconds, TIMEOUT_MAX_S)` (`solver.py:280`, cap `1e6` at `:161`). This fixes AUDIT-27 #2's `OverflowError`, and `test_audit_27.py` #2 now passes.
- `Settings.solver_timeout_s` is still `Field(gt=0.0)` with no upper bound (`config.py:49`). `build_graph` passes it through unchanged (`orchestrator/graph.py:86`). `SOLVER_TIMEOUT_S=2e6` or `inf` is accepted and then becomes 1e6 s without a log line or error. AUDIT-27 #2's suggested fix was to bound it in `Settings`.
- CLAUDE.md §8 puts constants in `config.py`. `SOLVER_TIMEOUT_S` is there, but its new upper bound `TIMEOUT_MAX_S` is in `solver.py`. That name is public (no underscore) but not in `solver.__all__` (`:863-873`).
- **Repro.** `test_settings_refuse_a_solver_timeout_the_solver_would_clamp`: `Z3Solver(2e6)._args(...)[-1] == 1e6`. `Settings(solver_timeout_s=2e6)` raises nothing.
- **Fix.** Add `le=TIMEOUT_MAX_S` to the field, with the constant moved to `config.py` as `Final[float]`. Or at least log the clamp.

## 3. Low: SG33 probe budget, docstring and prompt against the code

- `MAX_PROBE_CHARS`' docstring says the budget is "one `MAX_ARITY` Int function and a few dozen constants" (`formalizer.py:296-298`).
- Measured with `_probe_chars`:
  - a 32-argument Int function costs 137 632;
  - a Bool constant costs 1 220;
  - the budget is 163 840.
  - So one such function leaves room for **21** constants. The 22nd is refused: `declarations up to 'c21' need over 163840 characters of builtin probes`. Two 32-argument Int functions are refused together, though each is within `MAX_ARITY`.
- The prompt states the arity and nesting caps as numbers (`:207-209`). The probe budget appears only as "a step's declarations must stay small (each function of many Int or Real arguments costs the most)" (`:210-211`). The refusal names a character count the model cannot measure. A reply that keeps every numeric rule the prompt states can therefore be refused, and the retry is told only to "declare fewer or narrower symbols".
- What held: the nesting cap matches the prompt exactly. Depth 8 inside the outer pair is accepted and 9 is refused, for both `declare-fun` and `declare-const` (checked by running it). The arity cap still runs before any probe, the probes now run only after every line is costed, and the refusal is a `FormalizationError`, so the formalizer's retry sees it.
- **Repro.** `test_the_probe_budget_admits_what_its_docstring_promises`: one 32-ary Int function plus 24 Bool constants. Today it is refused at `c21`.
- **Fix.** Correct the docstring ("about 20 constants"), or raise the budget. State the budget in the prompt in countable terms (for example "at most N declarations, or one function of more than 16 Int/Real arguments").

## 4. Low: SG32's stdlib patch, and a `Workers` target in `__main__`

- `solver_workers` replaces `multiprocessing.spawn.get_preparation_data` at import (`solver_workers.py:68-85`). It drops `init_main_from_path`/`init_main_from_name` for processes named `z3-worker`. Every process gets that name (`:33`, `:94`). The import happens transitively on any import of `autolab.verification.solver`, `vericot` or `autolab.orchestrator`. ADR-0009's SG32 amendment documents the patch and why, and the ADR's account of CPython 3.11's forkserver is accurate (`forkserver.ensure_running` keeps only `main_path`/`sys_path`, which `get_preparation_data` never emits).
- **Consequences not documented:**
  - `Workers(target, idle_max, preload)` takes any target, and its docstring says so (`:161-162`). A target defined in the caller's `__main__` can no longer be unpickled in the worker. Every worker "failed to start", `call` returns `None`, and the only sign is a forkserver traceback (`AttributeError: Can't get attribute 'double' on <module '__main__' (built-in)>`), which is not always on the caller's stderr. The module docstring says "A worker needs nothing from the caller's main module" (`:9-11`). That holds only for `solver._solve`.
  - This is a process-wide change to the stdlib, made by importing a library. CLAUDE.md principle 5 rules out global mutable state. The patch is idempotent in effect and filtered by name, but a host process of its own named `z3-worker` would also lose its main module. Separately (pre-existing since SG29), `ctx.set_forkserver_preload` (`:181`) sets the preload of the one process-wide forkserver, which a host application may also use.
  - `solver_workers` still has no `__all__`. It now exports `DIED`, `FAILED_TO_START` and `OUT_OF_TURN` as `Final[str]` fault codes, used by `solver._lost`.
- **Repro.** `test_workers_run_a_target_defined_in_the_main_module` runs a guarded script whose `Workers(double)` worker gets `call((21,), 30)`. Observed: `REPLY None 'failed to start'`. `test_the_patch_is_installed_on_import` (passes) pins that `spawn.get_preparation_data is solver_workers._without_main`.
- **Fix.** Either make `Workers` private to the solver (rename it, or bind the target to `_solve`), or document the target constraint in `Workers`' docstring and ADR-0009. Consider installing the patch on the first `Workers._context()` rather than at import, so a program that only imports the package keeps the stdlib unchanged.

## 5. Low: `Verifier()` starts processes in its constructor

- SG32 makes `Verifier.__init__` call `default_executor()` when no executor is given (`vericot.py:101-104`). That constructs `SolverExecutor`, whose constructor starts a thread that starts the forkserver and up to `workers + slow_workers` worker processes (`solver.py:729-732`). The constructor docstring says so (`vericot.py:89-90`). `build_graph`'s docstring (`orchestrator/graph.py:70-80`) and README.md do not.
- `build_graph` always builds a `Verifier` (`graph.py:84`). The CLI calls `build_graph` before it knows whether anything will be checked (`cli.py:115`). So `autolab resume <id> abort`, a resume of an unknown or finished thread (exit 2), and every test that builds a graph start the forkserver and workers for nothing.
- This also widens the open AUDIT-27 #3 race. `shutdown_default_executor()` does not stop the pre-start thread, so workers started after it returns stay running until exit. That race was reachable only through `default_executor()`; it is now reachable through `Verifier(llm)` followed by `shutdown_default_executor()`. `test_audit_27.py::test_no_worker_is_left_running_after_shutting_down_the_default_executor` still fails under `--runxfail` (4 workers left).
- **Fix.** Document the side effect where callers meet it (`build_graph`, README). Fixing AUDIT-27 #3 (a closed flag on `Workers`) removes the race.

## 6. Low: records and API docs

- **SG0 contract.** PLAN.md's SG0 section is the contract later SGs build against. It lists `StepVerdict.reason` without `worker_died` (`PLAN.md:62`). ADR-0007's reason list (`0007-…md:23`, `:27`) also predates it. ADR-0009's amendment is the only place the new value is described.
- **`check_entailment` docstring.** The new line "A worker lost before its budget ran out → UNGROUNDED (`worker_died`)" (`solver.py:222`) sits under "Any solver error / unknown / timeout / killed worker → UNGROUNDED", and the docstring still says it raises `SolverWorkerError` (`:224-225`). The AUDIT-27 #4 contradiction is unchanged.
- **Exports.** None of these public `Final` names is in an `__all__`: `TIMEOUT_MAX_S`, `MAX_ARITY`, `MAX_NESTING`, `MAX_PROBE_CHARS` (`formalizer.__all__`, `:496`), or the three fault codes (#4). The `formalizer` constants are untyped `Final` (`:289`, `:292`, `:296`), unlike the `Final[int]` elsewhere. `Vocabulary.merge` and `Formalizer.formalize` still list only some of their `FormalizationError` causes (`formalizer.py:113-118`, `:130-138`); `merged` lists them all (`:57-63`).
- **Dead fallback.** `vericot.py:250`: `reason: StepReason = trivial.reason or "timeout"`. `SolverOutcome.reason` is a required `StepReason`, never empty, so the fallback cannot fire. A lost worker in the tautology probe is now reported as `worker_died`, which is correct, but the line suggests otherwise.
- **Observability.** `render_trace_table` shows `verdict` and `details` but not `reason` (`monitoring/dashboard.py:64-90`). A reviewer can tell `worker_died` from `timeout` only by the details text. `scripts/bench_verifier.py:25` excludes only `assumed` and `formalization_failed` from latency, so a lost worker's few-ms `solver_ms` is counted as a solved check's latency.
- **Records.**
  - SG28 to SG33 are "done (PR pending)" with no **Result** paragraph (`PLAN.md:46-51`). The last Result is in the round-4 section (`:393`). AUDIT-23 #6 and AUDIT-27 #5 already raised this.
  - AUDIT-22 and AUDIT-24 to AUDIT-28 have no **Resolution** lines for the findings SG28 to SG33 flipped. For example, AUDIT-27 #1 and #2 now pass undecorated in `test_audit_27.py`, but AUDIT-27.md reads as if they were open.
  - ADR-0007 still has no back-pointer to ADR-0009.

---

## Earlier findings re-checked

| Item | Holds? |
|---|---|
| **AUDIT-21 #1 / AUDIT-23 #1** (medium): approving an escalated design re-checks cost on the design's own figures, not the estimate | **Still open.** `orchestrator/checkpoints.py:149-157` and `agents/design.py` are unchanged. Under `--runxfail`, both `test_audit_21.py::test_approving_a_figure_request_still_pauses_on_the_larger_figure` cases and `test_audit_23.py::test_approving_a_disagreement_still_pauses_on_the_estimated_cost` still fail. |
| AUDIT-27 #1 (medium): a dead worker reported as a timeout | **Resolved** by SG31. `_lost` gives `worker_died` with "worker died (signal 9)" or "worker failed to start" (`solver.py:293-299`). Both `test_audit_27.py` #1 repros pass. The routing consequence is new finding #1. |
| AUDIT-27 #2 (low): `OverflowError` for a long timeout | **Resolved in the solver** (clamp). `Settings` is still unbounded (#2). |
| AUDIT-27 #3 (low): `shutdown_default_executor()` races the pre-start thread | **Still open**, and now reachable through `Verifier()` (#5). |
| AUDIT-27 #4 (low): API docs | **Mostly open.** The "workers means two things" and CPU-budget wording are unchanged. The `check_entailment` contradiction and the missing `__all__` remain. Missing exports grew by four names (#6). |
| AUDIT-27 #5 (low): records | **Partly resolved.** The main-module rule is moot since SG32, and the ADR strikes it through with a reason. The Result, Resolution and ADR-0007 gaps remain (#6). |
| AUDIT-23 #2 to #5 | Unchanged: no agent, orchestrator or LLM code changed. Their repros still fail under `--runxfail`. |

## What held up

- **`worker_died` consumers.**
  - The contradiction trigger fails closed on any non-VALID step, so it pauses with "could not rule out a contradiction: step n is worker_died" (`checkpoints.py:219-222`).
  - `blocking_reason` names the reason when it is the final step.
  - `StepVerdict` persists as JSON (`store.py:72`), so the new literal needs no migration.
  - No `match` or exhaustive switch on `StepReason` exists anywhere in `src/`.
- **SG31.**
  - The sequence-number match (`solver_workers.py` `call`) does what ADR-0009 says. Any other message sets `OUT_OF_TURN`, and `check_entailment` discards that worker.
  - `ready()` is inside the guarded block.
  - `_lost` reads the exit code after `discard` has joined the process, so the signal is reported (checked: "worker died (signal 9)").
- **SG32.**
  - The ADR's CPython 3.11 analysis is correct. `_forkserver_init.py` and `_main_imports` have no remaining references outside a comment in `test_audit_28.py:547`.
  - An unguarded script's check is VALID and its body runs once (`test_audit_27.py` #1 script repro passes).
  - Newcomer ranking in `_Slots._rank` and `SolverExecutor.run` matches the module docstring, the `_Slots` docstring and the amendment: rank 1 with short callers, and no lending, because `_grant` is called with `short=False`.
- **SG33.** The nesting cap and the prompt agree to the level. Costs are counted before any parse. Every new refusal is a `FormalizationError` whose message names the cap.
- **SDK.** No change since round 6. `call_structured` still goes through `messages.parse(output_format=...)`. No `json.loads` or regex was added on LLM output.

## How this was measured

- Read CLAUDE.md, PLAN.md (SG0, round 7 section), AUDIT-27.md and ADR-0007/0009. Read `git diff 7123eed..f09ea60` for `src/` and `docs/`. Grepped every use of `StepReason`, `.reason`, `timeout_seconds`/`solver_timeout`, `__main__` and `preload` in `src/`, `scripts/` and the docs.
- #1: a 4-step golden-style trace through `Verifier(FakeLLMClient)` and `VerificationMonitor().decide`. The worker is killed on the third `Worker.call`.
- #3: `_probe_chars` evaluated for 1 to 32 arguments of Int, Real and Bool, then `Vocabulary().merged` with one 32-ary Int function plus k constants, increasing k until refused. Nesting was checked by building `(Array Int …)` sorts to depths 6 to 9.
- #4: a guarded script defining `double` in `__main__`, run with `.venv/bin/python` (traceback seen on stderr when run directly) and under `subprocess.run` in the test.
- Repro stability: `uv run pytest tests/audit/test_audit_31.py --runxfail -p no:randomly` 5 times: 4 failed and 1 passed each time, on the asserted lines.

## Gate

- `make lint typecheck`: ruff clean (154 files formatted), `mypy --strict` clean (141 source files).
- `uv run pytest tests/audit/test_audit_31.py -q`: 1 passed, 4 xfailed.
- `uv run pytest -m "not perf" -q -p no:randomly`: 1364 passed, 13 skipped, 50 deselected, 71 xfailed (including the 4 here). `make test` (with `perf`) was not run.

## Bottom line

SG31 to SG33 do what PLAN.md asked. A lost worker is named as such, replies are matched to their request, a long timeout no longer overflows, newcomers rank as short, an unguarded main runs once, and probe work per reply is bounded. ADR-0009's amendments describe that code accurately with one exception. The new `worker_died` reason has no consumer that acts on it: the monitor routes it as any UNGROUNDED step, so a crashed check on an intermediate step passes, and the ADR's stated reason for not retrying it is not true. The rest are edges of the new interfaces:
- a timeout bound that `Settings` does not enforce;
- a probe budget smaller than its docstring and absent from the prompt;
- a process-wide stdlib patch that silently narrows the generic `Workers` API;
- a constructor that now starts processes.

The open medium from rounds 5 to 7, an approved estimate disagreement that skips the cost pause, is unchanged.
