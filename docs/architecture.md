# AutoLab Architecture

> Living document. Phase 0 captures the high-level intent; later phases fill in detail.

AutoLab is a five-agent autonomous research platform whose distinguishing property is **non-bypassable verification**: every reasoning step is auto-formalized into first-order logic and checked by an SMT solver (Z3) before downstream agents may act on it.

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

See `CLAUDE.md` for the full operating contract and `docs/plans/MASTER.md` for the phase-by-phase roadmap.
