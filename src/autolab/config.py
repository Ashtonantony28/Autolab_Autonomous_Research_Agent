"""Application configuration loaded from the environment."""

from __future__ import annotations

from functools import lru_cache
from typing import Final

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PASS_THRESHOLD: Final[float] = 0.7
RETRY_THRESHOLD: Final[float] = 0.4
MAX_RETRIES: Final[int] = 2
HITL_COST_USD: Final[float] = 10.0
HITL_DURATION_MIN: Final[float] = 60.0
SOLVER_TIMEOUT_S: Final[float] = 5.0
STEP_BUDGET_MS: Final[float] = 50.0


class Settings(BaseSettings):
    """Strongly-typed env-backed settings.

    All values come from environment variables or a local `.env` file.
    Never hard-code credentials; never read os.environ directly elsewhere.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    anthropic_api_key: str = Field(default="", description="Anthropic API key for Claude calls.")
    database_url: str = Field(
        default="sqlite+aiosqlite:///./autolab.db",
        description="SQLAlchemy URL for the persistent state store.",
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis URL for the LangGraph checkpointer and agent comms.",
    )
    log_level: str = Field(default="INFO", description="Logging level (DEBUG/INFO/WARNING/ERROR).")
    pass_threshold: float = Field(default=PASS_THRESHOLD, ge=0.0, le=1.0)
    retry_threshold: float = Field(default=RETRY_THRESHOLD, ge=0.0, le=1.0)
    max_retries: int = Field(default=MAX_RETRIES, ge=0)
    hitl_cost_usd: float = Field(default=HITL_COST_USD, ge=0.0)
    hitl_duration_min: float = Field(default=HITL_DURATION_MIN, ge=0.0)
    solver_timeout_s: float = Field(default=SOLVER_TIMEOUT_S, gt=0.0)
    step_budget_ms: float = Field(default=STEP_BUDGET_MS, gt=0.0)
    run_live_tests: bool = Field(
        default=False,
        description="If True, tests marked `live` will hit real external services.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
