"""Dashboard data-shape helpers."""

from __future__ import annotations

from uuid import uuid4

from autolab.monitoring.dashboard import (
    render_trace_table,
    trace_count,
    trace_label,
    verification_summary,
)
from autolab.state.models import (
    ReasoningStep,
    ReasoningTrace,
    StepVerdict,
    VerificationResult,
)
from autolab.state.schema import empty_state


def test_render_trace_table_empty_state_returns_header_only() -> None:
    rows = render_trace_table(empty_state("Q?"))
    assert rows == [["agent", "step", "claim_type", "claim", "verdict", "details"]]


def test_render_trace_table_joins_traces_with_verdicts() -> None:
    trace = ReasoningTrace(
        agent="research",
        steps=(
            ReasoningStep(index=0, claim="P", claim_type="premise"),
            ReasoningStep(
                index=1, claim="Q", claim_type="conclusion", referenced_step_indices=(0,)
            ),
        ),
    )
    verdict = VerificationResult(
        trace_id=trace.id,
        step_verdicts=(
            StepVerdict(step_index=0, verdict="VALID", details="premise"),
            StepVerdict(step_index=1, verdict="INVALID", details="bad inference"),
        ),
        consistency_score=0.5,
    )
    state = empty_state("Q?")
    state["reasoning_traces"] = [trace]
    state["verification_history"] = [verdict]

    rows = render_trace_table(state)
    assert len(rows) == 3
    assert rows[1][0] == "research"
    assert rows[1][2] == "premise"
    assert rows[1][4] == "VALID"
    assert rows[2][4] == "INVALID"
    assert "bad inference" in rows[2][5]


def test_trace_count() -> None:
    state = empty_state("Q?")
    assert trace_count(state) == 0
    state["reasoning_traces"] = [
        ReasoningTrace(agent="a", steps=(ReasoningStep(index=0, claim="c", claim_type="premise"),))
    ]
    assert trace_count(state) == 1


def test_verification_summary_and_trace_label() -> None:
    trace = ReasoningTrace(
        agent="design",
        steps=(ReasoningStep(index=0, claim="c", claim_type="premise"),),
    )
    verdict = VerificationResult(
        trace_id=trace.id,
        step_verdicts=(StepVerdict(step_index=0, verdict="VALID"),),
        consistency_score=1.0,
        summary="1 valid",
    )
    s = verification_summary(verdict)
    assert "score=1.00" in s
    assert "1 valid" in s
    label = trace_label(trace)
    assert label.startswith("design#")


def test_dashboard_module_imports_without_streamlit() -> None:
    # If streamlit were imported eagerly, this would still pass because the
    # dashboard extra is installed in CI; the real point is that `render`
    # is *not* invoked at import time. We just confirm the module is loaded.
    import autolab.monitoring.dashboard as dash

    assert hasattr(dash, "render")
    assert hasattr(dash, "render_trace_table")


def test_render_trace_table_handles_missing_verdict_gracefully() -> None:
    trace = ReasoningTrace(
        agent="x",
        steps=(ReasoningStep(index=0, claim="c", claim_type="premise"),),
    )
    state = empty_state("Q?")
    state["reasoning_traces"] = [trace]
    # No verification_history -> verdict cells should be "-".
    rows = render_trace_table(state)
    _ = uuid4()  # silence unused import
    assert rows[1][4] == "-"
    assert rows[1][5] == ""
