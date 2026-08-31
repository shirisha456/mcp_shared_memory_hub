"""Async engine and session plumbing.

Two settings here carry design weight rather than being defaults someone copied:

``pool_timeout``
    Bounded. When the pool is exhausted an acquire fails in ~2s with a clear
    error instead of queueing indefinitely. Unbounded queueing converts a slow
    backend into an unbounded latency tail (architecture doc, section 9).

``statement_timeout``
    Applied server-side per connection. A runaway retrieval query is killed by
    PostgreSQL. Relying on an application-side timeout leaves the backend still
    burning CPU on a query nobody is waiting for any more.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from memhub.config import Settings


def create_engine(settings: Settings, *, url: str | None = None) -> AsyncEngine:
    """Build the async engine.

    ``url`` overrides ``settings.database_url`` so the test harness can point a
    fully configured engine at a freshly cloned template database.
    """
    return create_async_engine(
        url or settings.database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_s,
        # Detects connections severed by a backend restart before handing them
        # out, turning a failure mode from section 9 into a transparent retry.
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "application_name": settings.service_name,
                "statement_timeout": str(settings.db_statement_timeout_ms),
            },
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One transaction per scope: commit on success, roll back on any exception.

    The service layer uses this so that a write and its side effects - the
    idempotency claim, the dedup key, the outbox row - either all land or none
    do. That atomicity is what makes the failure model in section 9 hold.
    """
    async with factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        else:
            await session.commit()


async def ping(engine: AsyncEngine) -> str:
    """Round-trip the database and return its version string."""
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT version()"))
        return str(result.scalar_one())


async def server_version(engine: AsyncEngine) -> int:
    """Numeric server version, e.g. 160004 for PostgreSQL 16.4."""
    async with engine.connect() as conn:
        result = await conn.execute(text("SHOW server_version_num"))
        return int(result.scalar_one())
