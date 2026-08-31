"""Configuration validation.

Settings are the one place where a bad value should stop the process rather than
surface later as a confusing runtime error, so the validators are tested.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memhub.config import Settings

VALID_URL = "postgresql+asyncpg://memhub:memhub@localhost:5435/memhub"


def test_defaults_are_usable() -> None:
    settings = Settings()
    assert settings.sqlalchemy_url.drivername == "postgresql+asyncpg"
    assert settings.db_pool_size >= 1
    assert settings.db_pool_timeout_s > 0


def test_sync_driver_is_rejected() -> None:
    """The persistence layer is async end to end; a sync driver would silently
    block the event loop rather than fail loudly, so reject it at startup."""
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        Settings(database_url="postgresql://memhub:memhub@localhost:5435/memhub")


def test_psycopg_driver_is_rejected() -> None:
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        Settings(database_url="postgresql+psycopg://memhub:memhub@localhost:5435/memhub")


def test_url_without_database_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must name a database"):
        Settings(database_url="postgresql+asyncpg://memhub:memhub@localhost:5435")


def test_log_level_is_normalised() -> None:
    assert Settings(log_level="debug").log_level == "DEBUG"


def test_unknown_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError, match="log_level must be one of"):
        Settings(log_level="LOUD")


def test_unknown_setting_is_rejected() -> None:
    """extra='forbid' catches typo'd environment variables at startup instead of
    silently ignoring them."""
    with pytest.raises(ValidationError):
        Settings(databse_url=VALID_URL)  # type: ignore[call-arg]


def test_max_concurrent_connections_is_pool_plus_overflow() -> None:
    settings = Settings(db_pool_size=50, db_max_overflow=5)
    assert settings.max_concurrent_connections == 55


@pytest.mark.parametrize("pool_size", [0, -1, 501])
def test_pool_size_bounds(pool_size: int) -> None:
    with pytest.raises(ValidationError):
        Settings(db_pool_size=pool_size)
