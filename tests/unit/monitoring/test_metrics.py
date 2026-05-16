"""Metrics aggregation."""

from __future__ import annotations

import pytest

from autolab.monitoring.metrics import AgentMetrics, Metrics


def test_initial_snapshot_is_empty() -> None:
    assert Metrics().snapshot() == {}


def test_observe_records_invocations() -> None:
    m = Metrics()
    m.observe_agent("research", latency_ms=100.0, consistency_score=0.9, escalated=False)
    m.observe_agent("research", latency_ms=200.0, consistency_score=0.7, escalated=False)
    snap = m.snapshot()
    assert snap["research"]["invocations"] == 2.0
    assert snap["research"]["avg_latency_ms"] == 150.0
    assert snap["research"]["avg_consistency_score"] == pytest.approx(0.8)
    assert snap["research"]["hitl_escalations"] == 0


def test_escalations_tracked() -> None:
    m = Metrics()
    m.observe_agent("design", latency_ms=10.0, consistency_score=0.5, escalated=True)
    m.observe_agent("design", latency_ms=10.0, consistency_score=0.9, escalated=False)
    assert m.snapshot()["design"]["hitl_escalations"] == 1.0


def test_empty_agent_metrics_avg_is_zero() -> None:
    am = AgentMetrics()
    assert am.avg_latency_ms == 0.0
    assert am.avg_consistency_score == 0.0
