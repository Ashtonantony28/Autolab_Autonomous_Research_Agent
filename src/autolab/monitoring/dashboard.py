"""Streamlit dashboard for AutoLab runs.

The actual `streamlit` import is lazy (inside `render`) so this module can
be imported in tests without requiring the dashboard extra.

`scripts/dashboard.py` (`make dashboard`) renders a persisted run: `load_run`
rebuilds it from the `StateStore` and `render_run` draws it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from autolab.monitoring.events import TraceEvent
from autolab.state.models import ReasoningTrace, VerificationResult
from autolab.state.schema import AutoLabState
from autolab.state.store import StateStore

EVENT_COLUMNS = ["node", "attempt", "latency_ms", "score", "decision", "trigger", "reason"]


@dataclass(frozen=True)
class RunView:
    """A persisted run: its entities as an `AutoLabState` plus its trace events."""

    run_id: UUID
    state: AutoLabState
    events: list[TraceEvent]


async def load_run(store: StateStore, run_id: UUID) -> RunView:
    """Rebuild a run from the store (what the dashboard shows)."""
    state = AutoLabState(
        research_question=dict(await store.list_runs()).get(run_id, ""),
        hypotheses=await store.load_hypotheses(run_id),
        reasoning_traces=await store.load_traces(run_id),
        verification_history=await store.load_verifications(run_id),
        analysis_reports=await store.load_reports(run_id),
    )
    return RunView(run_id=run_id, state=state, events=await store.load_events(run_id))


def render_event_table(events: list[TraceEvent]) -> list[list[str]]:
    """Return a header + one row per agent execution, in order."""
    rows = [list(EVENT_COLUMNS)]
    for e in events:
        score = "-" if e.consistency_score is None else f"{e.consistency_score:.2f}"
        rows.append(
            [
                e.node,
                str(e.attempt),
                f"{e.latency_ms:.1f}",
                score,
                e.decision,
                e.trigger or "",
                e.hitl_reason or "",
            ]
        )
    return rows


def render_trace_table(state: AutoLabState) -> list[list[str]]:
    """Return a header + rows view of every step in every reasoning trace.

    Pure data so tests can assert without invoking Streamlit. Columns:
    `agent`, `step`, `claim_type`, `claim`, `verdict`, `details`.
    """
    rows: list[list[str]] = [["agent", "step", "claim_type", "claim", "verdict", "details"]]
    verdicts_by_trace = {v.trace_id: v for v in (state.get("verification_history") or [])}
    for trace in state.get("reasoning_traces") or []:
        verdict = verdicts_by_trace.get(trace.id)
        for step in trace.steps:
            sv = (
                next((sv for sv in verdict.step_verdicts if sv.step_index == step.index), None)
                if verdict
                else None
            )
            rows.append(
                [
                    trace.agent,
                    str(step.index),
                    step.claim_type,
                    step.claim,
                    sv.verdict if sv else "-",
                    sv.details if sv else "",
                ]
            )
    return rows


def render(state: AutoLabState) -> None:  # pragma: no cover - streamlit-dependent
    """Render the live AutoLab dashboard.

    Runs only when invoked under `streamlit run ...`. Streamlit is a
    `dashboard` optional extra; the import is lazy so this module is
    safe to import in core test runs.
    """
    import streamlit as st

    st.title("AutoLab — Live Run")
    st.write(f"**Research question:** {state.get('research_question', '<none>')}")
    st.write(f"**Current agent:** {state.get('current_agent', '<idle>')}")
    st.write(f"**Confidence:** {state.get('confidence', 0.0):.2f}")

    hitl = state.get("hitl_pending")
    if hitl is not None:
        st.error(f"HITL pending: {hitl.agent} — {hitl.reason}")

    st.subheader("Reasoning traces")
    rows = render_trace_table(state)
    st.table(rows)


def render_run(view: RunView) -> None:  # pragma: no cover - streamlit-dependent
    """Render one persisted run: decisions, hypotheses, reports and verified traces."""
    import streamlit as st

    st.title("AutoLab — Run audit")
    st.write(f"**Run:** `{view.run_id}`")
    st.write(f"**Research question:** {view.state.get('research_question') or '<unknown>'}")
    st.subheader("Agent decisions")
    st.table(render_event_table(view.events))
    st.subheader("Hypotheses")
    for h in view.state.get("hypotheses", []):
        st.write(f"- {h.statement} (confidence={h.confidence:.2f})")
    st.subheader("Analysis")
    for report in view.state.get("analysis_reports", []):
        st.write(report.summary)
    st.subheader("Reasoning traces")
    st.table(render_trace_table(view.state))


def main(database_url: str) -> None:  # pragma: no cover - streamlit-dependent
    """Streamlit page: pick a persisted run (newest first) and render it."""
    import streamlit as st

    async def runs() -> list[tuple[UUID, str]]:
        store = await StateStore.create(database_url)
        try:
            return await store.list_runs()
        finally:
            await store.dispose()

    async def load(run_id: UUID) -> RunView:
        store = await StateStore.create(database_url)
        try:
            return await load_run(store, run_id)
        finally:
            await store.dispose()

    available = asyncio.run(runs())
    if not available:
        st.info(f"No runs recorded in {database_url}.")
        return
    labels = {f"{q} ({rid})": rid for rid, q in available}
    choice = st.selectbox("Run", list(labels))
    render_run(asyncio.run(load(labels[choice])))


def trace_count(state: AutoLabState) -> int:
    """Convenience: number of reasoning traces recorded so far."""
    return len(state.get("reasoning_traces") or [])


def verification_summary(verdict: VerificationResult) -> str:
    return f"{verdict.summary} (score={verdict.consistency_score:.2f})"


def trace_label(trace: ReasoningTrace) -> str:
    return f"{trace.agent}#{trace.id.hex[:6]}"


__all__ = [
    "EVENT_COLUMNS",
    "RunView",
    "load_run",
    "main",
    "render",
    "render_event_table",
    "render_run",
    "render_trace_table",
    "trace_count",
    "trace_label",
    "verification_summary",
]
