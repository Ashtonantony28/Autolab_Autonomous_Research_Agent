# AutoLab Completion Plan

Status legend: `todo` · `active` · `blocked` · `done`. Every subagent reads this file first and updates only its own row and section when it finishes.

## Rules

- **Order:** SG0 merges before anything else starts. After that, at most 2 sub-goals run at once, and never two that own the same file.
- **Subagent report format:** files changed, tests added, verification command + result, open issues. Nothing else.
- **Stop and report** if a task fails its verification 3 times, or if a fix needs a change to an SG0 interface. Do not work around it.
- **Code:** match existing conventions (`ruff`, `mypy --strict`, Pydantic v2, frozen models). Keep comments minimal and PRs small: one PR per sub-goal.
- **Fixture-first:** SG1–SG4 run on `FakeLLMClient` and golden traces only. No real LLM calls before SG5.
- **Every PR must pass:** `make lint && make typecheck && make test`, plus that sub-goal's own verification commands.

## Sub-goals

| ID | Title | Depends on | Owns (files) | Status |
|---|---|---|---|---|
| SG0 | Shared interfaces | — | `state/models.py`, `verification/llm.py`, `config.py`, `state/schema.py` | done (PR open) |
| SG1 | Verifier soundness | SG0 | `verification/solver.py`, `premise_grounder.py`, `confidence.py`, `formalizer.py`, `tests/fixtures/golden_traces.py` | done (PR pending) |
| SG2 | Checker latency budget | SG1 | `verification/vericot.py` (timing only), `scripts/bench_verifier.py` | done (PR pending) |
| SG3 | Bind traces to payloads | SG1 | `agents/*.py` (except monitor), `verification/vericot.py` (claim binding) | done (PR pending) |
| SG4 | Orchestration completion | SG3 | `orchestrator/*`, `agents/base.py`, `agents/verification_monitor.py`, `cli.py` | done (PR pending) |
| SG5 | Live LLM path | SG1–SG4 | `verification/llm.py` (Claude adapter), `tests/live/` | built, live run pending API key |
| SG6 | Persistence and observability wiring | SG4 | `monitoring/*`, `state/store.py`, `state/checkpointer.py` | done (PR pending) |
| SG7 | Soundness round 2 (AUDIT-1 #1–#4, AUDIT-3 #1) | audit round 1 | `verification/*` (except solver executor), `agents/verification_monitor.py`, `agents/base.py`, `tests/fixtures/*` | todo |
| SG8 | Permanent-error escalation and retry hygiene (AUDIT-3 #2, #4, #11) | audit round 1 | `orchestrator/recovery.py`, `verification/llm.py` | todo |
| SG9 | Entity identity and store keys (AUDIT-3 #3, AUDIT-2 store findings) | SG7 | `agents/*.py` (id handling), `state/store.py` | todo |
| SG10 | Bounded solver executor (AUDIT-2 #1) | SG7 | `verification/vericot.py` (executor only), `verification/solver.py` | todo |

Out of scope unless the owner asks: multimodal RAG (`tools/rag/`), real lab hardware.

---

### SG0: Shared interfaces

These are the contracts that later sub-goals build against. Only signatures, models and tests land here; no behavior changes.

- `FormalizedStep(BaseModel)`: `declarations: str`, `assertion: str`. This replaces the ad hoc JSON dict in `Formalizer`.
- `StepVerdict.reason: Literal["assumed", "entailed", "not_entailed", "premises_inconsistent", "formalization_failed", "no_premises", "timeout"]`, plus `solver_ms: float`.
- `VerificationContext(BaseModel)`: `facts: tuple[FormalizedStep, ...]` (grounded premises such as measurements and citations) and `payload_claim: str | None`.
- `Verifier.verify(trace, *, context: VerificationContext | None = None) -> VerificationResult`.
- `LLMClient.call_structured(purpose, prompt, schema: type[T]) -> T`. `FakeLLMClient` implements it by validating its stored JSON.
- `HITLResolution(BaseModel)`: `action: Literal["approve", "retry", "abort"]`, `note: str = ""`. New state field `hitl_payload: dict | None` holds the withheld payload.
- `Settings`: `pass_threshold=0.7`, `retry_threshold=0.4`, `max_retries=2`, `hitl_cost_usd=10.0`, `hitl_duration_min=60.0`, `solver_timeout_s=5.0`, `step_budget_ms=50.0`. These replace the three hard-coded 0.7s.

**Acceptance:** all new types are importable and round-trip through JSON; the existing 140 tests still pass unchanged; mypy strict is clean.
**Verify:** `make lint && make typecheck && make test`

**Result (2026-10-19):** files changed: `state/models.py`, `state/schema.py`, `state/checkpointer.py`, `config.py`, `verification/llm.py`, `verification/solver.py`, `verification/vericot.py`, `agents/base.py`, `agents/verification_monitor.py`, `orchestrator/routing.py`. Tests added: `tests/unit/test_interfaces.py` (16). Verify: lint and mypy clean, 156 passed (140 existing unchanged), `autolab.verification` coverage 96%. As built: `Verifier.verify` replaces `extra_premises` with `context`; solver and verifier now fill `reason` and `solver_ms`; `AutoLabState` gains `analysis_reports`, `hitl_payload`, `retry_count`, `feedback`. Open: `Formalizer` still parses raw JSON; SG1 switches it to `call_structured(..., FormalizedStep)`.

### SG1: Verifier soundness

Goal: the verifier never admits an invalid step as VALID (no false negatives) on the fixture corpus.

- Before checking any non-premise step, check that the premise set is satisfiable. If it is unsat, the step gets INVALID with reason `premises_inconsistent`.
- INVALID and UNGROUNDED steps are excluded from the premise pool.
- If a step has `referenced_step_indices`, its premises are those steps plus `context.facts`. With no references, keep the current behavior (all earlier steps).
- Score: any INVALID non-premise step caps the trace at `retry_threshold - ε`, so it can never pass. The `(V + 0.5U)/N` formula stays for the remaining cases. Record this in ADR-0007.
- `Formalizer` uses `llm.call_structured("formalize", prompt, FormalizedStep)`; a `ValidationError` becomes `FormalizationError`.
- `Vocabulary.merge` becomes atomic (build a copy, validate, then swap), and runs after the Z3 parse check.
- Expand the corpus to at least 50 golden traces: the existing 20, plus explosion, dilution (many premises with one bad conclusion), poisoned chains, wrong citations, quantifier and arithmetic edge cases, and vocabulary conflicts.
- Property test (`hypothesis`): generate random propositional traces over 3–5 atoms. A brute-force truth-table oracle decides entailment, and the verifier's verdict must match the oracle on every generated trace.

**Acceptance:** all golden traces classify correctly. The error-injection harness reports catch rate 100% and a real precision figure, replacing the placeholder. The property test passes with at least 500 examples. Verification coverage stays at or above 95%.
**Verify:** `uv run pytest tests/unit/verification tests/integration/verification -q` · `uv run python -m scripts.error_injection` · `uv run pytest --cov=autolab.verification --cov-fail-under=95 tests`

**Result (2026-10-19):** files changed: `verification/solver.py`, `premise_grounder.py`, `confidence.py`, `formalizer.py`, `vericot.py` (premise pool only), `tests/fixtures/golden_traces.py`, `scripts/error_injection.py`, and the verification tests. ADR-0007 added. Tests added: 67, including 40 new golden traces (60 in total) and `test_soundness_property.py` (500 examples, about 6 s). Verify: 117 verification tests passed. Error injection: catch rate 34/34 (100%), precision 100%, 26/26 valid traces admitted. Lint and mypy are clean. The full suite passes (223). `autolab.verification` coverage is 97.9% over `tests`. As built: the solver checks premise consistency first (`premises_inconsistent`); only VALID steps support later steps; `referenced_step_indices` restricts the premise pool; any INVALID step caps the score at `RETRY_THRESHOLD - 0.01`; `Formalizer` uses `call_structured`, and its vocabulary commits atomically after the Z3 parse check. `golden_traces.build` is the shared fixture builder. Open: a trace whose only flaw is UNGROUNDED steps can still pass (ADR-0007), and an INVALID step now always escalates rather than retrying (relevant to SG4).

### SG2: Checker latency budget

- Record per-step `solver_ms` (wall time of the Z3 check only).
- Run Z3 through `asyncio.to_thread`, so a 5 s check does not block the event loop.
- `scripts/bench_verifier.py` runs the golden corpus N times and prints p50, p95 and max per step.
- **Budget:** p95 ≤ `step_budget_ms` (50 ms) on the golden corpus; the hard timeout gives UNGROUNDED with reason `timeout`. Adjust the budget once, after the first measurement, and record the measured numbers here.

**Acceptance:** the benchmark prints numbers within budget. A test proves that a pathological formula (nonlinear integer arithmetic) times out to UNGROUNDED inside `solver_timeout_s + 1 s`. A test proves that two traces verified concurrently both finish without serializing.
**Verify:** `uv run python -m scripts.bench_verifier --runs 20` · `uv run pytest tests/unit/verification/test_latency.py -q`

**Result (2026-10-19):** files changed: `verification/vericot.py` (Z3 check via `asyncio.to_thread`, timing, default timeout), `verification/solver.py` (fresh `z3.Context` per check, one shared deadline for both checks, default `SOLVER_TIMEOUT_S`), `scripts/bench_verifier.py` (new). Tests added: `tests/unit/verification/test_latency.py` (8): `solver_ms` excludes formalization, a Fermat-cubes NIA query times out to UNGROUNDED/`timeout` within `timeout + 1 s`, the event loop keeps ticking during a slow check, two concurrent traces finish in under 0.75× their summed solver time, and the bench `run`/exit code. Bench (`--runs 20`, 60 traces, 1600 checked steps): step p50 5.72 ms, p95 8.33 ms, max 20.90 ms; per-trace total p50 6.47 ms, p95 17.08 ms, max 29.39 ms. Budget stays at 50 ms (p95 is well under it; no reason to tighten). Verify: lint and mypy clean, 231 passed, `autolab.verification` coverage 98.6%. As built: `solver_ms` is the wall time of the threaded Z3 call, so it includes about 2 ms of `z3.Context` creation and the thread hop; the formalizer's SMT parse check still runs on the event loop (cheap, no solving). Open: the shared deadline is enforced by code but only the single-check timeout paths are tested.

### SG3: Bind traces to payloads

- Each agent sets `context.payload_claim`. For Research it is the hypothesis statement, for Design the protocol rationale, and for Analysis the top interpretation.
- The verifier requires the trace's last step to be a `conclusion` whose claim matches `payload_claim`; otherwise the trace gets UNGROUNDED with reason `no_premises`.
- Execute injects the simulator's measurements as `context.facts`, so its trace is checked against data and not just the LLM's narration of it.
- Design checks that `hypothesis_id` matches the hypothesis it was given.
- Factor out the four copies of `_parse_trace` into a single helper.

**Acceptance:** for each agent there is a test that a consistent trace with an unrelated conclusion is rejected, and a test that a trace contradicting the measurements is INVALID.
**Verify:** `uv run pytest tests/unit/agents -q`

**Result (2026-10-19):** files changed: `agents/common.py` (new: `parse_trace`, `measurement_facts`, `result_claim`, `conclusion_instruction`), `agents/research.py`, `design.py`, `execute.py`, `analysis.py`, `agents/base.py` (minimal: `AgentOutput.context`, passed to `verify`), `verification/vericot.py` (claim binding), `tests/fixtures/agent_helpers.py` (`register_trace`; `conclusion`/`facts` on the trivial and failing helpers), and the callers in `tests/integration/orchestrator/test_graph.py` and `scripts/run_benchmark.py`. Tests added: 42 (16 agent binding/data tests, 17 in `tests/unit/agents/test_common.py`, 9 in `tests/unit/verification/test_payload_binding.py`). Verify: 48 agent tests passed; lint and mypy clean; full suite 273 passed; `autolab.verification` coverage 98.6% (`vericot.py` 100%); error injection catch rate 34/34 (100%), precision 100%, 26/26 valid admitted. As built: an unbound trace (last step not a `conclusion`, or its claim differs from `payload_claim` after whitespace collapse and casefold) is not formalized; every step gets UNGROUNDED/`no_premises` and the score is forced to 0.0, so it cannot pass at any threshold. Payload claims: Research the hypothesis statement, Design `ExperimentDesign.protocol` (the model has no separate rationale field), Analysis the rank-0 interpretation, Execute a code-built `result_claim` ("experiment <status>: <name> = <value>, ..."). Execute and Analysis inject one `(declare-const <name> Real)` / `(= <name> <value>)` fact per measurement (names sanitized, exact decimal literals, non-finite values raise). Design raises `ValueError` when `hypothesis_id` differs from the target hypothesis. `parse_trace` validates through a Pydantic model instead of `json.loads`, so malformed traces raise `ValidationError`. Open: a score of 0.0 means an unbound trace escalates rather than retries (SG4 may want RETRY with "conclude X" feedback); binding checks the conclusion's text, not that its formalization faithfully states it (SG5 faithfulness); a trace can still restate the payload claim as a `premise`, which is assumed VALID.

### SG4: Orchestration completion

- Wire `VerificationMonitor` into the graph: PASS goes to the next agent, RETRY re-runs the same agent with the verdict summary as feedback (up to `max_retries`), and ESCALATE goes to `hitl`.
- `hitl` resumes on an `HITLResolution`. `approve` merges `hitl_payload` and continues, `retry` re-runs the agent, and `abort` ends the run.
- Add a cost/duration HITL trigger on `ExperimentDesign` (above `hitl_cost_usd` or `hitl_duration_min`), and a trigger for a new hypothesis that contradicts an earlier verified one (`CLAUDE.md` principle 7c).
- Wrap LLM calls in `with_retries` for transient errors only. Permanent errors (validation failures) escalate to HITL instead of crashing the graph.
- Store the `AnalysisReport` in state.
- `autolab run` without an API key exits with a clear error instead of a `KeyError`.

**Acceptance:** graph tests for every edge: pass, retry then pass, retry exhausted then escalate, approve, retry and abort resumes, cost trigger, and a malformed LLM response. The benchmark scenarios with cost > $10 now pause.
**Verify:** `uv run pytest tests/integration/orchestrator tests/unit/orchestrator -q` · `uv run python -m scripts.run_benchmark`

**Result (2026-10-19):** files changed: `orchestrator/graph.py`, `routing.py`, `checkpoints.py`, `recovery.py`, `agents/base.py`, `agents/verification_monitor.py`, `cli.py`, `verification/confidence.py` (`INVALID_SCORE_CAP` only), `agents/common.py` (new `with_feedback`), `agents/research.py`, `design.py`, `execute.py`, `analysis.py` (minimal: feedback in prompts; Analysis merges into `analysis_reports`), `scripts/run_benchmark.py`, ADR-0007 (amendment), and the tests. Tests added: 24 (12 new graph tests in `tests/integration/orchestrator/test_graph.py`, `test_routing.py` rewritten for the monitor node, 5 in `test_recovery.py`, 3 monitor tests, 1 CLI test); the benchmark test now asserts the pauses. Verify: orchestrator tests 32 passed; lint and mypy clean; full suite 297 passed; `autolab.verification` coverage 98.6%; error injection catch rate 34/34 (100%), precision 100%, 26/26 valid admitted. Benchmark: all three scenarios pause once and complete after `approve`: compound_activity (`estimated cost $15.00 > $10.00`), materials_optimization (`$25.00 > $10.00`) and protein_crystallization (`estimated duration 120 min > 60 min`; its cost is $8). As built: each agent node is followed by a `monitor` node, which applies `VerificationMonitor` and routes with `Command(goto=...)`: no `hitl_pending` goes to the next agent and resets `retry_count`/`feedback`; a `verification` request rated RETRY with `retry_count < max_retries` re-runs the agent with `feedback = verdict.summary`; anything else goes to `hitl`. Agents always withhold the payload's state update in `hitl_payload` when they raise a `HITLRequest`; `payload["trigger"]` is `verification`, `policy` or `error`, and only `verification` is retried. `hitl` resumes on `HITLResolution` or the equivalent dict: `approve` merges `hitl_payload` and continues, `retry` re-runs the agent with the note (or the request's reason) as feedback and a fresh retry budget, `abort` goes to END. Triggers: Design pauses strictly above `hitl_cost_usd` or `hitl_duration_min`; Research verifies a trace of every prior hypothesis statement plus the new one as premises, then the new one citing them all, and pauses when that step is `premises_inconsistent` (Z3 finds the formalized statements jointly unsat). Every LLM call (agents and verifier) goes through `RetryingLLMClient`, which retries only `TRANSIENT_ERRORS` (Anthropic connection, rate-limit and 5xx errors, `ConnectionError`, `TimeoutError`). A `ValueError` from an agent node (`ValidationError`, `FormalizationError`, Design's `hypothesis_id` check) becomes an `error` HITL request. `autolab run` without `ANTHROPIC_API_KEY` exits 2 with a message on stderr (it no longer falls back to an empty `FakeLLMClient`). The in-process `MemorySaver` allowlists the state models for msgpack, so resumes also work under `LANGGRAPH_STRICT_MSGPACK`. **Retry-first default** (owner not yet asked; easy to flip): a trace with a refuted (INVALID) step, or one that does not conclude its payload claim (SG3's score 0.0), is retried with the verifier's feedback and escalates only after `max_retries`. `INVALID_SCORE_CAP` is now `PASS_THRESHOLD - 0.01`, so a diluted refuted trace lands in the retry band, and `VerificationMonitor.retry_rework=True` routes any trace with an INVALID or `no_premises` step to RETRY whatever its score; the unbound case is handled by reason in the monitor, so SG3's 0.0 score is unchanged. To flip back to escalate-first, set `retry_rework=False` and the cap to `RETRY_THRESHOLD - 0.01` (ADR-0007 amendment). Open: the contradiction check costs one formalization per prior hypothesis and only fires on a solver-proven inconsistency; a formalization failure or timeout leaves it silent, it treats human-approved hypotheses as verified, and it is only as faithful as the formalization (SG5). Transient errors that outlast 3 attempts still raise out of the graph. `approve` on an `error` request has no payload to merge, so the next agent usually escalates again. `autolab run` prints the pause and exits, because there is no cross-process resume until SG6. `Settings.retry_threshold` is honoured by the monitor, but the cap still derives from the module constants.

### SG5: Live LLM path

This sub-goal starts only after SG1–SG4 are done.

- `ClaudeLLMClient.call_structured` keeps SG0's `messages.parse(output_format=schema)` instead of forced `tool_choice`: it is the SDK's structured-output path, which constrains decoding to the schema and validates the reply with Pydantic, so a forced tool call would add nothing. Add one system prompt per purpose and reuse a single `AsyncAnthropic` client.
- Faithfulness check: back-translate each formalized step to English and ask a judge whether it matches the claim. A mismatch gives UNGROUNDED with reason `formalization_failed`. This runs behind a setting that is on by default for live runs.
- Add live tests, gated on `RUN_LIVE_TESTS=1`: one golden trace per category, verified end to end with real formalization; and one full `autolab run`.

**Acceptance:** with the live flag, at least 90% of golden traces classify correctly through real formalization (any misclassifications are listed here), and `autolab run "<q>"` completes. Without the flag, the suite is unchanged.
**Verify:** `RUN_LIVE_TESTS=1 uv run pytest tests/live -q` (needs `ANTHROPIC_API_KEY`)

**Result (2026-10-19):** files changed: `verification/llm.py` (system prompts, JSON-purpose routing, `is_live`), `verification/faithfulness.py` (new), `verification/vericot.py` (faithfulness hook only), `orchestrator/recovery.py` (one line: `RetryingLLMClient.live` forwards `is_live(inner)`), `scripts/live_accuracy.py` (new), `tests/live/` (new: `conftest.py`, `corpus.py`, `test_live_verifier.py`, `test_live_run.py`). Tests added: 110 offline (`tests/unit/verification/test_claude_client.py` 19, `test_faithfulness.py` 65 including all 60 golden traces with a faithful judge, `tests/integration/verification/test_live_accuracy_harness.py` 26) and 12 live tests, which skip without `RUN_LIVE_TESTS=1` and a key. Verify (offline): `tests/live` 12 skipped; lint and mypy clean; full suite 407 passed, 12 skipped; `autolab.verification` coverage 98.8% (`faithfulness.py` and `vericot.py` 100%); error injection catch rate 34/34 (100%), precision 100%, 26/26 valid admitted. **The live acceptance has not been run yet** (no `ANTHROPIC_API_KEY` in this environment), so there is no real-formalization accuracy figure and no misclassification list; run `RUN_LIVE_TESTS=1 uv run pytest tests/live -q` or `uv run python -m scripts.live_accuracy` and record both here. As built: `SYSTEM_PROMPTS` holds one prompt per purpose family (`formalize`, `faithfulness`, `research`, `design`, `execute`, `analysis`, keyed by the purpose before the first `.`), with a default for anything else; the user turn is now the bare prompt (no `[purpose=...]` prefix). The agents still call `LLMClient.call` for their JSON replies, so `call` routes those purposes (`research.hypothesis`, `design.experiment`, `analysis.report`, every `*.trace`) through `call_structured` and returns the validated JSON; agent code is unchanged. One `AsyncAnthropic` is built lazily per `ClaudeLLMClient`, and the CLI builds one client per run. Faithfulness: after a step formalizes, one `call_structured("faithfulness", ..., FaithfulnessVerdict)` asks the judge to back-translate the SMT from the SMT alone and say whether it states the claim; `faithful=false`, a `ValidationError` or a `RuntimeError` (no structured output) gives UNGROUNDED/`formalization_failed` and the step leaves the premise pool. It is a `Verifier(check_faithfulness=...)` flag; the default `None` turns it on only when `is_live(llm)` (`ClaudeLLMClient.live`, forwarded by `RetryingLLMClient`), so `build_graph`, the CLI and the contradiction trigger get it with Claude, and `FakeLLMClient` fixtures are unchanged. `config.py` is unchanged. Live corpus (`tests/live/corpus.py`): 34 traces in 9 categories: the 20 original golden traces and m04 as written, plus 13 soundness traces (2 each from explosion, dilution, poisoned, wrong citations, quantifiers and arithmetic, 1 from chains) whose placeholder fixture claims are restated in words; vocabulary conflicts are left out because they simulate a misbehaving formalizer. Open: the judge sees the claim, so it is not a blind back-translation; this costs one extra call per step and it is itself an LLM (a lenient judge reduces it to a no-op); the unfaithful step's declarations stay in the vocabulary (no assertion does); the live `autolab run` test accepts a pause, and the graph test approves up to 8 pauses and requires a hypothesis, a result and an analysis report.

### SG6: Persistence and observability wiring

- Hook `RunTracer` and `Metrics` into the graph through a node wrapper that records latency, score and escalation per agent.
- Make interrupted runs survive a restart, using LangGraph's SQLite checkpointer. That needs a new dependency, justified in `pyproject.toml`.
- Add a `streamlit run` entry script for the dashboard.

**Acceptance:** a test interrupts a run at HITL, rebuilds the graph in a fresh process-equivalent, and resumes to the end. Metrics snapshots match the agent count.
**Verify:** `uv run pytest tests/integration/monitoring tests/integration/test_resume.py -q`

**Result (2026-10-19):** files changed: `monitoring/events.py` (new `TraceEvent`), `monitoring/tracer.py`, `metrics.py`, `dashboard.py`, `state/store.py`, `state/checkpointer.py`, `orchestrator/graph.py` (node wrapper and `checkpointer`/`tracer`/`metrics` parameters only), `cli.py`, `scripts/dashboard.py` (new), `Makefile` (`make dashboard`), `pyproject.toml` + `uv.lock` (`langgraph-checkpoint-sqlite>=3.0`, which also bumped `langgraph-checkpoint` 4.1.0 → 4.3.0), and the tests. Tests added: 29 (7 in `tests/integration/test_resume.py`, 11 in `tests/integration/monitoring/test_graph_observability.py`, 6 CLI tests in `tests/integration/test_cli_resume.py`, 4 store tests, 1 metrics test; the strict-msgpack resume case is a parametrization). Verify: monitoring + resume tests 21 passed; lint and mypy clean; full suite 326 passed; `autolab.verification` coverage 98.6%; `store.py`, `graph.py`, `dashboard.py`, `events.py`, `metrics.py` 100%; benchmark unchanged (all three scenarios pause once and complete after `approve`). As built: `build_graph(tracer=, metrics=)` wraps every agent node (outside `escalate_permanent_errors`, so errors are observed too) in `_observed`, which times the node and derives the decision by calling the pure monitor node on the would-be state: `pass` (next agent), `retry` (same agent) or `escalate` (`hitl`). `Metrics.observe_agent` gains `retried` and the snapshot a `retries` count; invocations count every execution including retries, so their sum equals the number of reasoning traces. With a tracer, each execution writes one `TraceEvent` (`run_id` = the thread id, which must then be a UUID; `node`, `attempt` = `retry_count + 1`, `started_at`, `latency_ms`, `trace_id`, `consistency_score`, `verdict_summary`, `decision`, `trigger`, `hitl_reason`) plus any new entities (now including `AnalysisReport`) to the `StateStore`. `StateStore.append` upserts by id (`VerificationResult` by `trace_id`), so a resumed run in a new process re-records nothing twice; the store also gains `ensure_run`, `list_runs`, `load_reports`, `load_events` (all loads ordered by timestamp). `state.checkpointer.sqlite_saver(path)` opens an `AsyncSqliteSaver` whose serializer allowlists `CHECKPOINT_TYPES`; a test under `LANGGRAPH_STRICT_MSGPACK` fails without it. `autolab run` now checkpoints to `--checkpoints` (default `autolab_checkpoints.db`), traces to `DATABASE_URL`, and on a pause prints `autolab resume <thread_id> approve|retry|abort [--note ...]`; `autolab resume` exits 2 if the thread has no pending interrupt. `make dashboard` (`streamlit run scripts/dashboard.py [-- --db URL]`) lists persisted runs newest first and renders the decision table, hypotheses, analysis and verified trace table; a Streamlit `AppTest` renders a completed run. Open: only agent nodes emit events; the human's `HITLResolution` is not yet a trace event (it is visible only as the following agent's events). `Metrics` is in-process and the CLI does not print or export it. A run aborted at `hitl` still prints `Run ... complete.`. `Settings` has no checkpoint-path field (SG0 interface), so the path is a CLI flag with a module default. CLI tests call `main` on a worker thread because `asyncio.run` on the main thread after pytest-asyncio tests leaks a loop that `filterwarnings = error` reports in a later test. `.gitignore` does not yet ignore the default `autolab.db` / `autolab_checkpoints.db` files.

---

### Audit round 1 → SG7–SG10

Round 1 found 1 critical and 9 high findings (AUDIT-1..4 at the repo root; repros in `tests/audit/`, strict-xfail). Each sub-goal below is done when its listed xfails are flipped to plain passing tests and `make lint typecheck test` is green.

- **SG7 Soundness round 2.**
  - A trace must not conclude its payload claim from a premise that states the same claim; a circular premise yields UNGROUNDED (AUDIT-1 #1).
  - `declarations` may contain only `declare-const` / `declare-fun` / `declare-sort` forms; anything else is a formalization failure (#2).
  - The payload conclusion must be VALID for the trace to pass, whatever the score (#3).
  - Premise steps are checked for consistency with `context.facts` and with each other (#4, medium, folded in).
  - The INVALID cap is derived from the configured pass threshold, not the import-time constant (AUDIT-3 #1).
  - `register_trivial_trace` and other fixtures are rebuilt so that passing traces are genuinely entailed.
- **SG8 Escalation and retries.**
  - Refusals, empty structured output and permanent `anthropic` API errors escalate to HITL instead of crashing the graph (AUDIT-3 #2).
  - 529 Overloaded is retried (#4).
  - The SDK's own retries are set to 0, so `RetryingLLMClient` is the single retry layer, with jitter (#11, AUDIT-2 retry finding).
- **SG9 Entity identity.**
  - `id` and `created_at` are assigned in code, not by the model (AUDIT-3 #3).
  - Store keys include `run_id`.
  - Upserts are race-free (AUDIT-2 store findings).
- **SG10 Bounded solver executor.**
  - Z3 runs on a dedicated bounded executor, so it never starves `asyncio`'s default pool (AUDIT-2 #1).
  - `solver_ms` is measured inside the worker, so queueing time is excluded.

AUDIT-4 H1 (offline tests never exercise NL→SMT) is closed by SG5's live accuracy run, which waits on an `ANTHROPIC_API_KEY`. AUDIT-4 H2 is fixed by the deterministic tests it added. Medium and low findings stay in the AUDIT files for a later pass unless a sub-goal folds them in. After SG7–SG10, audit round 2 re-runs all four audits with fresh agents.

## Audits (after SG6)

Each audit is a fresh agent that wrote none of the code. It writes `AUDIT-<n>.md` with findings ranked by severity, each with a repro or a failing test. Critical and high findings become new SG rows here. Audits repeat until a full round finds no critical or high issues.

| # | Focus |
|---|---|
| AUDIT-1 | Encoding soundness: try to construct a trace that is wrong but scores ≥ pass threshold (false negatives) |
| AUDIT-2 | Performance under load: concurrent runs, 50-step traces, solver timeouts, event-loop blocking |
| AUDIT-3 | API/SDK consistency: `LLMClient`, models, agents, CLI and docs agree; no dead interfaces |
| AUDIT-4 | Test coverage: untested branches, weak assertions, fakes that test nothing |

## Log

- 2026-10-19: audit round 1 done (1 critical, 9 high); SG7–SG10 added. SG5 live acceptance waits on an API key.
- 2026-10-19: plan approved by Ashton; SG0 built.
- 2026-10-19: plan drafted. Baseline numbers (140 tests, coverage by module) are quoted from `docs/status.md` and have not yet been re-run in this environment.
