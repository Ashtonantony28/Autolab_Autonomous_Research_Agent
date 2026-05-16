# Phase 1 — Data Models & State Plan

**Goal:** Pydantic core models + LangGraph `AutoLabState` TypedDict with reducers + async SQLAlchemy persistence + a checkpointer abstraction. Round-trip serialization and "resume after simulated crash" tests must pass.

**Out of scope:** any agent logic, any LLM calls, the VeriCoT verifier. This is data + persistence only.

---

## 1. Modules to implement

### `src/autolab/state/models.py` — Pydantic v2 models
All `frozen=True` value types use `model_config = ConfigDict(frozen=True, extra="forbid")` so they're hashable and don't silently swallow typos. Mutable container types (state) use `extra="forbid"` without freezing.

- `Citation` — `source: Literal["arxiv","pubmed","manual"]`, `identifier`, `title`, `url`, `year`.
- `Hypothesis` — `id: UUID`, `question: str`, `statement: str`, `rationale: str`, `citations: list[Citation]`, `confidence: float in [0,1]`, `created_at: datetime`.
- `ExperimentParameter` — `name: str`, `value: float | int | str | bool`, `unit: str | None`.
- `ExperimentDesign` — `id`, `hypothesis_id`, `protocol: str`, `parameters: list[ExperimentParameter]`, `estimated_cost_usd: float`, `estimated_duration_minutes: float`, `success_criteria: list[str]`.
- `Measurement` — `name`, `value: float`, `unit`, `timestamp`.
- `ExperimentResult` — `id`, `design_id`, `measurements: list[Measurement]`, `raw_artifacts: list[str]` (paths/URIs), `status: Literal["success","partial","failure"]`, `notes`.
- `ReasoningStep` — `index: int`, `claim: str`, `claim_type: Literal["premise","inference","conclusion"]`, `referenced_step_indices: list[int]`.
- `ReasoningTrace` — `id`, `agent: str`, `steps: list[ReasoningStep]`, `produced_at: datetime`.
- `StepVerdict` — `step_index`, `verdict: Literal["VALID","INVALID","UNGROUNDED"]`, `details: str`.
- `VerificationResult` — `trace_id`, `step_verdicts: list[StepVerdict]`, `consistency_score: float in [0,1]`, `summary: str`.
- `ConfidenceScore` — `value: float in [0,1]`, `basis: str`.
- `HITLRequest` — `id`, `agent`, `reason`, `payload: dict[str, Any]`, `created_at`.

Validators: numeric ranges, `referenced_step_indices` must be < `index`, `step_verdicts` must reference indices that exist in the trace, IDs default to `uuid4()` via `default_factory`.

### `src/autolab/state/schema.py` — `AutoLabState` TypedDict
Exactly the shape from CLAUDE.md §7 Pattern A, with `Annotated[..., operator.add]` for list fields. Add `current_agent: str` and `confidence: float` per the brief. Reducers are pure; immutable models inside.

### `src/autolab/state/store.py` — Async SQLAlchemy persistence
- `Base = declarative_base()`.
- ORM tables: `runs` (root entity for one research question), `hypotheses`, `experiment_designs`, `experiment_results`, `reasoning_traces`, `verification_results`, `hitl_requests`. Each stores its Pydantic model as a JSONB column (SQLite gets `JSON`) plus a few projected columns for querying (id, run_id, created_at, status).
- `StateStore` async class with `async create_engine(...)`, `save_run(state)`, `load_run(run_id)`, plus per-entity append helpers.
- Engine is constructed from `Settings.database_url`.

### `src/autolab/state/checkpointer.py` (new) — Checkpointer abstraction
The brief calls for a Redis-backed LangGraph checkpointer. Since Redis isn't running here, I'll:
1. Define a small `Checkpointer` protocol (`put(thread_id, state) -> None`, `get(thread_id) -> AutoLabState | None`, `list(thread_id) -> list[Checkpoint]`).
2. Provide `InMemoryCheckpointer` (a dict, used in tests) and `RedisCheckpointer` (real implementation). Both implement the protocol.
3. Defer the actual `langgraph.checkpoint` integration to Phase 4 — for Phase 1 we only need the abstraction so persistence + resume semantics are testable now.

### `alembic/` setup
- `alembic init` produces a stock layout; we wire it to read `DATABASE_URL` from `Settings`.
- First revision: create all the tables.
- For tests we use `Base.metadata.create_all` on an in-memory SQLite — Alembic is exercised only when targeting Postgres.

---

## 2. Tests

- `tests/unit/test_models.py`
  - Each model round-trips through `model_dump_json()` → `model_validate_json()`.
  - Validator failures: out-of-range confidence, missing required field, `referenced_step_indices` pointing forward.
  - Hypothesis property test (Hypothesis): random valid inputs round-trip.
- `tests/unit/test_state_reducers.py`
  - `Annotated[..., add]` reducers append correctly; multiple updates merge as expected.
- `tests/integration/test_store.py`
  - In-memory SQLite engine, create tables, save a `Run` with three hypotheses + one trace + one verification result, load back, assert equality field-by-field.
  - Use `pytest_asyncio` for async fixtures.
- `tests/integration/test_checkpointer_resume.py`
  - Build an `AutoLabState` with one hypothesis. Snapshot via `InMemoryCheckpointer.put`. Simulate "crash" by dropping the in-process state. Restore via `.get(thread_id)`. Assert deep equality. Repeat with `RedisCheckpointer` only if `RUN_LIVE_TESTS=1`.

Coverage target for `state/`: ≥80% (the §4 ≥70% bar with margin).

---

## 3. Commands

```bash
uv run alembic init alembic                 # only if not already initialized
make lint typecheck test                    # all green
git add -A && git commit -m "feat: phase 1 — data models, state schema, persistence"
git push origin claude/plan-execute-modes-PT67G
```

---

## 4. Definition of Done

1. All Pydantic models defined with strict validation.
2. `AutoLabState` TypedDict with `Annotated` reducers.
3. SQLAlchemy async ORM + tables, exercised against in-memory SQLite.
4. `Checkpointer` protocol + `InMemoryCheckpointer` working; `RedisCheckpointer` implemented but only exercised under `RUN_LIVE_TESTS=1`.
5. Round-trip + resume tests pass. `make lint typecheck test` all green.
6. ADR-0003 documenting the SQLite-in-tests / Postgres-in-prod decision.
7. ADR-0004 documenting why LangGraph checkpoint integration is deferred to Phase 4.

---

## 5. Risks

- `mypy --strict` + Pydantic generics can produce noisy errors around `Annotated[list[...], add]`. Mitigation: keep type aliases simple, use `typing_extensions.TypedDict` if needed.
- SQLAlchemy 2.0 async + SQLite in-memory has a known caveat: each connection sees its own DB. Mitigation: use `StaticPool` so the in-memory DB is shared across connections within a test.
- `filterwarnings = ["error"]` may catch deprecation noise from SQLAlchemy/Pydantic. Will whitelist specific patterns rather than relax globally.
