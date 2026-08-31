"""Alembic environment, wired for the async engine.

Two things differ from the stock async template:

1. The database URL is resolved from ``config.attributes["db_url"]`` first, then
   from :mod:`memhub.config`. The ``attributes`` channel is used rather than
   ``set_main_option`` because the latter goes through ConfigParser, which
   performs ``%`` interpolation and mangles URLs containing percent-encoded
   credentials. The test harness passes a per-test database URL this way.

2. ``compare_type`` and ``compare_server_default`` are on, so the Milestone 0
   drift test (``alembic check``) actually catches a column whose type changed
   in the models but not in a migration.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from memhub.config import get_settings
from memhub.persistence.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    injected = config.attributes.get("db_url")
    if injected:
        return str(injected)
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a database connection.

    Only ever invoked explicitly via ``alembic upgrade --sql``; the MCP server
    never runs in this mode, so writing SQL to stdout here is safe.
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    engine = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
