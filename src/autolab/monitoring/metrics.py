"""In-process per-agent metrics.

A Prometheus-compatible exporter can be layered on top later; for now we
keep counters in a plain dataclass so tests don't depend on a metrics SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentMetrics:
    """Per-agent aggregate metrics."""

    invocations: int = 0
    total_latency_ms: float = 0.0
    sum_consistency_score: float = 0.0
    hitl_escalations: int = 0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.invocations if self.invocations else 0.0

    @property
    def avg_consistency_score(self) -> float:
        return self.sum_consistency_score / self.invocations if self.invocations else 0.0


@dataclass
class Metrics:
    """Collection of per-agent metrics, keyed by agent name."""

    by_agent: dict[str, AgentMetrics] = field(default_factory=dict)

    def observe_agent(
        self,
        name: str,
        *,
        latency_ms: float,
        consistency_score: float,
        escalated: bool,
    ) -> None:
        m = self.by_agent.setdefault(name, AgentMetrics())
        m.invocations += 1
        m.total_latency_ms += latency_ms
        m.sum_consistency_score += consistency_score
        if escalated:
            m.hitl_escalations += 1

    def snapshot(self) -> dict[str, dict[str, float]]:
        """Return a JSON-friendly snapshot."""
        return {
            name: {
                "invocations": float(m.invocations),
                "avg_latency_ms": m.avg_latency_ms,
                "avg_consistency_score": m.avg_consistency_score,
                "hitl_escalations": float(m.hitl_escalations),
            }
            for name, m in self.by_agent.items()
        }


__all__ = ["AgentMetrics", "Metrics"]
