# Phase 6 — Monitoring & Dashboard Plan

**Goal:** Trace persistence + per-agent metrics + a Streamlit dashboard skeleton. The dashboard itself can't be exercised interactively here, so the test focus is on the data layer — the dashboard is a thin shim over it.

## Modules

- `monitoring/tracer.py` — `RunTracer` async class wrapping `StateStore`. One call per agent event: `await tracer.record_agent(run_id, state)`. Persists hypotheses/designs/results/traces/verifications using the existing `append` API.
- `monitoring/metrics.py` — in-process counters (no Prometheus client yet — keeps deps light). `Metrics.observe_agent(name, latency_ms, verdict_score)` and `Metrics.snapshot()`.
- `monitoring/dashboard.py` — a real Streamlit app rendering a passed-in state. Avoids `if __name__ == "__main__"` so it can be imported in tests. Renders: research question, reasoning traces with per-step verdicts (color-coded), HITL pending if any, metrics table.

## Tests

- `tests/integration/monitoring/test_tracer.py` — round-trip: build an end-to-end state, hand to tracer, read back via the StateStore APIs.
- `tests/unit/monitoring/test_metrics.py` — counts + average latency tracked correctly.
- `tests/unit/monitoring/test_dashboard.py` — import-only smoke test (no streamlit runtime exercised), plus a `render_trace_table(state)` helper that returns a list-of-lists rendering — fully unit-testable.

## DoD

1. Trace persistence covers all four agent outputs (Hypothesis, Design, Result, Verification).
2. Metrics increment correctly under sequential agent runs.
3. Dashboard module imports without invoking Streamlit (lazy `import streamlit`).
4. `make lint typecheck test` all green.
