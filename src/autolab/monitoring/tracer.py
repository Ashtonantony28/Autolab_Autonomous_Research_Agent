"""Persist AutoLab state events into the StateStore.

Thin glue between an `AutoLabState` snapshot and the persistence layer.
Idempotent enough to call after every agent: we append each new entity
once. Detecting "new" is by id — the tracer keeps a per-run set of ids
already persisted, and the store upserts by `(run_id, id)`, so a fresh tracer after a
restart does not duplicate rows either.

`record_node` is what the graph calls after every agent execution: it
persists the run row, any new entities and one `TraceEvent`.
"""

from __future__ import annotations

from uuid import UUID

from autolab.monitoring.events import TraceEvent
from autolab.state.schema import AutoLabState
from autolab.state.store import StateStore


class RunTracer:
    """Persist agent outputs from an evolving `AutoLabState`."""

    def __init__(self, store: StateStore) -> None:
        self._store = store
        # Keyed by (run_id, entity_type, entity_id) so a VerificationResult
        # doesn't collide with the ReasoningTrace it references.
        self._seen: set[tuple[UUID, str, UUID]] = set()
        self._runs: set[UUID] = set()

    async def record_node(
        self, event: TraceEvent, research_question: str, *states: AutoLabState
    ) -> None:
        """Persist one agent execution: its run row, new entities in `states`, then `event`."""
        if event.run_id not in self._runs:
            await self._store.ensure_run(event.run_id, research_question)
            self._runs.add(event.run_id)
        for state in states:
            await self.record_state(event.run_id, state)
        await self._store.append(event.run_id, event)

    async def record_state(self, run_id: UUID, state: AutoLabState) -> int:
        """Append everything in `state` that we haven't yet persisted.

        Returns the number of entities newly persisted.
        """
        appended = 0

        for h in state.get("hypotheses", []) or []:
            if self._mark(run_id, "hypothesis", h.id):
                await self._store.append(run_id, h)
                appended += 1

        current = state.get("current_experiment")
        if current is not None and self._mark(run_id, "design", current.id):
            await self._store.append(run_id, current)
            appended += 1

        for r in state.get("results", []) or []:
            if self._mark(run_id, "result", r.id):
                await self._store.append(run_id, r)
                appended += 1

        for t in state.get("reasoning_traces", []) or []:
            if self._mark(run_id, "trace", t.id):
                await self._store.append(run_id, t)
                appended += 1

        for v in state.get("verification_history", []) or []:
            # Identified by trace_id (VerificationResult has no own UUID).
            if self._mark(run_id, "verification", v.trace_id):
                await self._store.append(run_id, v)
                appended += 1

        for a in state.get("analysis_reports", []) or []:
            if self._mark(run_id, "report", a.id):
                await self._store.append(run_id, a)
                appended += 1

        return appended

    def _mark(self, run_id: UUID, kind: str, entity_id: UUID) -> bool:
        """Return True if this entity is newly seen (and record it)."""
        key = (run_id, kind, entity_id)
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


__all__ = ["RunTracer"]
