"""Structured trace event: one per agent node execution (CLAUDE.md principle 8)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

NodeDecision = Literal["pass", "retry", "escalate"]
"""Where the monitor routes the execution: next agent, the same agent, or `hitl`."""


class TraceEvent(BaseModel):
    """What one agent execution did and how the orchestrator routed it."""

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


__all__ = ["NodeDecision", "TraceEvent"]
