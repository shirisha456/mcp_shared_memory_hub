"""Typed application configuration.

Nothing in this codebase reads ``os.environ`` directly. Every knob is declared
here, typed, validated once at startup, and passed explicitly to whatever needs
it. Configuration that fails validation should stop the process, not surface as
a confusing runtime error three layers down.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

Environment = Literal["local", "ci", "production"]
LogFormat = Literal["json", "text"]

_REQUIRED_DRIVER = "postgresql+asyncpg"


class Settings(BaseSettings):
    """Runtime settings, populated from the environment and ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="MEMHUB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    environment: Environment = "local"

    # -- database ----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://memhub:memhub@localhost:5435/memhub"

    db_pool_size: int = Field(default=10, ge=1, le=500)
    db_max_overflow: int = Field(default=5, ge=0, le=500)
    db_pool_timeout_s: float = Field(default=2.0, gt=0)
    """Fail fast when the pool is exhausted. Unbounded queueing turns a slow
    backend into an unbounded latency tail (architecture doc, section 9)."""

    db_statement_timeout_ms: int = Field(default=5_000, ge=100)
    """Server-side ceiling on any single statement. A retrieval query that runs
    away should be killed by PostgreSQL, not by an application-side race."""

    db_echo: bool = False

    # -- observability -----------------------------------------------------
    log_level: str = "INFO"
    log_format: LogFormat = "json"
    service_name: str = "memhub"

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        url = make_url(value)
        if url.drivername != _REQUIRED_DRIVER:
            msg = (
                f"database_url must use the {_REQUIRED_DRIVER!r} driver, "
                f"got {url.drivername!r}. The whole persistence layer is async."
            )
            raise ValueError(msg)
        if not url.database:
            raise ValueError("database_url must name a database")
        return value

    @field_validator("log_level")
    @classmethod
    def _known_log_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return upper

    @property
    def sqlalchemy_url(self) -> URL:
        return make_url(self.database_url)

    @property
    def max_concurrent_connections(self) -> int:
        """Total connections this process may hold open at once.

        Referenced by the concurrency tests: a test claiming N-way concurrency
        must assert this is at least N, or it silently measures N/pool_size
        sequential waves instead (architecture doc, section 12.2).
        """
        return self.db_pool_size + self.db_max_overflow


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached because settings are immutable for the life of the process. Tests
    that need different values construct ``Settings(...)`` directly rather than
    mutating the singleton.
    """
    return Settings()
