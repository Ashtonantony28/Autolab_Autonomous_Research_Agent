# ADR-0003: SQLite for tests, Postgres for production

- **Status:** Accepted
- **Date:** 2026-05-16

## Context

CLAUDE.md §3 locks Postgres as the production state store. The dev container has no Postgres service, and asking every contributor to run one for unit tests is heavy.

## Decision

The persistence layer uses async SQLAlchemy 2.0 and selects the backend purely from `Settings.database_url`. Tests use `sqlite+aiosqlite:///:memory:`; production uses `postgresql+psycopg://...`. Alembic migrations exist for the Postgres path only — tests rely on `Base.metadata.create_all` for speed.

All ORM column types used are common between SQLite and Postgres (`String`, `DateTime`, `JSON`). Where they diverge (e.g. `JSONB` indexes), we'll add Postgres-only specifics inside Alembic migrations.

## Consequences

- Tests run fast and require no services.
- A small risk of dialect-specific bugs slipping past CI; mitigated by adding a Postgres service container to a `live` CI job once we have anything that actually depends on Postgres-only features.
- Alembic migrations are exercised only in production / a future integration job; documented as a known gap.
