"""Structured trace event: one per node execution or human decision (CLAUDE.md principle 8)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

NodeDecision = Literal["pass", "retry", "escalate", "approve", "abort"]
"""Where the run goes next.

For an agent node: the next agent (`pass`), the same agent (`retry`) or `hitl`
(`escalate`). For the `hitl` node: the human's `approve`, `retry` or `abort`,
or `escalate` when an approval re-pauses on the agent's policy.
"""


class TraceEvent(BaseModel):
    """What one agent execution or human decision did and how the orchestrator routed it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    node: str
    attempt: int = Field(ge=1)
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    latency_ms: float = Field(ge=0.0)
    trace_id: UUID | None = None
    consistency_score: float | None = None
    verdict_summary: str = ""
    decision: NodeDecision
    trigger: str | None = None
    hitl_reason: str | None = None
    agent: str | None = None
    """For a `hitl` event: the agent whose request was resolved."""
    note: str = ""
    """For a `hitl` event: the human's note."""


__all__ = ["NodeDecision", "TraceEvent"]
