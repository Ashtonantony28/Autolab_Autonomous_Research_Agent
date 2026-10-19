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
| SG3 | Bind traces to payloads | SG1 | `agents/*.py` (except monitor), `verification/vericot.py` (claim binding) | todo |
| SG4 | Orchestration completion | SG3 | `orchestrator/*`, `agents/base.py`, `agents/verification_monitor.py`, `cli.py` | todo |
| SG5 | Live LLM path | SG1–SG4 | `verification/llm.py` (Claude adapter), `tests/live/` | todo |
| SG6 | Persistence and observability wiring | SG4 | `monitoring/*`, `state/store.py`, `state/checkpointer.py` | todo |

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

### SG4: Orchestration completion

- Wire `VerificationMonitor` into the graph: PASS goes to the next agent, RETRY re-runs the same agent with the verdict summary as feedback (up to `max_retries`), and ESCALATE goes to `hitl`.
- `hitl` resumes on an `HITLResolution`. `approve` merges `hitl_payload` and continues, `retry` re-runs the agent, and `abort` ends the run.
- Add a cost/duration HITL trigger on `ExperimentDesign` (above `hitl_cost_usd` or `hitl_duration_min`), and a trigger for a new hypothesis that contradicts an earlier verified one (`CLAUDE.md` principle 7c).
- Wrap LLM calls in `with_retries` for transient errors only. Permanent errors (validation failures) escalate to HITL instead of crashing the graph.
- Store the `AnalysisReport` in state.
- `autolab run` without an API key exits with a clear error instead of a `KeyError`.

**Acceptance:** graph tests for every edge: pass, retry then pass, retry exhausted then escalate, approve, retry and abort resumes, cost trigger, and a malformed LLM response. The benchmark scenarios with cost > $10 now pause.
**Verify:** `uv run pytest tests/integration/orchestrator tests/unit/orchestrator -q` · `uv run python -m scripts.run_benchmark`

### SG5: Live LLM path

This sub-goal starts only after SG1–SG4 are done.

- `ClaudeLLMClient.call_structured` uses tool use with `schema.model_json_schema()` and forced `tool_choice`. Add one system prompt per purpose and reuse a single `AsyncAnthropic` client.
- Faithfulness check: back-translate each formalized step to English and ask a judge whether it matches the claim. A mismatch gives UNGROUNDED with reason `formalization_failed`. This runs behind a setting that is on by default for live runs.
- Add live tests, gated on `RUN_LIVE_TESTS=1`: one golden trace per category, verified end to end with real formalization; and one full `autolab run`.

**Acceptance:** with the live flag, at least 90% of golden traces classify correctly through real formalization (any misclassifications are listed here), and `autolab run "<q>"` completes. Without the flag, the suite is unchanged.
**Verify:** `RUN_LIVE_TESTS=1 uv run pytest tests/live -q` (needs `ANTHROPIC_API_KEY`)

### SG6: Persistence and observability wiring

- Hook `RunTracer` and `Metrics` into the graph through a node wrapper that records latency, score and escalation per agent.
- Make interrupted runs survive a restart, using LangGraph's SQLite checkpointer. That needs a new dependency, justified in `pyproject.toml`.
- Add a `streamlit run` entry script for the dashboard.

**Acceptance:** a test interrupts a run at HITL, rebuilds the graph in a fresh process-equivalent, and resumes to the end. Metrics snapshots match the agent count.
**Verify:** `uv run pytest tests/integration/monitoring tests/integration/test_resume.py -q`

---

## Audits (after SG6)

Each audit is a fresh agent that wrote none of the code. It writes `AUDIT-<n>.md` with findings ranked by severity, each with a repro or a failing test. Critical and high findings become new SG rows here. Audits repeat until a full round finds no critical or high issues.

| # | Focus |
|---|---|
| AUDIT-1 | Encoding soundness: try to construct a trace that is wrong but scores ≥ pass threshold (false negatives) |
| AUDIT-2 | Performance under load: concurrent runs, 50-step traces, solver timeouts, event-loop blocking |
| AUDIT-3 | API/SDK consistency: `LLMClient`, models, agents, CLI and docs agree; no dead interfaces |
| AUDIT-4 | Test coverage: untested branches, weak assertions, fakes that test nothing |

## Log

- 2026-10-19: plan approved by Ashton; SG0 built.
- 2026-10-19: plan drafted. Baseline numbers (140 tests, coverage by module) are quoted from `docs/status.md` and have not yet been re-run in this environment.
