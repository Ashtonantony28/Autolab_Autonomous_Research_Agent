# ADR-0004: Defer LangGraph checkpoint integration to Phase 4

- **Status:** Accepted
- **Date:** 2026-05-16

## Context

CLAUDE.md §6 Phase 1 asks for a "Redis-backed checkpointer for LangGraph." LangGraph's `BaseCheckpointSaver` API is non-trivial — it tracks per-node intermediate state and replay tokens, and only makes sense once a real `StateGraph` exists. In Phase 1 we don't have a graph yet.

## Decision

Phase 1 ships a small `Checkpointer` protocol (`put` / `get` by thread id) with two implementations:

- `InMemoryCheckpointer` — dict, used by tests and single-process dev runs.
- `RedisCheckpointer` — async Redis client, exercised under `RUN_LIVE_TESTS=1`.

This is **not** a LangGraph `BaseCheckpointSaver`. The "resume after crash" semantics required by Phase 1's DoD are validated by snapshotting and restoring an `AutoLabState`, which is exactly what an outer-loop checkpointer would do.

Phase 4 will adapt our protocol to LangGraph's `BaseCheckpointSaver` once the graph wiring lands. The protocol is intentionally narrow so the adapter is straightforward.

## Consequences

- Phase 1 is testable end-to-end without LangGraph internals.
- The Phase 4 plan must include the adapter work explicitly (will be noted there).
- If LangGraph's checkpoint API changes shape before Phase 4, we revisit then — the protocol is small, so the blast radius is minimal.
