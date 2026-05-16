"""Streamlit dashboard for AutoLab runs.

The actual `streamlit` import is lazy (inside `render`) so this module can
be imported in tests without requiring the dashboard extra.
"""

from __future__ import annotations

from autolab.state.models import ReasoningTrace, VerificationResult
from autolab.state.schema import AutoLabState


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


def trace_count(state: AutoLabState) -> int:
    """Convenience: number of reasoning traces recorded so far."""
    return len(state.get("reasoning_traces") or [])


def verification_summary(verdict: VerificationResult) -> str:
    return f"{verdict.summary} (score={verdict.consistency_score:.2f})"


def trace_label(trace: ReasoningTrace) -> str:
    return f"{trace.agent}#{trace.id.hex[:6]}"


__all__ = [
    "render",
    "render_trace_table",
    "trace_count",
    "trace_label",
    "verification_summary",
]
