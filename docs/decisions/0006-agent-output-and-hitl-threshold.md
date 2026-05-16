# ADR-0006: AgentOutput wrapper and HITL threshold placement

- **Status:** Accepted
- **Date:** 2026-05-16

## Context

Every agent produces a different payload type (`Hypothesis`, `ExperimentDesign`, `ExperimentResult`, `AnalysisReport`). But all of them share the same downstream contract: the output must pass through the verifier before becoming state.

Two questions:

1. **How do we express the per-agent payload type while keeping a shared base class?**
2. **Where does the HITL threshold check live — inside the agent, or inside the orchestrator?**

## Decision

### Generic `AgentOutput[PayloadT]`

`BaseAgent` is generic in `PayloadT: BaseModel`. Each subclass declares its payload type (e.g. `class ResearchAgent(BaseAgent[Hypothesis])`) and implements `_produce(state) -> AgentOutput[PayloadT]`. The template `__call__` runs verification and then calls `_merge_payload(payload)` which the subclass overrides to specify which state fields the payload should land in.

### HITL threshold lives in `BaseAgent`

If `verdict.consistency_score < hitl_threshold` (default 0.7, configurable per-agent), the agent emits a `HITLRequest` *instead of* the payload. The orchestrator still sees a state update — just one that fills `hitl_pending` rather than appending the payload. This keeps the orchestrator dumb: it routes on `hitl_pending`, not on a separate signal.

The `VerificationMonitor` agent (separate) introduces a *coarser* three-way decision (PASS / RETRY / ESCALATE) with its own pair of thresholds. The two checks coexist: the per-agent threshold is the gate the agent enforces on itself; the monitor's thresholds are how the orchestrator decides what to do next on a verdict-history basis.

## Consequences

- Adding a new agent is mechanical: subclass `BaseAgent[YourPayloadType]`, implement `_produce` + `_merge_payload`. No verifier wiring per agent.
- The HITL threshold is configurable per-agent — a high-cost Design agent can be set to 0.85 while a cheap Research agent stays at 0.7.
- The two threshold sets (per-agent and monitor) can drift apart; documented in this ADR and revisited if Phase 7 benchmarks suggest a unified scheme.
- `AnalysisReport._merge_payload` returns `{}` deliberately — the report is informational metadata, not a list-appended fact. Will revisit if Phase 4 orchestration needs the reports addressable in state.
