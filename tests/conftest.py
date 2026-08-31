"""Test harness.

Integration tests run against a real PostgreSQL instance. Never SQLite: every
guarantee this project makes - partial unique indexes, ``EvalPlanQual`` re-check
semantics under READ COMMITTED, ``FOR UPDATE SKIP LOCKED`` - is a PostgreSQL
behaviour. A test that passes on SQLite would prove nothing about the system we
are actually shipping.

Isolation strategy, in two layers:

*Template databases.* Migrations run **once** per session against a template
database. Each test module then clones it with ``CREATE DATABASE ... TEMPLATE``,
which PostgreSQL implements as a file copy. Running the full migration chain per
module would grow linearly with the number of migrations; cloning does not.

*Transaction rollback.* Within a module, each test gets a session wrapped in a
transaction that is rolled back afterwards. Fast, and adequate for everything
that is not concurrent.

Deliberate limitation: the rollback fixture **cannot** be used by concurrency
tests. Two writers inside one transaction are not two writers. From Milestone 2,
concurrency tests take the module-scoped database directly and manage their own
transactions.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from memhub.config import Settings
from memhub.persistence.engine import create_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
MAINTENANCE_DB = "postgres"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _asyncpg_dsn(url: URL, database: str) -> str:
    """Render a SQLAlchemy URL as a plain libpq DSN for direct asyncpg use.

    Administrative statements (CREATE DATABASE, DROP DATABASE) cannot run inside
    a transaction block, and SQLAlchemy's async connections are transactional by
    default. Going straight to asyncpg keeps that explicit.
    """
    return url.set(drivername="postgresql", database=database).render_as_string(hide_password=False)


async def _admin_execute(url: URL, statement: str) -> None:
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(url, MAINTENANCE_DB))
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


async def _drop_database(url: URL, name: str) -> None:
    """Drop a database, evicting anything still connected to it.

    Without the eviction step this is flaky: a pool that has not finished
    disposing keeps a backend alive and DROP DATABASE fails.
    """
    conn = await asyncpg.connect(dsn=_asyncpg_dsn(url, MAINTENANCE_DB))
    try:
        await conn.execute(
            """
            SELECT pg_terminate_backend(pid)
              FROM pg_stat_activity
             WHERE datname = $1 AND pid <> pg_backend_pid()
            """,
            name,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        await conn.close()


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    # Passed via attributes rather than set_main_option: see migrations/env.py.
    cfg.attributes["db_url"] = db_url
    return cfg


# ---------------------------------------------------------------------------
# session-scoped fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
async def database_available(settings: Settings) -> bool:
    """Probe the database once.

    Locally, a missing database skips integration tests with an actionable
    message. In CI, ``MEMHUB_REQUIRE_DB=1`` turns that skip into a hard failure,
    so a broken service container can never be mistaken for a green build.
    """
    url = settings.sqlalchemy_url
    try:
        conn = await asyncpg.connect(dsn=_asyncpg_dsn(url, MAINTENANCE_DB), timeout=5)
        await conn.close()
    except (TimeoutError, OSError, asyncpg.PostgresError) as exc:
        if os.environ.get("MEMHUB_REQUIRE_DB") == "1":
            pytest.fail(f"MEMHUB_REQUIRE_DB=1 but PostgreSQL is unreachable: {exc}")
        pytest.skip(
            f"PostgreSQL unreachable at {url.host}:{url.port} ({exc}). "
            "Run 'docker compose up -d --wait' first."
        )
    return True


@pytest.fixture(scope="session")
async def template_database(
    settings: Settings,
    database_available: bool,
) -> AsyncIterator[str]:
    """Create a migrated template database, once per test session."""
    url = settings.sqlalchemy_url
    template_name = f"{url.database}_tmpl"

    await _drop_database(url, template_name)
    await _admin_execute(url, f'CREATE DATABASE "{template_name}"')

    template_url = url.set(database=template_name).render_as_string(hide_password=False)
    # Alembic's online mode calls asyncio.run(), which cannot be nested inside a
    # running loop. A worker thread gives it a clean loop of its own.
    await asyncio.to_thread(command.upgrade, _alembic_config(template_url), "head")

    yield template_name

    await _drop_database(url, template_name)


@pytest.fixture(scope="module")
async def test_database(settings: Settings, template_database: str) -> AsyncIterator[str]:
    """Clone the template into a database private to this test module."""
    url = settings.sqlalchemy_url
    name = f"{url.database}_t_{uuid.uuid4().hex[:12]}"

    await _admin_execute(url, f'CREATE DATABASE "{name}" TEMPLATE "{template_database}"')
    yield name
    await _drop_database(url, name)


@pytest.fixture(scope="module")
async def engine(settings: Settings, test_database: str) -> AsyncIterator[AsyncEngine]:
    url = settings.sqlalchemy_url.set(database=test_database).render_as_string(hide_password=False)
    eng = create_engine(settings, url=url)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db_connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A connection in an always-rolled-back transaction.

    Not for concurrency tests - see the module docstring.
    """
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            yield conn
        finally:
            await transaction.rollback()


@pytest.fixture
async def db_session(db_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(bind=db_connection, expire_on_commit=False) as session:
        yield session


@pytest.fixture
async def committing_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session whose commits are real.

    The rollback fixtures above are the right default, but some tests need data
    to outlive a single transaction - a benchmark that seeds a corpus and then
    measures repeated reads against it, for instance. Isolation for those comes
    from the module-scoped database being dropped afterwards.
    """
    async with AsyncSession(bind=engine, expire_on_commit=False) as session:
        yield session
