# Phase 4 — Orchestration Plan

**Goal:** A LangGraph `StateGraph` wiring all 5 agents into a runnable flow with HITL interrupts and retry/escalation routing. End-to-end test: toy research question runs through all agents to completion, with at least one HITL pause + resume.

**Out of scope:** real Postgres backing (still SQLite in tests), full hardware simulator (Phase 5), dashboard (Phase 6).

## Modules

- `orchestrator/graph.py` — `build_graph(verifier, llm, simulator)` returns a compiled `StateGraph`. Nodes: `research`, `design`, `execute`, `analysis`, `hitl`. Edges:
  ```
  START → research → route_after(research)
    ├─ pass → design → route_after(design)
    │   ├─ pass → execute → route_after(execute)
    │   │   ├─ pass → analysis → route_after(analysis)
    │   │   │   ├─ pass → END
    │   │   │   └─ hitl → hitl
    │   │   └─ hitl → hitl
    │   └─ hitl → hitl
    └─ hitl → hitl
  hitl → (interrupt, await human resume) → END
  ```
- `orchestrator/routing.py` — `route_after(agent_name)` returns a router function that inspects `hitl_pending` and the latest `VerificationResult`. Maps to "next" / "hitl" / "retry" / "end".
- `orchestrator/checkpoints.py` — adapter from our `Checkpointer` protocol to LangGraph's `BaseCheckpointSaver`. Phase 1 deferred this (ADR-0004); we land it here.
- `orchestrator/recovery.py` — small `with_retries(coro, max_attempts=3, backoff=...)` helper for transient errors. Not LangGraph-specific.

## Tests

- `tests/integration/orchestrator/test_graph.py` — end-to-end:
  - Build graph with `FakeLLMClient` pre-registered for every step.
  - Run with a toy research question.
  - Assert the final state has: 1 hypothesis, 1 design, 1 result, ≥ 4 reasoning traces, ≥ 4 verification results, all with consistency_score >= 0.7.
- `test_graph_hitl_pause.py` — force a verifier failure on `design`. Assert the graph reaches the `hitl` node and surfaces a `HITLRequest`. Resume via `Command(resume=...)` and assert it completes.
- `test_routing.py` — unit tests for `route_after`: returns "next" on clean state, "hitl" when `hitl_pending` is set, "retry" when monitor says RETRY.
- `test_recovery.py` — `with_retries` succeeds after N-1 transient failures; surfaces the original exception when all attempts fail.

## DoD

1. End-to-end test passes (no HITL pause version).
2. HITL pause + resume test passes.
3. Routing unit tests pass.
4. `Checkpointer` adapter wired to `MemorySaver` (LangGraph's built-in) and our `InMemoryCheckpointer`.
5. `make lint typecheck test` all green.

## Risks

- **LangGraph API churn.** I'll bind to the `StateGraph` + `interrupt()` API; if `langgraph>=0.2` has shifted in the installed version, will adapt minimally.
- **Async + interrupt semantics** can produce confusing test failures. Mitigation: keep the test path synchronous via `.invoke()` rather than `.astream()` where possible.
- **Conditional edges** must return values present in the mapping or LangGraph raises. We'll always return one of `{"next", "hitl", "retry", "end"}`.
