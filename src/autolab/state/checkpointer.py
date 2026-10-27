"""Checkpointer abstraction over AutoLab state.

The brief asks for a Redis-backed checkpointer for LangGraph. We define a
small `Checkpointer` protocol so the rest of the codebase doesn't care which
backend is in use. Phase 1 ships an in-memory implementation (used by tests
and dev runs) and a Redis implementation (exercised under RUN_LIVE_TESTS=1).
LangGraph's own checkpoint integration is wired in Phase 4 (ADR-0004);
`sqlite_saver` opens the durable LangGraph checkpointer that lets a run
paused at HITL resume in a new process.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol, runtime_checkable

import aiosqlite
import redis.asyncio as aioredis
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from autolab.state.schema import AutoLabState


@runtime_checkable
class Checkpointer(Protocol):
    """Persist and restore `AutoLabState` keyed by a thread id."""

    async def put(self, thread_id: str, state: AutoLabState) -> None: ...

    async def get(self, thread_id: str) -> AutoLabState | None: ...


class InMemoryCheckpointer:
    """Dict-backed checkpointer. Safe for tests and single-process dev runs."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def put(self, thread_id: str, state: AutoLabState) -> None:
        self._store[thread_id] = _dumps(state)

    async def get(self, thread_id: str) -> AutoLabState | None:
        blob = self._store.get(thread_id)
        return _loads(blob) if blob is not None else None


class RedisCheckpointer:
    """Redis-backed checkpointer. Each thread is one key."""

    def __init__(self, client: aioredis.Redis[str], *, namespace: str = "autolab:ckpt") -> None:
        self._client = client
        self._ns = namespace

    @classmethod
    def from_url(cls, url: str) -> RedisCheckpointer:
        return cls(aioredis.from_url(url, decode_responses=True))

    def _key(self, thread_id: str) -> str:
        return f"{self._ns}:{thread_id}"

    async def put(self, thread_id: str, state: AutoLabState) -> None:
        await self._client.set(self._key(thread_id), _dumps(state))

    async def get(self, thread_id: str) -> AutoLabState | None:
        raw = await self._client.get(self._key(thread_id))
        if raw is None:
            return None
        assert isinstance(raw, str)
        return _loads(raw)


@asynccontextmanager
async def sqlite_saver(path: str | Path) -> AsyncIterator[AsyncSqliteSaver]:
    """Open a LangGraph `AsyncSqliteSaver` on `path`, allowlisting the state models."""
    # Lazy: the orchestrator package imports `autolab.state`.
    from autolab.orchestrator.checkpoints import CHECKPOINT_TYPES

    async with aiosqlite.connect(str(path)) as conn:
        saver = AsyncSqliteSaver(
            conn, serde=JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)
        )
        await saver.setup()
        yield saver


def _dumps(state: AutoLabState) -> str:
    """Serialize an AutoLabState to JSON via each model's `.model_dump`."""
    serializable: dict[str, object] = {}
    for key, value in state.items():
        serializable[key] = _to_jsonable(value)
    return json.dumps(serializable)


def _loads(blob: str) -> AutoLabState:
    """Deserialize an AutoLabState from JSON.

    Pydantic is happy to re-validate dict payloads back into models. We import
    locally to keep the protocol module light.
    """
    from autolab.state.models import (
        AnalysisReport,
        ExperimentDesign,
        ExperimentResult,
        HITLRequest,
        Hypothesis,
        ReasoningTrace,
        VerificationResult,
    )

    data = json.loads(blob)
    state = AutoLabState()
    if "research_question" in data:
        state["research_question"] = data["research_question"]
    if "hypotheses" in data:
        state["hypotheses"] = [Hypothesis.model_validate(h) for h in data["hypotheses"]]
    if "current_experiment" in data:
        ce = data["current_experiment"]
        state["current_experiment"] = ExperimentDesign.model_validate(ce) if ce else None
    if "results" in data:
        state["results"] = [ExperimentResult.model_validate(r) for r in data["results"]]
    if "reasoning_traces" in data:
        state["reasoning_traces"] = [
            ReasoningTrace.model_validate(t) for t in data["reasoning_traces"]
        ]
    if "verification_history" in data:
        state["verification_history"] = [
            VerificationResult.model_validate(v) for v in data["verification_history"]
        ]
    if "analysis_reports" in data:
        state["analysis_reports"] = [
            AnalysisReport.model_validate(a) for a in data["analysis_reports"]
        ]
    if "hitl_pending" in data:
        hp = data["hitl_pending"]
        state["hitl_pending"] = HITLRequest.model_validate(hp) if hp else None
    if "hitl_payload" in data:
        state["hitl_payload"] = data["hitl_payload"]
    if "current_agent" in data:
        state["current_agent"] = data["current_agent"]
    if "confidence" in data:
        state["confidence"] = data["confidence"]
    if "retry_count" in data:
        state["retry_count"] = data["retry_count"]
    if "feedback" in data:
        state["feedback"] = data["feedback"]
    return state


def _to_jsonable(value: object) -> object:
    """Recursively convert Pydantic models inside a value to plain JSON-safe data."""
    from pydantic import BaseModel

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


__all__ = ["Checkpointer", "InMemoryCheckpointer", "RedisCheckpointer", "sqlite_saver"]
