.PHONY: install test test-cov lint format typecheck run dashboard clean help

help:  ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install:  ## Sync all dev + runtime dependencies via uv
	uv sync --all-extras

test:  ## Run the test suite
	uv run pytest

test-cov:  ## Run tests with coverage report
	uv run pytest --cov=autolab --cov-report=term-missing

lint:  ## Lint + format-check
	uv run ruff check .
	uv run ruff format --check .

format:  ## Auto-format
	uv run ruff format .
	uv run ruff check --fix .

typecheck:  ## Strict mypy
	uv run mypy src tests

run:  ## Run the CLI entry point
	uv run autolab

dashboard:  ## Audit persisted runs in Streamlit (DATABASE_URL)
	uv run streamlit run scripts/dashboard.py

clean:  ## Remove caches & build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info .coverage coverage.xml htmlcov
	find . -type d -name __pycache__ -exec rm -rf {} +
