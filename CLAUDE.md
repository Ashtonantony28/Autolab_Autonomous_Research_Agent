# AutoLab: Autonomous Research Agent — Claude Code Project Brief

> **Read this entire document before writing any code.** This is your operating contract for building AutoLab. Refer back to it before each phase.

---

## 1. Mission

You are building **AutoLab**, a production-grade autonomous research platform that combines multi-agent orchestration with **verifiable chain-of-thought reasoning**. Unlike pure agentic systems that hallucinate experimental plans, AutoLab uses neuro-symbolic verification (VeriCoT methodology) and human-in-the-loop checkpoints to produce *trustworthy* autonomous science.

**The core differentiator:** Every reasoning step is formalized into first-order logic and verified by an SMT solver (Z3) before downstream agents can act on it.

**Target user:** A research scientist who wants AI to design, run, and analyze experiments — but needs to *trust and audit* every decision.

---

## 2. Architecture (5 Agents + Orchestrator + Verifier)

```
                 ┌──────────────────────────────┐
                 │   Master Orchestrator        │
                 │   (LangGraph StateGraph)     │
                 │   - Routes tasks             │
                 │   - Manages HITL checkpoints │
                 │   - Persists state           │
                 └──────────┬───────────────────┘
                            │
        ┌──────────┬────────┼────────┬──────────┬────────────────┐
        ▼          ▼        ▼        ▼          ▼                ▼
  ┌──────────┐ ┌───────┐ ┌──────┐ ┌────────┐ ┌──────────────────┐
  │ Research │ │Design │ │Execute│ │Analysis│ │ Verification     │
  │  Agent   │ │ Agent │ │ Agent │ │ Agent  │ │ Monitor (VeriCoT)│
  └──────────┘ └───────┘ └──────┘ └────────┘ └──────────────────┘
        │          │        │        │              │
        └──────────┴────────┴────────┴──────────────┘
                            │
                            ▼
                 ┌──────────────────────┐
                 │  Shared State Store  │
                 │  (Postgres + Redis)  │
                 └──────────────────────┘
```

The **Verification Monitor is non-bypassable**: every agent output passes through it. If verification fails (logical inconsistency, ungrounded premises, contradictions with prior state), the orchestrator either retries with feedback or escalates to a human.

---

## 3. Technology Stack (Locked Choices)

Use **exactly** these. Do not substitute without raising the question:

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Type hints, async, mature LLM ecosystem |
| LLM SDK | `anthropic` (Claude Opus 4.7 for orchestrator, Sonnet 4.6 for agents) | Native tool use, structured outputs |
| Orchestration | `langgraph` >= 0.2 | TypedDict state, checkpointing, HITL |
| Validation | `pydantic` v2 | Strict structured outputs from LLMs |
| Symbolic solver | `z3-solver` | First-order logic / SMT-LIB verification |
| Vector DB | `chromadb` (local) or `qdrant` (prod) | Multimodal RAG |
| Database | `postgresql` + `sqlalchemy` 2.0 | Execution traces, persistent memory |
| Shared state | `redis` | Agent communication, checkpoints |
| API | `fastapi` + `uvicorn` | Tool gateways, lab hardware mocks |
| Dashboard | `streamlit` | Reasoning trace visualization |
| Testing | `pytest` + `pytest-asyncio` + `hypothesis` | Unit + property-based |
| Tracing | `langsmith` (or OpenTelemetry) | Agent observability |
| Package mgmt | `uv` | Fast, reproducible installs |
| Lint/format | `ruff` + `mypy --strict` | Quality gate |

**Do NOT add a dependency** without justifying it in a comment in `pyproject.toml`.

---

## 4. Development Principles (Non-Negotiable)

1. **Plan before code.** Every phase starts with a written plan in `docs/plans/phase_N.md` that I review before you write implementation code. Show me the plan, wait for approval keyword "proceed", then implement.

2. **TDD for the verification engine.** The VeriCoT verifier (Phase 2) is the *foundation of trust*. Write tests first. Aim for >90% coverage on `verification/`. The rest of the codebase: pragmatic testing, ≥70% coverage on core logic.

3. **Strict typing everywhere.** `mypy --strict` must pass on every commit. No `Any` types except at LLM-output parsing boundaries (and immediately validated by Pydantic).

4. **Structured LLM outputs only.** Every LLM call uses Pydantic schemas via Anthropic's tool-use / structured-output mechanism. Never parse free-form prose into critical state.

5. **State is explicit, never hidden.** Use LangGraph's `TypedDict` state schema with `Annotated` reducers. No global mutable state, no hidden agent memory outside the state graph.

6. **Idempotent and resumable.** Every agent action must be safe to retry. Use LangGraph checkpointing so a crash mid-experiment can resume without re-running expensive steps.

7. **Human-in-the-loop is the default, not a fallback.** Insert `interrupt()` checkpoints before: (a) any agent decision with confidence < 0.7, (b) any action with cost > $10 or duration > 1 hour, (c) any hypothesis that contradicts verified prior state.

8. **Reasoning is observable.** Every decision writes a structured trace: inputs, reasoning steps, verification result, confidence, downstream effect. These traces are the product, not a byproduct.

9. **Confirm before destructive actions.** Before deleting files, dropping tables, force-pushing, or calling external services that send messages/run experiments: ask first. Reversible local edits are fine without asking.

10. **Read before you write.** Always `view` existing files before editing. Use `grep` / search before assuming code exists or doesn't exist.

---

## 5. Project Structure

Create this layout in Phase 0. Stick to it.

```
autolab/
├── pyproject.toml              # uv-managed, locked deps
├── README.md                   # Quickstart, architecture summary
├── CLAUDE.md                   # This file (symlink or copy)
├── .env.example                # ANTHROPIC_API_KEY, DATABASE_URL, etc.
├── docs/
│   ├── architecture.md         # System design rationale
│   ├── plans/                  # Phase plans (phase_0.md, phase_1.md...)
│   └── decisions/              # ADRs (Architecture Decision Records)
├── src/autolab/
│   ├── __init__.py
│   ├── config.py               # Pydantic Settings, env loading
│   ├── state/
│   │   ├── schema.py           # TypedDict AutoLabState + reducers
│   │   ├── models.py           # Pydantic: Hypothesis, Experiment, Result, ReasoningTrace
│   │   └── store.py            # Postgres + Redis persistence
│   ├── verification/           # PHASE 2 — BUILD FIRST
│   │   ├── vericot.py          # Main pipeline
│   │   ├── formalizer.py       # NL → SMT-LIB (uses Claude)
│   │   ├── premise_grounder.py # Premise extraction & grounding
│   │   ├── solver.py           # Z3 wrapper, entailment checks
│   │   └── confidence.py       # Confidence scoring
│   ├── agents/
│   │   ├── base.py             # BaseAgent ABC with verify_output hook
│   │   ├── research.py
│   │   ├── design.py
│   │   ├── execute.py
│   │   ├── analysis.py
│   │   └── verification_monitor.py
│   ├── orchestrator/
│   │   ├── graph.py            # LangGraph StateGraph definition
│   │   ├── routing.py          # Conditional edges, agent dispatch
│   │   ├── checkpoints.py      # HITL interrupt logic
│   │   └── recovery.py         # Error handling, retries
│   ├── tools/
│   │   ├── literature.py       # arxiv, PubMed (read-only APIs)
│   │   ├── lab_hardware/       # Mock simulator + real hardware adapter interface
│   │   ├── rag/                # Multimodal retrieval
│   │   └── simulation/         # Dry-run environments
│   ├── monitoring/
│   │   ├── tracer.py           # Structured execution traces
│   │   ├── metrics.py          # Prometheus-compatible counters
│   │   └── dashboard.py        # Streamlit app
│   └── cli.py                  # Entry point: `autolab run "<task>"`
├── tests/
│   ├── unit/                   # Mirrors src/ layout
│   ├── integration/
│   ├── e2e/                    # Full pipeline scenarios
│   └── fixtures/               # Sample hypotheses, mock LLM responses
└── scripts/
    ├── seed_db.py
    └── run_benchmark.py
```

---

## 6. Phased Development Plan

Work through phases **in order**. Each phase has a written plan (`docs/plans/phase_N.md`), implementation, tests, and a **Definition of Done** I must confirm before you proceed to the next phase.

### Phase 0 — Foundation (target: 1 session)
- Initialize repo, `pyproject.toml` with locked deps, `.env.example`, `.gitignore`.
- Set up `ruff`, `mypy --strict`, `pytest` configs.
- Create empty package skeleton matching Section 5.
- Add `Makefile` / `justfile` with: `make install`, `make test`, `make lint`, `make typecheck`, `make run`.
- Set up GitHub Actions CI: lint + typecheck + test on every push.
- **DoD:** `make install && make test && make lint && make typecheck` all pass on empty skeleton.

### Phase 1 — Data Models & State (target: 1 session)
- Pydantic models in `state/models.py`: `Hypothesis`, `ExperimentDesign`, `ExperimentResult`, `ReasoningStep`, `ReasoningTrace`, `VerificationResult`, `ConfidenceScore`.
- LangGraph `AutoLabState` TypedDict in `state/schema.py` with `Annotated` reducers (use `operator.add` for trace lists, `last_value` for current step).
- Postgres schema + SQLAlchemy 2.0 models. Migrations via Alembic.
- Redis-backed checkpointer for LangGraph.
- **DoD:** Round-trip serialization tests pass. State persists across simulated crash.

### Phase 2 — VeriCoT Verification Engine ⭐ FOUNDATION (target: 3-4 sessions)

This is the most important phase. Build it carefully, test it thoroughly.

The pipeline implements the VeriCoT methodology (arXiv:2511.04662):
1. **Extract CoT steps** from a reasoning trace (each step = NL sentence + claim type).
2. **Auto-formalize** each step into SMT-LIB first-order logic, using Claude with a strict schema. Maintain a growing vocabulary of predicates/constants across steps.
3. **Ground premises** — for each step, identify supporting premises from: (a) source context, (b) commonsense (LLM-as-judge), (c) prior verified steps.
4. **Solver check** — use Z3 to verify each step is entailed by its premises. Returns: `VALID`, `INVALID`, `UNGROUNDED`.
5. **Aggregate** into a `VerificationResult` with per-step verdicts and an overall `consistency_score ∈ [0,1]`.

Sub-tasks:
- Write the verifier with **tests first**. Test cases from ProofWriter / LegalBench style examples.
- Build a "golden" set of 20 hand-crafted reasoning traces (10 valid, 10 invalid) in `tests/fixtures/`. Verifier must classify them correctly.
- Edge cases: empty trace, single-step trace, traces with quantifiers, traces with arithmetic.
- Performance: a 10-step trace should verify in < 30 seconds.

**DoD:** ≥90% test coverage on `verification/`. All 20 golden traces classified correctly. mypy strict passes.

### Phase 3 — Individual Agents (target: 4-5 sessions)
Build one agent per session, in this order. Each agent extends `BaseAgent`, which enforces: structured Pydantic output → verification pass → state update.

- **Research Agent**: takes a research question, queries literature tools (arxiv, PubMed), produces a `Hypothesis` with grounded citations.
- **Design Agent**: takes a `Hypothesis`, produces an `ExperimentDesign` with protocol, parameters, cost/time estimate.
- **Execute Agent**: takes an `ExperimentDesign`, calls hardware tools (mock simulator in dev), produces raw `ExperimentResult`. Stream progress to state.
- **Analysis Agent**: takes `ExperimentResult`, produces interpretation with multiple ranked hypotheses for what the data means.
- **Verification Monitor**: wraps the VeriCoT engine, decides whether to: pass, retry with feedback, or escalate to human.

Each agent: unit tests with mocked LLM responses, integration test with real Anthropic API behind a `RUN_LIVE_TESTS=1` env flag.

**DoD per agent:** Produces validated output. Verification pass on golden inputs. Failure modes (LLM timeout, malformed output, tool error) handled gracefully.

### Phase 4 — Orchestration (target: 2 sessions)
- LangGraph `StateGraph` wiring all 5 agents.
- Conditional edges: route based on verification verdict (`pass → next agent`, `retry → same agent with feedback`, `escalate → human node`).
- HITL `interrupt()` nodes at the trigger points listed in Principle #7.
- Recovery: on transient errors (rate limit, network), exponential backoff. On permanent errors, escalate.

**DoD:** End-to-end test: run a toy research question through all 5 agents to completion, with at least one HITL pause. Resume after the pause completes successfully.

### Phase 5 — Tools & Integrations (target: 2-3 sessions)
- Literature: arxiv API + PubMed E-utilities, with RAG over results.
- Mock lab hardware: a `LabSimulator` class implementing a `HardwareAdapter` protocol. Simulates: liquid handler, plate reader, simple imaging. Inject realistic noise.
- Multimodal RAG: image + text retrieval using chromadb with CLIP embeddings.
- Define `HardwareAdapter` interface so real lab hardware can be plugged in later without code changes.

**DoD:** Each tool has its own integration tests. The simulator is realistic enough to surface real bugs in the agents.

### Phase 6 — Monitoring & Dashboard (target: 1-2 sessions)
- Streamlit dashboard showing: live agent state, reasoning traces, verification verdicts per step, HITL queue.
- Trace persistence: every action writes a structured event to Postgres.
- Metrics: per-agent latency, verification pass rate, HITL escalation rate, cost (tokens × price).

**DoD:** Dashboard renders a completed run end-to-end. A scientist can audit every decision in < 2 minutes per experiment.

### Phase 7 — Benchmark & Hardening (target: 2 sessions)
- Three benchmark scenarios in `scripts/run_benchmark.py`: (1) protein crystallization sweep, (2) compound activity screening, (3) materials parameter optimization. All against the simulator.
- Measure: hypothesis quality vs. ground truth, experiment efficiency, verification catch rate (inject deliberate reasoning errors, check they're caught), time-to-insight.
- Hardening: rate limiting, secrets management, structured logging, graceful shutdown.

**DoD:** Benchmark report (`docs/benchmarks/v1.md`) shows verification catches ≥80% of injected reasoning errors. All three scenarios complete without unhandled exceptions.

---

## 7. Critical Implementation Patterns

These are non-obvious — get them right the first time.

### Pattern A: LangGraph State Schema
```python
from typing import Annotated, TypedDict
from operator import add
from langgraph.graph import add_messages

class AutoLabState(TypedDict):
    research_question: str
    hypotheses: Annotated[list[Hypothesis], add]
    current_experiment: ExperimentDesign | None
    results: Annotated[list[ExperimentResult], add]
    reasoning_traces: Annotated[list[ReasoningTrace], add]
    verification_history: Annotated[list[VerificationResult], add]
    hitl_pending: HITLRequest | None
    current_agent: str
    confidence: float
```

### Pattern B: Verifier as a Decorator on Agent Outputs
```python
class BaseAgent(ABC):
    @abstractmethod
    async def _produce(self, state: AutoLabState) -> AgentOutput: ...

    async def __call__(self, state: AutoLabState) -> dict:
        output = await self._produce(state)
        trace = output.reasoning_trace
        verdict = await self.verifier.verify(trace, context=state)
        if verdict.consistency_score < 0.7:
            return {"hitl_pending": HITLRequest.from_verdict(verdict, output)}
        return {"reasoning_traces": [trace], "verification_history": [verdict], ...}
```

### Pattern C: VeriCoT Formalization with Vocabulary Growth
The formalizer must maintain a vocabulary (predicates, constants) that grows across steps. When formalizing step N, pass steps 1..N-1's vocabulary so symbols stay consistent. Don't re-formalize from scratch each call.

### Pattern D: HITL Interrupts
Use LangGraph's `interrupt()` primitive. The dashboard polls for `hitl_pending` and surfaces the request. Human responses are written back to state via `Command(resume=...)`.

### Pattern E: Structured Outputs from Claude
Always use the Anthropic SDK's tool-use mechanism for structured outputs:
```python
response = await client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=4096,
    tools=[{"name": "submit_hypothesis", "input_schema": Hypothesis.model_json_schema()}],
    tool_choice={"type": "tool", "name": "submit_hypothesis"},
    messages=[...]
)
hypothesis = Hypothesis.model_validate(response.content[0].input)
```
Never `json.loads()` free-form text.

---

## 8. Quality Standards

- **Tests must pass before any commit.** Use a pre-commit hook.
- **`mypy --strict` must pass.** Use `cast()` sparingly and only with a comment explaining why.
- **No silent failures.** Every `except` either re-raises, logs and escalates, or has a comment explaining why swallowing is correct.
- **Every public function has a docstring.** Args, returns, raises. One-liner is fine for trivial functions.
- **No magic numbers.** Constants in `config.py` or module-level `Final` types.
- **No hardcoded credentials, ever.** Use `pydantic-settings` reading from env.
- **Async-first.** All I/O (LLM calls, DB, HTTP) is async. Use `asyncio.gather` for parallelism.
- **Commit messages follow Conventional Commits.** `feat:`, `fix:`, `test:`, `docs:`, `refactor:`.

---

## 9. Anti-Patterns — Do NOT Do These

- ❌ Skipping the verifier "just for this one agent."
- ❌ Bundling multiple agents into a "super-agent" because orchestration feels heavy.
- ❌ Parsing LLM outputs with regex or string splitting.
- ❌ Storing agent reasoning in instance variables instead of explicit state.
- ❌ Catching `Exception` broadly. Catch specific exception types.
- ❌ Writing tests that mock so heavily they test nothing real.
- ❌ Adding a feature not in the phase plan because it "seems useful."
- ❌ Calling real lab hardware in Phases 0-6. Always go through the simulator until Phase 7.
- ❌ Hand-rolling JSON schema for Pydantic models. Use `.model_json_schema()`.
- ❌ Using `print()` for logging. Use the structured logger.

---

## 10. Working Style With Me

- **Start every session by reading this file.**
- **Ask before destructive operations** (deleting files, dropping schemas, `git push --force`).
- **Propose a plan, wait for "proceed"**, then implement. Don't ask permission for individual file edits inside the agreed plan.
- **When stuck for >10 minutes, stop and report.** Tell me what you tried, what you observed, and propose 2-3 paths forward.
- **Update `docs/decisions/` (ADRs)** whenever you make a non-obvious technical choice. Title, context, decision, consequences.
- **End each phase with a short summary**: what was built, what was deferred, what surprised you, what to watch for next phase.
- **Run `make lint typecheck test` before declaring a phase done.** Paste me the output.

---

## 11. First Steps (Run These Now)

1. Confirm you've read this entire document. Summarize back to me: the mission, the 5 agents, the verifier's role, and the phase order.
2. Create `docs/plans/phase_0.md` with your concrete plan for Phase 0 (the exact files you'll create, the exact commands you'll run, the success criteria). Stop after writing the plan.
3. Wait for me to say "proceed."

---

**Remember:** AutoLab's value is *trustworthy autonomy*. A faster but less verifiable system is a worse system. When in doubt, verify more, escalate sooner, and document the reasoning.
