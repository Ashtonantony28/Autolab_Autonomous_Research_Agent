# Phase 3 — Individual Agents Plan

**Goal:** Five agents — Research, Design, Execute, Analysis, VerificationMonitor — each extending a `BaseAgent` ABC that enforces produce → verify → state-update. All testable with `FakeLLMClient`; live API path exists but gated.

**Out of scope:** LangGraph orchestration (Phase 4), real lab hardware (Phase 5; we'll use a tiny stub simulator here), Streamlit UI.

---

## 1. Architecture

```
BaseAgent (ABC)
  ├─ _produce(state) -> AgentOutput          (subclass implements)
  ├─ __call__(state) -> AutoLabState update  (template method)
       1. output = await _produce(state)
       2. verdict = await verifier.verify(output.trace)
       3. if verdict.consistency_score < threshold → emit HITL request
          else → emit the agent's state update
```

`AgentOutput` is a small Pydantic model wrapping `(payload, reasoning_trace)`. Subclasses constrain `payload` to their specific output type (Hypothesis, ExperimentDesign, ExperimentResult, AnalysisReport).

The `VerificationMonitor` is special — it doesn't produce; it inspects the latest trace + verdict and decides `pass | retry | escalate`.

## 2. Modules

- `agents/base.py` — `BaseAgent` ABC, `AgentOutput` wrapper, `AgentDecision` enum, `HITL_THRESHOLD: Final = 0.7`.
- `agents/research.py` — `ResearchAgent.produce`: takes `research_question` from state, queries (mocked in tests) literature, returns a `Hypothesis` with at least one `Citation` and a reasoning trace.
- `agents/design.py` — `DesignAgent.produce`: takes latest `Hypothesis`, returns `ExperimentDesign` with parameters & cost/time estimate.
- `agents/execute.py` — `ExecuteAgent.produce`: takes latest `ExperimentDesign`, calls a `LabSimulator` (a stub here; full version in Phase 5), returns `ExperimentResult`.
- `agents/analysis.py` — `AnalysisAgent.produce`: takes latest `ExperimentResult`, returns ranked interpretations (modeled as a new Pydantic type `AnalysisReport`).
- `agents/verification_monitor.py` — `VerificationMonitor.decide(verdict)` returns `AgentDecision`.
- `verification/llm.py` — extend with `ClaudeLLMClient` (real Anthropic adapter, not unit-tested with live API).
- `tools/lab_hardware/simulator.py` (new) — minimal stub `LabSimulator.run(design) -> ExperimentResult` returning plausible measurements; expanded in Phase 5.
- `state/models.py` — add `AnalysisReport` model (ranked interpretations).

## 3. Tests

For each agent:

- Unit test with `FakeLLMClient`: verify `_produce` returns a well-formed output + reasoning trace.
- Integration test: `await agent(state)` returns a valid state update with a verifier-cleared trace.
- HITL path: deliberately register a low-confidence formalization so verifier returns score < 0.7; assert the agent emits a `HITLRequest` instead of an update.

VerificationMonitor: 3 unit tests covering pass / retry / escalate decisions on stubbed `VerificationResult`s.

LabSimulator stub: 2 unit tests — produces measurements matching design parameters; never raises.

Coverage target: ≥75% on `agents/` (pragmatic; the live LLM path can't be unit-tested).

## 4. Definition of Done

1. All five agents implemented, each with at least one passing happy-path test + the HITL fallback path.
2. `BaseAgent` invariant: every output passes through the verifier before becoming state.
3. `ClaudeLLMClient` exists, instantiable, **not** exercised in tests (live tests gated on `RUN_LIVE_TESTS=1`).
4. `make lint typecheck test` all green.
5. ADR-0006 explaining `AgentOutput` and the HITL threshold.

## 5. Risks

- **Output schema drift.** Each agent has its own Pydantic payload — easy to forget to register one with the verifier. Mitigation: `BaseAgent` is generic in the payload type, enforced via `Generic[PayloadT]`.
- **Reasoning trace authoring** for tests gets verbose. Mitigation: a small `tests/fixtures/agent_traces.py` helper that builds canonical traces + matching fake formalizations.
- **`structlog` config** if I touch logging now without an explicit setup. I'll keep agents silent for Phase 3 (no log calls) — observability lands in Phase 6.
