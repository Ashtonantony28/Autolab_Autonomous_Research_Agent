"""Application configuration loaded from the environment."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    run_live_tests: bool = Field(
        default=False,
        description="If True, tests marked `live` will hit real external services.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
