# Phase 0 — Foundation Plan

**Branch:** `claude/plan-execute-modes-PT67G`
**Goal:** A green, empty skeleton that satisfies `make install && make test && make lint && make typecheck` and matches the project layout in `CLAUDE.md` §5.
**Out of scope (deferred to later phases):** any agent logic, verifier, LangGraph wiring, DB migrations, dashboard. Phase 0 is plumbing only.

---

## 1. Files to create (exact list)

### Repo root
- `CLAUDE.md` — copy of the uploaded operating contract so it lives in the repo (§5 requires it).
- `README.md` — quickstart only: install, run tests, lint, typecheck. Architecture summary deferred to §2 of the README in Phase 1.
- `pyproject.toml` — `uv`-managed; pinned Python `>=3.11,<3.13`. Dependencies split into:
  - `[project.dependencies]`: `anthropic`, `langgraph>=0.2`, `pydantic>=2`, `pydantic-settings`, `z3-solver`, `sqlalchemy>=2`, `alembic`, `psycopg[binary]`, `redis`, `fastapi`, `uvicorn`, `streamlit`, `chromadb`, `httpx`, `structlog`.
  - `[dependency-groups].dev`: `pytest`, `pytest-asyncio`, `pytest-cov`, `hypothesis`, `ruff`, `mypy`, `types-redis`.
  - Each entry gets a one-line `# why:` comment (§3 of CLAUDE.md).
- `.python-version` — `3.11`.
- `.env.example` — `ANTHROPIC_API_KEY=`, `DATABASE_URL=postgresql+psycopg://...`, `REDIS_URL=redis://localhost:6379/0`, `LOG_LEVEL=INFO`, `RUN_LIVE_TESTS=0`.
- `.gitignore` — Python (`__pycache__`, `.venv`, `*.egg-info`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `dist/`, `build/`), env (`.env`, `.env.local`), editor (`.vscode/`, `.idea/`, `.DS_Store`), tooling (`uv.lock` is **kept**; `coverage.xml`, `.coverage` ignored).
- `Makefile` — targets: `install`, `test`, `lint`, `format`, `typecheck`, `run`, `clean`. `run` will `python -m autolab.cli` once the CLI stub exists.
- `ruff.toml` — line length 100, target `py311`, enable `E,F,I,UP,B,SIM,RUF`; ignore nothing globally.
- `mypy.ini` — `strict = True`, `python_version = 3.11`, `plugins = pydantic.mypy`, `disallow_any_explicit = True`, `warn_unreachable = True`.
- `.pre-commit-config.yaml` — ruff (lint + format) + mypy + pytest (fast unit only). Installed but not enforced as part of DoD; documented in README.

### CI
- `.github/workflows/ci.yml` — single job on `ubuntu-latest`, matrix Python `3.11`. Steps: checkout → install `uv` → `uv sync` → `make lint typecheck test`. Caches `~/.cache/uv` keyed on `pyproject.toml`.

### Package skeleton (mirrors §5; every dir gets an `__init__.py` so mypy treats it as a package)
- `src/autolab/__init__.py` — exposes `__version__ = "0.0.0"`.
- `src/autolab/config.py` — minimal `Settings(BaseSettings)` with `anthropic_api_key`, `database_url`, `redis_url`, `log_level`; `model_config = SettingsConfigDict(env_file=".env", extra="ignore")`. Single `get_settings()` cached with `functools.lru_cache`.
- `src/autolab/cli.py` — `def main() -> None:` that prints `"AutoLab v0.0.0 — skeleton OK"` and exits 0. Wired via `[project.scripts] autolab = "autolab.cli:main"`.
- Empty subpackages with `__init__.py` only (no logic, but importable so mypy walks them):
  - `state/` (+ `schema.py`, `models.py`, `store.py` as empty modules with `"""Placeholder — implemented in Phase 1."""`)
  - `verification/` (+ `vericot.py`, `formalizer.py`, `premise_grounder.py`, `solver.py`, `confidence.py`)
  - `agents/` (+ `base.py`, `research.py`, `design.py`, `execute.py`, `analysis.py`, `verification_monitor.py`)
  - `orchestrator/` (+ `graph.py`, `routing.py`, `checkpoints.py`, `recovery.py`)
  - `tools/` (+ `literature.py`, `lab_hardware/__init__.py`, `rag/__init__.py`, `simulation/__init__.py`)
  - `monitoring/` (+ `tracer.py`, `metrics.py`, `dashboard.py`)
- `py.typed` marker at `src/autolab/py.typed`.

### Tests
- `tests/__init__.py`
- `tests/conftest.py` — `pytest_asyncio` mode `auto`; a `monkeypatch_settings` fixture that injects fake env vars.
- `tests/unit/test_smoke.py` — three trivial tests: `import autolab`, `autolab.__version__ == "0.0.0"`, `cli.main()` exits cleanly via `capsys`.
- Empty `tests/unit/`, `tests/integration/`, `tests/e2e/`, `tests/fixtures/` directories each with a `.gitkeep`.

### Scripts / docs
- `scripts/.gitkeep` (real scripts come in Phase 1).
- `docs/architecture.md` — one paragraph + the diagram from §2 of CLAUDE.md. Detail deferred.
- `docs/decisions/0001-record-architecture-decisions.md` — ADR-0001 establishing the ADR format itself (Nygard template).
- `docs/plans/phase_0.md` — this file.

---

## 2. Commands I will run (in order)

```bash
# Tool prereqs (assumed present in this container; verified first)
which uv || curl -LsSf https://astral.sh/uv/install.sh | sh

# Initialize project
uv init --package --no-readme --python 3.11   # (or manually author pyproject.toml — see fallback below)
uv add anthropic 'langgraph>=0.2' 'pydantic>=2' pydantic-settings z3-solver \
       'sqlalchemy>=2' alembic 'psycopg[binary]' redis fastapi uvicorn \
       streamlit chromadb httpx structlog
uv add --dev pytest pytest-asyncio pytest-cov hypothesis ruff mypy types-redis

# Sanity gates (each must exit 0)
make install
make lint
make typecheck
make test

# First commit
git add -A
git commit -m "feat: phase 0 foundation skeleton"
git push -u origin claude/plan-execute-modes-PT67G
```

**Fallback if `uv init` template fights the layout:** author `pyproject.toml` by hand to the exact spec above and run only `uv sync`. The layout in §5 is the source of truth, not `uv`'s default.

---

## 3. Definition of Done (you confirm)

All four pass on a fresh clone:
1. `make install` — `uv sync` resolves and installs without error.
2. `make lint` — `ruff check .` and `ruff format --check .` clean.
3. `make typecheck` — `mypy --strict src/ tests/` clean.
4. `make test` — `pytest` collects ≥3 tests and all pass; coverage report runs (no threshold enforced yet).
5. CI workflow runs the same four targets and is green on the pushed branch.
6. Layout on disk matches §5 of CLAUDE.md (verified by `tree -L 3 src tests docs`).

I will paste the four command outputs and the CI run URL into the chat when the phase is done.

---

## 4. Risks / things to watch

- **`z3-solver` wheels** sometimes fail on minimal CI images. If install breaks CI, I'll pin a known-good version and document it in an ADR rather than swapping the solver.
- **`mypy --strict` + `pydantic.mypy`** can flag the empty placeholder modules. I'll add `# type: ignore[empty-body]`-style escapes only if unavoidable, and only on placeholders that get real code in Phase 1.
- **`chromadb`** pulls a large dependency tree. If it slows CI noticeably, I'll move it to an optional extra (`[project.optional-dependencies].rag`) before Phase 5 needs it.
- **No DB / Redis running** in CI for Phase 0 — that's fine because nothing touches them yet. Phase 1 will add service containers to the workflow.

---

## 5. What I will NOT do in Phase 0

- No agent logic, no verifier code, no LangGraph wiring.
- No Alembic migration files (the `alembic` dep is installed, init happens in Phase 1).
- No real Anthropic API calls in tests.
- No dashboard.
- No benchmark scripts.

---

**Stopping here per CLAUDE.md §11. Awaiting "proceed".**
