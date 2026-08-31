"""Fixtures for genuinely concurrent tests.

The rolled-back ``db_session`` fixture used everywhere else is unusable here:
two writers inside one transaction are not two writers. These tests take the
module database directly, build their own engine with a pool large enough for
the concurrency they claim, and manage their own transactions.

**The pool assertion is the point.** A test claiming 50-way concurrency against
a 10-connection pool measures five sequential waves of ten and passes for
entirely the wrong reason - green, meaningless, and impossible to notice.
:func:`concurrent_engine` refuses to hand back an engine that cannot deliver the
concurrency asked for.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from memhub.config import Settings
from memhub.persistence.engine import create_engine, create_session_factory


@pytest.fixture
def concurrent_engine(
    settings: Settings, test_database: str
) -> Iterator[Callable[[int], AsyncEngine]]:
    """Build an engine guaranteed to support N simultaneous connections."""
    engines: list[AsyncEngine] = []

    def build(concurrency: int) -> AsyncEngine:
        # One spare beyond the requested concurrency, so a helper connection
        # taken by the test itself cannot starve a worker.
        sized = Settings(
            db_pool_size=concurrency + 1,
            db_max_overflow=0,
            db_pool_timeout_s=30.0,
            db_statement_timeout_ms=30_000,
        )
        assert sized.max_concurrent_connections > concurrency, (
            f"pool of {sized.max_concurrent_connections} cannot deliver "
            f"{concurrency}-way concurrency"
        )
        url = sized.sqlalchemy_url.set(database=test_database).render_as_string(hide_password=False)
        engine = create_engine(sized, url=url)
        engines.append(engine)
        return engine

    yield build

    for engine in engines:
        asyncio.get_event_loop().create_task(engine.dispose())


@pytest.fixture
async def sessions(
    concurrent_engine: Callable[[int], AsyncEngine],
) -> AsyncIterator[Callable[[int], async_sessionmaker[AsyncSession]]]:
    """A session factory sized for N concurrent writers."""

    def build(concurrency: int) -> async_sessionmaker[AsyncSession]:
        return create_session_factory(concurrent_engine(concurrency))

    yield build


async def assert_backends_are_distinct(
    factory: async_sessionmaker[AsyncSession], concurrency: int
) -> None:
    """Prove the pool really does hand out N separate backends.

    A barrier forces every task to hold its connection before any proceeds. If
    the pool serialised them the barrier would never trip and this would hang
    rather than quietly passing.
    """
    barrier = asyncio.Barrier(concurrency)

    async def hold() -> int:
        async with factory() as session:
            pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await barrier.wait()
            return int(pid)

    async with asyncio.timeout(30):
        pids = await asyncio.gather(*(hold() for _ in range(concurrency)))

    assert len(set(pids)) == concurrency, (
        f"expected {concurrency} distinct backends, got {len(set(pids))} - "
        "the workload was serialised and any result below is meaningless"
    )


async def run_together[T](
    make_task: Callable[[int], Awaitable[T]], concurrency: int
) -> list[T | BaseException]:
    """Run N coroutines aligned on a barrier, collecting results and exceptions.

    The barrier is what makes this a race rather than a sequence: every task has
    already opened its transaction and is poised on the same instruction before
    any of them proceeds. Without it, tasks start microseconds apart and the
    first one routinely finishes before the last begins.
    """
    barrier = asyncio.Barrier(concurrency)

    async def gated(index: int) -> T:
        await barrier.wait()
        return await make_task(index)

    return await asyncio.gather(*(gated(i) for i in range(concurrency)), return_exceptions=True)
