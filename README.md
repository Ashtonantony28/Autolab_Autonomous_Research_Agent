# AutoLab

Autonomous research agent with **verifiable** chain-of-thought reasoning.

Every reasoning step is formalized into first-order logic and verified by an SMT solver (Z3) before downstream agents can act on it. Five specialized agents — Research, Design, Execute, Analysis, and a Verification Monitor — are coordinated by a LangGraph orchestrator with human-in-the-loop checkpoints.

> **Status:** Phase 0 — foundation skeleton. See `docs/plans/MASTER.md` for the full roadmap and `docs/plans/phase_N.md` for each phase's concrete plan.

## Quickstart

```bash
# Install (uv-managed)
make install

# Lint + typecheck + test
make lint
make typecheck
make test

# Smoke-run the CLI
make run
```

## Layout

See `docs/architecture.md` and `CLAUDE.md` (the operating contract) for full detail.

```
src/autolab/
├── state/           # Pydantic models + LangGraph TypedDict + persistence
├── verification/    # VeriCoT — Z3-backed reasoning verifier
├── agents/          # Research, Design, Execute, Analysis, VerificationMonitor
├── orchestrator/    # LangGraph StateGraph, routing, HITL checkpoints
├── tools/           # Literature, lab hardware (simulator), RAG, simulation
└── monitoring/      # Tracing, metrics, Streamlit dashboard
```

## Development

- **Python:** 3.11+
- **Package manager:** `uv`
- **Quality gates:** `ruff` (lint + format), `mypy --strict`, `pytest`
- **CI:** GitHub Actions on every push — lint + typecheck + test must pass.
