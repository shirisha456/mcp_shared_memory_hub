"""Database harness verification.

These tests prove the *foundation*, which is what Milestone 0 is for: the engine
connects, the pool behaves as configured, the server is a version whose features
we depend on, and the isolation fixtures actually isolate.

The pool test is the important one. It pre-empts the trap documented in the
architecture document (section 12.2): a test claiming 50-way concurrency against
a 10-connection pool measures five sequential waves of ten and passes for
entirely the wrong reason. Catching that now, with an explicit assertion,
means Milestone 2's concurrency proof rests on something verified.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from memhub.config import Settings
from memhub.persistence.engine import create_engine, ping, server_version

pytestmark = pytest.mark.integration

MIN_SERVER_VERSION = 160000  # PostgreSQL 16


async def test_engine_round_trips(engine: AsyncEngine) -> None:
    version = await ping(engine)
    assert "PostgreSQL" in version


async def test_server_version_is_supported(engine: AsyncEngine) -> None:
    """Guard the floor we design against.

    Partial unique indexes and FOR UPDATE SKIP LOCKED are far older than 16, but
    pinning the floor here means a surprise downgrade of the container image
    fails a test instead of failing in production behaviour nobody looks at.
    """
    assert await server_version(engine) >= MIN_SERVER_VERSION


async def test_statement_timeout_is_applied_server_side(engine: AsyncEngine) -> None:
    """The timeout must live on the backend, not in application code.

    An application-side timeout abandons the caller while PostgreSQL keeps
    burning CPU on a query nobody is waiting for.
    """
    settings = Settings()
    async with engine.connect() as conn:
        # pg_settings rather than SHOW: SHOW renders the value in whatever unit
        # PostgreSQL considers tidiest ("5s" for 5000ms), whereas pg_settings
        # reports the raw number in the setting's own unit, which is ms here.
        value = (
            await conn.execute(
                text("SELECT setting FROM pg_settings WHERE name = 'statement_timeout'")
            )
        ).scalar_one()
    assert int(value) == settings.db_statement_timeout_ms


async def test_application_name_is_set(engine: AsyncEngine) -> None:
    """Named connections make pg_stat_activity readable during the Milestone 9
    performance work, and let this module count its own backends."""
    async with engine.connect() as conn:
        value = (await conn.execute(text("SHOW application_name"))).scalar_one()
    assert str(value) == "memhub"


async def test_pool_grants_genuinely_concurrent_connections(test_database: str) -> None:
    """N coroutines must hold N *distinct* backends at the same time.

    A Barrier forces every coroutine to have acquired its connection before any
    of them proceeds. If the pool serialised them, the barrier would never trip
    and this test would time out rather than quietly pass.
    """
    concurrency = 20
    settings = Settings(
        db_pool_size=concurrency,
        db_max_overflow=0,
        db_pool_timeout_s=10.0,
    )
    assert settings.max_concurrent_connections >= concurrency

    url = settings.sqlalchemy_url.set(database=test_database).render_as_string(hide_password=False)
    engine = create_engine(settings, url=url)
    barrier = asyncio.Barrier(concurrency)

    async def hold_a_backend() -> int:
        async with engine.connect() as conn:
            pid = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            # Nobody leaves until everybody has arrived: proof of simultaneity.
            await barrier.wait()
            return int(pid)

    try:
        async with asyncio.timeout(30):
            pids = await asyncio.gather(*(hold_a_backend() for _ in range(concurrency)))
    finally:
        await engine.dispose()

    assert len(set(pids)) == concurrency, (
        f"Expected {concurrency} distinct backends, got {len(set(pids))}. "
        "The pool serialised the workload; any concurrency test built on this "
        "would measure sequential waves and pass for the wrong reason."
    )


async def test_pool_exhaustion_fails_fast_rather_than_queueing(test_database: str) -> None:
    """Bounded pool_timeout is a failure-model decision (section 9).

    Unbounded queueing turns a saturated pool into an unbounded latency tail;
    failing fast surfaces it as an explicit, measurable error.
    """
    settings = Settings(db_pool_size=1, db_max_overflow=0, db_pool_timeout_s=0.5)
    url = settings.sqlalchemy_url.set(database=test_database).render_as_string(hide_password=False)
    engine = create_engine(settings, url=url)

    try:
        async with engine.connect():
            loop = asyncio.get_running_loop()
            started = loop.time()
            # sqlalchemy.exc.TimeoutError, not the builtin: pool exhaustion is a
            # SQLAlchemy error, and asserting the precise type is what makes this
            # test able to tell "the pool refused" apart from "something else
            # timed out".
            with pytest.raises(PoolTimeout, match="QueuePool limit"):
                async with engine.connect():
                    pass
            elapsed = loop.time() - started
        # Fails at roughly the configured timeout, not instantly and not forever.
        assert 0.4 <= elapsed < 5.0
    finally:
        await engine.dispose()


async def test_rollback_fixture_isolates_between_tests(db_connection: AsyncConnection) -> None:
    await db_connection.execute(text("CREATE TABLE leak_check (id int PRIMARY KEY)"))
    await db_connection.execute(text("INSERT INTO leak_check VALUES (1)"))
    count = (await db_connection.execute(text("SELECT count(*) FROM leak_check"))).scalar_one()
    assert count == 1


async def test_previous_test_left_nothing_behind(db_connection: AsyncConnection) -> None:
    """Depends on running after the test above; proves the rollback fixture works.

    If isolation were broken, ``leak_check`` would still exist here.
    """
    exists = (
        await db_connection.execute(text("SELECT to_regclass('public.leak_check')"))
    ).scalar_one()
    assert exists is None


async def test_session_fixture_is_bound_to_the_rolled_back_connection(
    db_session: AsyncSession,
) -> None:
    value = (await db_session.execute(text("SELECT 1"))).scalar_one()
    assert value == 1
