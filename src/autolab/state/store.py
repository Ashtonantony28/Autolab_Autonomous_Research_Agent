"""Async SQLAlchemy persistence for AutoLab state.

Each domain model (`Hypothesis`, `ExperimentDesign`, ...) is stored as a JSON
blob on its own row, plus a few projected columns for querying (id, run_id,
created_at, optional status). Querying detail beyond projection happens in
application code by deserializing the blob — fine for the volumes AutoLab
deals with (thousands of rows, not millions).

The persistence backend is selected via `Settings.database_url`. SQLite +
`aiosqlite` is the dev / test default; Postgres + `psycopg` is production.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import JSON, DateTime, ForeignKey, String, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from autolab.state.models import (
    ExperimentDesign,
    ExperimentResult,
    Hypothesis,
    ReasoningTrace,
    VerificationResult,
)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    research_question: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class _EntityRow(Base):
    __abstract__ = True

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("runs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class HypothesisRow(_EntityRow):
    __tablename__ = "hypotheses"


class ExperimentDesignRow(_EntityRow):
    __tablename__ = "experiment_designs"


class ExperimentResultRow(_EntityRow):
    __tablename__ = "experiment_results"

    status: Mapped[str] = mapped_column(String(16))


class ReasoningTraceRow(_EntityRow):
    __tablename__ = "reasoning_traces"

    agent: Mapped[str] = mapped_column(String(64))


class VerificationResultRow(_EntityRow):
    __tablename__ = "verification_results"


_ROW_FOR_MODEL: dict[type[BaseModel], type[_EntityRow]] = {
    Hypothesis: HypothesisRow,
    ExperimentDesign: ExperimentDesignRow,
    ExperimentResult: ExperimentResultRow,
    ReasoningTrace: ReasoningTraceRow,
    VerificationResult: VerificationResultRow,
}


class StateStore:
    """Thin async wrapper around an `AsyncEngine` for AutoLab state.

    Construct via `await StateStore.create(url)`; that ensures the schema
    exists before any session is handed out.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    @classmethod
    async def create(cls, url: str, *, echo: bool = False) -> StateStore:
        """Create a store and ensure the schema is initialized."""
        engine = create_async_engine(url, echo=echo, future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return cls(engine)

    async def dispose(self) -> None:
        await self._engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as session:
            yield session

    async def create_run(self, research_question: str) -> UUID:
        """Insert a new run row and return its id."""
        run_id = uuid4()
        async with self.session() as session:
            session.add(
                RunRow(
                    id=str(run_id),
                    research_question=research_question,
                    created_at=datetime.now(tz=UTC),
                )
            )
            await session.commit()
        return run_id

    async def append(self, run_id: UUID, model: BaseModel) -> None:
        """Persist any supported domain model under the given run."""
        row_cls = _ROW_FOR_MODEL.get(type(model))
        if row_cls is None:
            raise TypeError(f"no persistence mapping for {type(model).__name__}")

        # All supported models carry an `id` field. Other projected columns are
        # populated only when present on the model.
        ident = getattr(model, "id", uuid4())
        created = getattr(model, "created_at", None) or getattr(model, "produced_at", None)
        if not isinstance(created, datetime):
            created = datetime.now(tz=UTC)

        # mypy: row_cls is one of our concrete subclasses, all of which accept
        # the same kwargs from `_EntityRow`; extras (status, agent) handled below.
        kwargs: dict[str, Any] = {
            "id": str(ident),
            "run_id": str(run_id),
            "created_at": created,
            "payload": model.model_dump(mode="json"),
        }
        if row_cls is ExperimentResultRow:
            kwargs["status"] = getattr(model, "status", "success")
        if row_cls is ReasoningTraceRow:
            kwargs["agent"] = getattr(model, "agent", "")

        async with self.session() as session:
            session.add(row_cls(**kwargs))
            await session.commit()

    async def load_hypotheses(self, run_id: UUID) -> list[Hypothesis]:
        async with self.session() as session:
            rows = await session.scalars(
                select(HypothesisRow).where(HypothesisRow.run_id == str(run_id))
            )
            return [Hypothesis.model_validate(r.payload) for r in rows]

    async def load_traces(self, run_id: UUID) -> list[ReasoningTrace]:
        async with self.session() as session:
            rows = await session.scalars(
                select(ReasoningTraceRow).where(ReasoningTraceRow.run_id == str(run_id))
            )
            return [ReasoningTrace.model_validate(r.payload) for r in rows]

    async def load_verifications(self, run_id: UUID) -> list[VerificationResult]:
        async with self.session() as session:
            rows = await session.scalars(
                select(VerificationResultRow).where(VerificationResultRow.run_id == str(run_id))
            )
            return [VerificationResult.model_validate(r.payload) for r in rows]


__all__ = [
    "Base",
    "ExperimentDesignRow",
    "ExperimentResultRow",
    "HypothesisRow",
    "ReasoningTraceRow",
    "RunRow",
    "StateStore",
    "VerificationResultRow",
]
