# AutoLab — Master Plan

> Living index of all phase plans. Updated at the end of each phase with what shipped, what slipped, and what to watch for next.

This session is operating in **autonomous mode**: I plan the full arc here, then execute phase-by-phase without asking for confirmation on routine decisions. I pause only if blocked or if a requirement is genuinely ambiguous. Each phase still gets its own concrete plan in `docs/plans/phase_N.md`, and each phase ends with a commit + push and a status update in `docs/status.md`.

---

## Constraints discovered up front

- **No `ANTHROPIC_API_KEY` in the container.** All LLM-touching code must work behind a `LLMClient` protocol with a deterministic fake; live tests gated on `RUN_LIVE_TESTS=1`.
- **No running Postgres / Redis service** in the dev container. Phase 1 uses SQLite for the SQLAlchemy layer in tests (via `aiosqlite`) and Postgres URLs are accepted but exercised only when `RUN_LIVE_TESTS=1`. Same approach for Redis: an in-memory checkpointer for tests, real Redis behind a flag.
- **No real lab hardware, ever** in this codebase — simulator only (CLAUDE.md §9).
- **CI must run in <5 min.** Heavy deps (`chromadb`, full `streamlit` stack) move to optional extras the moment they slow CI.

These are documented as ADRs as they're decided.

---

## Phase order & status

| Phase | Title | Plan | Status |
|---|---|---|---|
| 0 | Foundation | `phase_0.md` | ✅ done |
| 1 | Data models & state | `phase_1.md` | ✅ done |
| 2 | VeriCoT verification engine ⭐ | `phase_2.md` | ✅ done |
| 3 | Individual agents (5) | `phase_3.md` | ✅ done |
| 4 | Orchestration | `phase_4.md` | ✅ done |
| 5 | Tools & integrations | `phase_5.md` | pending |
| 6 | Monitoring & dashboard | `phase_6.md` | pending |
| 7 | Benchmark & hardening | `phase_7.md` | pending |

## High-level arc

- **Phase 0** — green skeleton: `make install lint typecheck test` all pass, CI configured. No business logic.
- **Phase 1** — Pydantic core models + `AutoLabState` TypedDict + SQLAlchemy persistence (SQLite in tests, Postgres-ready) + an in-memory checkpointer adapter that mirrors the Redis one's interface. Round-trip serialization tests + "resume after simulated crash" test.
- **Phase 2** ⭐ — VeriCoT pipeline. Build behind an `LLMClient` protocol so the formalizer is testable without network. TDD-first. Twenty golden traces (10 valid, 10 invalid) committed as fixtures. Solver wrapper around Z3. Aggregation produces `VerificationResult`. ≥90% coverage on `verification/`.
- **Phase 3** — Agents in order: Research → Design → Execute → Analysis → Verification Monitor. Each extends `BaseAgent` which enforces produce → verify → state-update. Mocked LLM in unit tests; live tests gated.
- **Phase 4** — LangGraph `StateGraph` wiring, conditional edges on verifier verdict, `interrupt()` HITL nodes, retry/backoff recovery. End-to-end test: toy question through all 5 agents with at least one HITL pause and a resume.
- **Phase 5** — arxiv + PubMed clients with caching; `LabSimulator` implementing a `HardwareAdapter` protocol (liquid handler, plate reader, simple imaging) with realistic noise; multimodal RAG over chromadb.
- **Phase 6** — Streamlit dashboard with live state view, trace timeline, verification verdicts, HITL queue. Trace persistence + metrics.
- **Phase 7** — Three benchmark scenarios + error-injection harness; show ≥80% catch rate; hardening pass.

## Working agreements for this session

1. Plan files in `docs/plans/phase_N.md` are written **before** code for that phase.
2. Each phase ends with: green gates locally, a single conventional commit, push to `claude/plan-execute-modes-PT67G`, and a status update in `docs/status.md`.
3. ADRs go in `docs/decisions/` whenever a non-obvious technical choice is made.
4. If a requirement is ambiguous, I stop and ask. Routine decisions (naming, file layout within a module, test structure) are made without asking.
