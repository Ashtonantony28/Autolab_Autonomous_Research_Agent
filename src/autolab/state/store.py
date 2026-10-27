"""Async SQLAlchemy persistence for AutoLab state.

Each domain model (`Hypothesis`, `ExperimentDesign`, ...) is stored as a JSON
blob on its own row, plus a few projected columns for querying (id, run_id,
created_at, optional status). Rows are keyed by `(run_id, id)`, so one run
never moves or overwrites another run's row, and every write is a single
`INSERT ... ON CONFLICT`, so concurrent writers of one key do not race.
Querying detail beyond projection happens in
application code by deserializing the blob — fine for the volumes AutoLab
deals with (thousands of rows, not millions).

The persistence backend is selected via `Settings.database_url`. SQLite +
`aiosqlite` is the dev / test default; Postgres + `psycopg` is production.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Final, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import JSON, DateTime, ForeignKey, String, event, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import make_url
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import ConnectionPoolEntry

from autolab.monitoring.events import TraceEvent
from autolab.state.models import (
    AnalysisReport,
    ExperimentDesign,
    ExperimentResult,
    Hypothesis,
    ReasoningTrace,
    VerificationResult,
)

ModelT = TypeVar("ModelT", bound=BaseModel)

SQLITE_BUSY_TIMEOUT_S: Final[float] = 30.0
"""How long a SQLite writer waits for another writer's lock before failing."""


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    research_question: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class _EntityRow(Base):
    __abstract__ = True

    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("runs.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
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


class AnalysisReportRow(_EntityRow):
    __tablename__ = "analysis_reports"


class TraceEventRow(_EntityRow):
    __tablename__ = "trace_events"

    node: Mapped[str] = mapped_column(String(64))


_ROW_FOR_MODEL: dict[type[BaseModel], type[_EntityRow]] = {
    Hypothesis: HypothesisRow,
    ExperimentDesign: ExperimentDesignRow,
    ExperimentResult: ExperimentResultRow,
    ReasoningTrace: ReasoningTraceRow,
    VerificationResult: VerificationResultRow,
    AnalysisReport: AnalysisReportRow,
    TraceEvent: TraceEventRow,
}


_INSERTS: Final[dict[str, Callable[[Any], sqlite.Insert | postgresql.Insert]]] = {
    "sqlite": sqlite.insert,
    "postgresql": postgresql.insert,
}
"""Per-dialect `insert` constructs that support `ON CONFLICT`."""


def upsert_statement(
    dialect: str, row_cls: type[Base], values: dict[str, Any]
) -> sqlite.Insert | postgresql.Insert:
    """One atomic `INSERT ... ON CONFLICT` for `dialect`.

    A run row is never overwritten; an entity row is updated in place by `(run_id, id)`.

    Raises:
        ValueError: if `dialect` has no `ON CONFLICT` support here.
    """
    if dialect not in _INSERTS:
        raise ValueError(f"unsupported database dialect {dialect!r}")
    stmt = _INSERTS[dialect](row_cls).values(**values)
    if row_cls is RunRow:
        return stmt.on_conflict_do_nothing(index_elements=["id"])
    return stmt.on_conflict_do_update(
        index_elements=["run_id", "id"],
        set_={k: stmt.excluded[k] for k in values if k not in ("run_id", "id")},
    )


def _create_engine(url: str, *, echo: bool) -> AsyncEngine:
    """An engine whose SQLite writers wait out each other's locks instead of failing."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return create_async_engine(url, echo=echo, future=True)
    engine = create_async_engine(
        url, echo=echo, future=True, connect_args={"timeout": SQLITE_BUSY_TIMEOUT_S}
    )
    if parsed.database not in (None, "", ":memory:"):

        @event.listens_for(engine.sync_engine, "connect")
        def _wal(dbapi_conn: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


class StateStore:
    """Thin async wrapper around an `AsyncEngine` for AutoLab state.

    Construct via `await StateStore.create(url)`; that ensures the schema
    exists before any session is handed out.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        self._dialect = engine.dialect.name

    @classmethod
    async def create(cls, url: str, *, echo: bool = False) -> StateStore:
        """Create a store and ensure the schema is initialized."""
        engine = _create_engine(url, echo=echo)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return cls(engine)

    async def dispose(self) -> None:
        await self._engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as session:
            yield session

    async def create_run(self, research_question: str, *, run_id: UUID | None = None) -> UUID:
        """Insert a new run row (with a fresh id unless `run_id` is given) and return its id."""
        run_id = run_id or uuid4()
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

    async def ensure_run(self, run_id: UUID, research_question: str) -> None:
        """Create the run row unless it already exists (e.g. a resumed run)."""
        values = {
            "id": str(run_id),
            "research_question": research_question,
            "created_at": datetime.now(tz=UTC),
        }
        async with self.session() as session:
            await session.execute(upsert_statement(self._dialect, RunRow, values))
            await session.commit()

    async def list_runs(self) -> list[tuple[UUID, str]]:
        """Return `(run_id, research_question)` for every run, newest first."""
        async with self.session() as session:
            rows = await session.scalars(select(RunRow).order_by(RunRow.created_at.desc()))
            return [(UUID(r.id), r.research_question) for r in rows]

    async def append(self, run_id: UUID, model: BaseModel) -> None:
        """Persist any supported domain model under the given run.

        Idempotent: rows are upserted by `(run_id, id)` (`VerificationResult` by
        `trace_id`), so re-recording an entity after a restart does not duplicate
        it, and the same id under another run is a separate row.
        """
        row_cls = _ROW_FOR_MODEL.get(type(model))
        if row_cls is None:
            raise TypeError(f"no persistence mapping for {type(model).__name__}")

        # All supported models carry an `id` field. Other projected columns are
        # populated only when present on the model.
        ident = getattr(model, "id", None) or getattr(model, "trace_id", None) or uuid4()
        created = next(
            (
                getattr(model, f)
                for f in ("created_at", "produced_at", "completed_at", "started_at")
                if hasattr(model, f)
            ),
            None,
        )
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
        if row_cls is TraceEventRow:
            kwargs["node"] = getattr(model, "node", "")

        async with self.session() as session:
            await session.execute(upsert_statement(self._dialect, row_cls, kwargs))
            await session.commit()

    async def load_hypotheses(self, run_id: UUID) -> list[Hypothesis]:
        return await self._load(run_id, Hypothesis)

    async def load_traces(self, run_id: UUID) -> list[ReasoningTrace]:
        return await self._load(run_id, ReasoningTrace)

    async def load_verifications(self, run_id: UUID) -> list[VerificationResult]:
        return await self._load(run_id, VerificationResult)

    async def load_reports(self, run_id: UUID) -> list[AnalysisReport]:
        return await self._load(run_id, AnalysisReport)

    async def load_events(self, run_id: UUID) -> list[TraceEvent]:
        """Return the run's trace events in execution order."""
        return await self._load(run_id, TraceEvent)

    async def _load(self, run_id: UUID, model: type[ModelT]) -> list[ModelT]:
        row_cls = _ROW_FOR_MODEL[model]
        async with self.session() as session:
            rows = await session.scalars(
                select(row_cls).where(row_cls.run_id == str(run_id)).order_by(row_cls.created_at)
            )
            return [model.model_validate(r.payload) for r in rows]


__all__ = [
    "AnalysisReportRow",
    "Base",
    "ExperimentDesignRow",
    "ExperimentResultRow",
    "HypothesisRow",
    "ReasoningTraceRow",
    "RunRow",
    "StateStore",
    "TraceEventRow",
    "VerificationResultRow",
    "upsert_statement",
]
