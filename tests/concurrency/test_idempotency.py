"""Concurrent retries must produce exactly one write.

The realistic version of this: a client sends ``memory_remember``, the
connection drops before the response arrives, and the client retries. It does
not know whether the first attempt committed. Without an idempotency key the
only safe action is to give up; with one, the retry is free.

Fifty *simultaneous* retries is harsher than reality and deliberately so. It
exercises the part of the protocol that is easy to get wrong: a duplicate
arriving while the original transaction is still in flight, which is what
``SELECT ... FOR SHARE`` exists to handle.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memhub.domain.enums import MemoryType
from memhub.domain.models import RememberResult, ReviseReplayed, ReviseResult, ReviseSucceeded
from memhub.services.idempotency import IdempotencyKeyReusedError
from memhub.services.memories import remember, revise
from memhub.services.projects import use_project
from tests.concurrency.conftest import run_together
from tests.integration.test_invariants import assert_invariants_hold

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]

CONCURRENCY = 50


async def seed_project(factory: async_sessionmaker[AsyncSession], slug: str) -> uuid.UUID:
    async with factory() as session, session.begin():
        project = await use_project(session, slug=slug, create=True)
    return project.id


async def test_fifty_concurrent_retries_create_one_memory(
    sessions: Callable[[int], async_sessionmaker[AsyncSession]],
) -> None:
    """The exit criterion.

    Exactly one memory exists, and all fifty callers learn the same memory_id -
    so every one of them can carry on as though its own request succeeded, which
    is the entire point of idempotency.
    """
    factory = sessions(CONCURRENCY)
    project_id = await seed_project(factory, "idem-basic")
    key = f"req-{uuid.uuid4()}"

    async def attempt(index: int) -> RememberResult:
        async with factory() as session, session.begin():
            return await remember(
                session,
                project_id,
                memory_type=MemoryType.DECISION,
                content="PostgreSQL is the task queue.",
                client_request_id=key,
                author_client="cursor",
            )

    results = await run_together(attempt, CONCURRENCY)

    for result in results:
        assert not isinstance(result, BaseException), f"unexpected exception: {result!r}"

    created = [r for r in results if r.outcome == "created"]  # type: ignore[union-attr]
    replayed = [r for r in results if r.outcome == "idempotent_replay"]  # type: ignore[union-attr]

    assert len(created) == 1
    assert len(replayed) == CONCURRENCY - 1

    # Every caller sees the same logical result.
    memory_ids = {r.memory.memory_id for r in results}  # type: ignore[union-attr]
    assert len(memory_ids) == 1
    revision_numbers = {r.memory.revision_no for r in results}  # type: ignore[union-attr]
    assert revision_numbers == {1}

    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM memories WHERE project_id = :p"),
                {"p": project_id},
            )
        ).scalar_one()
        assert count == 1, f"idempotency failed: {count} memories exist"
        await assert_invariants_hold(session)


async def test_different_keys_create_different_memories(
    sessions: Callable[[int], async_sessionmaker[AsyncSession]],
) -> None:
    """The control case.

    Without this, a broken implementation that ignored the key entirely and
    always returned the first memory would pass the test above.
    """
    factory = sessions(20)
    project_id = await seed_project(factory, "idem-distinct")

    async def attempt(index: int) -> RememberResult:
        async with factory() as session, session.begin():
            return await remember(
                session,
                project_id,
                memory_type=MemoryType.FACT,
                content=f"fact number {index}",
                client_request_id=f"req-{index}-{uuid.uuid4()}",
                author_client="cursor",
            )

    results = await run_together(attempt, 20)
    memory_ids = {r.memory.memory_id for r in results}  # type: ignore[union-attr]

    assert len(memory_ids) == 20
    assert all(r.outcome == "created" for r in results)  # type: ignore[union-attr]


async def test_identical_writes_deduplicate_even_without_a_key(
    sessions: Callable[[int], async_sessionmaker[AsyncSession]],
) -> None:
    """Two mechanisms, one outcome here - and it is worth being precise about
    which one did the work.

    Ten simultaneous writes of *identical* content collapse to one memory even
    with no idempotency key, because deduplication catches them: the content
    hash is a primary key, so only one writer can claim it. That is dedup, not
    idempotency.
    """
    factory = sessions(10)
    project_id = await seed_project(factory, "dedup-no-key")

    async def attempt(index: int) -> RememberResult:
        async with factory() as session, session.begin():
            return await remember(
                session,
                project_id,
                memory_type=MemoryType.FACT,
                content="the same sentence every time",
                author_client="cursor",
            )

    results = await run_together(attempt, 10)
    for result in results:
        assert not isinstance(result, BaseException), f"unexpected exception: {result!r}"

    memory_ids = {r.memory.memory_id for r in results}  # type: ignore[union-attr]
    assert len(memory_ids) == 1

    outcomes = [r.outcome for r in results]  # type: ignore[union-attr]
    assert outcomes.count("created") == 1
    assert outcomes.count("deduplicated") == 9

    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM memories WHERE project_id = :p"),
                {"p": project_id},
            )
        ).scalar_one()
        assert count == 1
        await assert_invariants_hold(session)


async def test_without_a_key_a_changed_retry_still_duplicates(
    sessions: Callable[[int], async_sessionmaker[AsyncSession]],
) -> None:
    """Honest about the limits of deduplication.

    Dedup keys on content, so it cannot help a retry whose content differs by so
    much as a word - and a client rebuilding a request after a dropped
    connection may well phrase it slightly differently. Only an idempotency key,
    which identifies the *request* rather than the content, covers that case.

    This is why the two mechanisms both exist and why the tool description asks
    for a key rather than relying on dedup.
    """
    factory = sessions(10)
    project_id = await seed_project(factory, "dedup-changed-retry")

    async def attempt(index: int) -> RememberResult:
        async with factory() as session, session.begin():
            return await remember(
                session,
                project_id,
                memory_type=MemoryType.FACT,
                # The same intent, trivially different wording.
                content=f"PostgreSQL is the task queue (attempt {index}).",
                author_client="cursor",
            )

    results = await run_together(attempt, 10)
    memory_ids = {r.memory.memory_id for r in results}  # type: ignore[union-attr]
    assert len(memory_ids) == 10, (
        "deduplication is content-addressed, so it cannot collapse requests that "
        "differ textually - that is what an idempotency key is for"
    )


async def test_key_reuse_with_a_different_payload_is_rejected(
    sessions: Callable[[int], async_sessionmaker[AsyncSession]],
) -> None:
    """Silently returning the earlier result would answer a question the caller
    never asked - a failure that leaves no trace."""
    factory = sessions(4)
    project_id = await seed_project(factory, "idem-reuse")
    key = f"req-{uuid.uuid4()}"

    async with factory() as session, session.begin():
        await remember(
            session,
            project_id,
            memory_type=MemoryType.FACT,
            content="the original request",
            client_request_id=key,
        )

    with pytest.raises(IdempotencyKeyReusedError, match="already used for a different"):
        async with factory() as session, session.begin():
            await remember(
                session,
                project_id,
                memory_type=MemoryType.FACT,
                content="a completely different request",
                client_request_id=key,
            )


async def test_keys_are_scoped_to_a_project(
    sessions: Callable[[int], async_sessionmaker[AsyncSession]],
) -> None:
    """Two projects using the same key must not collide.

    Keys are caller-generated, so a client using a counter rather than a UUID
    would otherwise have its second project silently replay the first project's
    memories.
    """
    factory = sessions(4)
    async with factory() as session, session.begin():
        first = await use_project(session, slug="idem-scope-a", create=True)
        second = await use_project(session, slug="idem-scope-b", create=True)

    key = "shared-key-000001"

    async with factory() as session, session.begin():
        a = await remember(
            session,
            first.id,
            memory_type=MemoryType.FACT,
            content="belongs to a",
            client_request_id=key,
        )
    async with factory() as session, session.begin():
        b = await remember(
            session,
            second.id,
            memory_type=MemoryType.FACT,
            content="belongs to b",
            client_request_id=key,
        )

    assert a.outcome == "created"
    assert b.outcome == "created"
    assert a.memory.memory_id != b.memory.memory_id


async def test_concurrent_revise_retries_replay_rather_than_conflict(
    sessions: Callable[[int], async_sessionmaker[AsyncSession]],
) -> None:
    """The subtle one, and the reason ordering matters.

    ``revise`` is already safe without a key - the compare-and-set makes a
    duplicate write impossible. But a retry of an *already applied* revise would
    fail the version check and be reported as a conflict, telling the caller it
    lost a race it actually won. Claiming the idempotency key before the
    compare-and-set turns that into a clean replay.
    """
    factory = sessions(CONCURRENCY)
    async with factory() as session, session.begin():
        project = await use_project(session, slug="idem-revise", create=True)
        created = await remember(
            session,
            project.id,
            memory_type=MemoryType.DECISION,
            content="Redis is the task queue.",
        )
    project_id, memory_id = project.id, created.memory.memory_id
    key = f"req-{uuid.uuid4()}"

    async def attempt(index: int) -> ReviseResult:
        async with factory() as session, session.begin():
            return await revise(
                session,
                project_id,
                memory_id,
                expected_revision=1,
                content="PostgreSQL is the task queue.",
                client_request_id=key,
                author_client="cursor",
            )

    results = await run_together(attempt, CONCURRENCY)

    for result in results:
        assert not isinstance(result, BaseException), f"unexpected exception: {result!r}"

    succeeded = [r for r in results if isinstance(r, ReviseSucceeded)]
    replayed = [r for r in results if isinstance(r, ReviseReplayed)]

    assert len(succeeded) == 1
    # Not one conflict: they are all the same request, so none of them lost.
    assert len(replayed) == CONCURRENCY - 1

    async with factory() as session:
        revisions = (
            await session.execute(
                text("SELECT count(*) FROM memory_revisions WHERE memory_id = :m"),
                {"m": memory_id},
            )
        ).scalar_one()
        assert revisions == 2
        await assert_invariants_hold(session)
