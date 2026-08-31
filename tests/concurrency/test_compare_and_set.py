"""Lost-update prevention, proved rather than asserted.

The scenario this exists for: Claude Desktop reads memory M at revision 4.
Cursor reads the same. Claude writes revision 5. Cursor then submits a change
based on 4. Cursor's write **must not** land - not "should usually not".

Fifty concurrent writers is a synthetic load against the service layer, not
fifty real MCP clients; with stdio there are realistically two. It is still the
right test, because the two real clients are separate OS processes sharing
nothing but PostgreSQL, so the database is the only thing that can adjudicate -
and fifty writers exercise that adjudication far harder than two.

Every test here finishes by running the invariant suite, because "exactly one
succeeded" is not sufficient: the corpus also has to be internally consistent
afterwards.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memhub.domain.enums import MemoryType
from memhub.domain.models import ReviseConflicted, ReviseResult, ReviseSucceeded
from memhub.services.memories import remember, revise
from memhub.services.projects import use_project
from tests.concurrency.conftest import assert_backends_are_distinct, run_together
from tests.integration.test_invariants import assert_invariants_hold

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]

CONCURRENCY = 50


async def seed_memory(
    factory: async_sessionmaker[AsyncSession], slug: str
) -> tuple[uuid.UUID, uuid.UUID]:
    async with factory() as session, session.begin():
        project = await use_project(session, slug=slug, create=True)
        created = await remember(
            session,
            project.id,
            memory_type=MemoryType.DECISION,
            content="Redis is the task queue.",
            author_client="claude-desktop",
        )
    return project.id, created.memory.memory_id


async def test_exactly_one_of_fifty_writers_wins(
    sessions: Callable[[int], async_sessionmaker[AsyncSession]],
) -> None:
    """The headline result: 1 success, 49 conflicts, deterministically.

    Deterministic because of the isolation level. Under READ COMMITTED the
    losers' compare-and-set re-checks its predicate against the winner's
    committed row version and matches zero rows - a clean answer. Under
    SERIALIZABLE the same collision would raise 40001 and the count of
    successes would depend on how many retries each caller made.
    """
    factory = sessions(CONCURRENCY)
    await assert_backends_are_distinct(factory, CONCURRENCY)
    project_id, memory_id = await seed_memory(factory, "cas-basic")

    async def attempt(index: int) -> ReviseResult:
        async with factory() as session, session.begin():
            return await revise(
                session,
                project_id,
                memory_id,
                expected_revision=1,
                content=f"PostgreSQL is the task queue. Writer {index} won.",
                author_client="cursor",
            )

    results = await run_together(attempt, CONCURRENCY)

    for result in results:
        assert not isinstance(result, BaseException), f"unexpected exception: {result!r}"

    succeeded = [r for r in results if isinstance(r, ReviseSucceeded)]
    conflicted = [r for r in results if isinstance(r, ReviseConflicted)]

    assert len(succeeded) == 1, f"expected exactly 1 winner, got {len(succeeded)}"
    assert len(conflicted) == CONCURRENCY - 1

    async with factory() as session:
        state = (
            await session.execute(
                text("SELECT current_revision_no FROM memories WHERE id = :m"),
                {"m": memory_id},
            )
        ).scalar_one()
        revisions = (
            await session.execute(
                text("SELECT count(*) FROM memory_revisions WHERE memory_id = :m"),
                {"m": memory_id},
            )
        ).scalar_one()
        currents = (
            await session.execute(
                text("SELECT count(*) FROM memory_revisions WHERE memory_id = :m AND is_current"),
                {"m": memory_id},
            )
        ).scalar_one()
        content = (
            await session.execute(
                text("SELECT content FROM memory_revisions WHERE memory_id = :m AND is_current"),
                {"m": memory_id},
            )
        ).scalar_one()

    # Exactly one increment happened, not fifty.
    assert state == 2
    assert revisions == 2
    assert currents == 1
    # The surviving content belongs to the writer that reported success.
    assert content == succeeded[0].memory.content

    async with factory() as session:
        await assert_invariants_hold(session)


async def test_losers_are_told_what_beat_them(
    sessions: Callable[[int], async_sessionmaker[AsyncSession]],
) -> None:
    """A conflict must be actionable in one round trip.

    Reporting "conflict" without the current state forces the caller into a
    second read before it can retry, and leaves the model guessing about what
    changed. Every loser gets the winner's revision number, content and author.
    """
    factory = sessions(CONCURRENCY)
    project_id, memory_id = await seed_memory(factory, "cas-conflict-detail")

    async def attempt(index: int) -> ReviseResult:
        async with factory() as session, session.begin():
            return await revise(
                session,
                project_id,
                memory_id,
                expected_revision=1,
                content=f"attempt {index}",
                author_client="cursor",
            )

    results = await run_together(attempt, CONCURRENCY)
    winner = next(r for r in results if isinstance(r, ReviseSucceeded))
    losers = [r for r in results if isinstance(r, ReviseConflicted)]

    # Assert the population before iterating it. Without this, the loop below
    # passes vacuously when there are no losers at all - which is exactly what
    # an experiment removing the version predicate from the CAS produced.
    assert len(losers) == CONCURRENCY - 1

    for loser in losers:
        assert loser.expected_revision == 1
        assert loser.current.revision_no == 2
        assert loser.current.content == winner.memory.content
        assert loser.current.author_client == "cursor"


async def test_a_second_round_advances_by_exactly_one(
    sessions: Callable[[int], async_sessionmaker[AsyncSession]],
) -> None:
    """Revision numbers advance monotonically under repeated contention.

    Guards against an off-by-one that only appears after the first round, and
    against gaps - which would break the 'max(revision_no) = count(*)' invariant
    and make history unreadable.
    """
    factory = sessions(CONCURRENCY)
    project_id, memory_id = await seed_memory(factory, "cas-two-rounds")

    for expected in (1, 2, 3):

        async def attempt(index: int, expected: int = expected) -> ReviseResult:
            async with factory() as session, session.begin():
                return await revise(
                    session,
                    project_id,
                    memory_id,
                    expected_revision=expected,
                    content=f"round {expected} writer {index}",
                    author_client="cursor",
                )

        results = await run_together(attempt, CONCURRENCY)
        for result in results:
            assert not isinstance(result, BaseException), (
                f"round {expected} raised {result!r}. A writer whose transaction "
                "aborted is not the same as one cleanly refused: an IntegrityError "
                "here would mean the compare-and-set is not doing its job and the "
                "unique indexes are catching the fallout instead."
            )
        assert len([r for r in results if isinstance(r, ReviseSucceeded)]) == 1
        assert len([r for r in results if isinstance(r, ReviseConflicted)]) == CONCURRENCY - 1

    async with factory() as session:
        final = (
            await session.execute(
                text("SELECT current_revision_no FROM memories WHERE id = :m"),
                {"m": memory_id},
            )
        ).scalar_one()
        numbers = [
            row[0]
            for row in (
                await session.execute(
                    text(
                        "SELECT revision_no FROM memory_revisions "
                        "WHERE memory_id = :m ORDER BY revision_no"
                    ),
                    {"m": memory_id},
                )
            ).all()
        ]
        await assert_invariants_hold(session)

    assert final == 4
    assert numbers == [1, 2, 3, 4], "revision numbers must be gapless and monotonic"


async def test_history_is_preserved_not_overwritten(
    sessions: Callable[[int], async_sessionmaker[AsyncSession]],
) -> None:
    """The winner's change appends; it does not destroy what came before.

    This is what makes ``memory_history`` possible and what separates a revision
    log from a mutable row.
    """
    factory = sessions(4)
    project_id, memory_id = await seed_memory(factory, "cas-history")

    async def attempt(index: int) -> ReviseResult:
        async with factory() as session, session.begin():
            return await revise(
                session,
                project_id,
                memory_id,
                expected_revision=1,
                content="PostgreSQL is the task queue.",
                author_client="cursor",
            )

    outcomes = await run_together(attempt, 4)
    for outcome in outcomes:
        assert not isinstance(outcome, BaseException), f"unexpected exception: {outcome!r}"

    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT revision_no, content, is_current, author_client "
                    "FROM memory_revisions WHERE memory_id = :m ORDER BY revision_no"
                ),
                {"m": memory_id},
            )
        ).all()

    assert len(rows) == 2
    assert rows[0].revision_no == 1
    assert rows[0].content == "Redis is the task queue."
    assert rows[0].is_current is False
    assert rows[0].author_client == "claude-desktop"

    assert rows[1].revision_no == 2
    assert rows[1].content == "PostgreSQL is the task queue."
    assert rows[1].is_current is True
    assert rows[1].author_client == "cursor"


async def test_every_attempt_is_audited(
    sessions: Callable[[int], async_sessionmaker[AsyncSession]],
) -> None:
    """Conflicts are recorded, not just successes.

    A conflict rate is an operational signal - it tells you two clients are
    fighting over the same knowledge. Auditing only the winners would hide that
    entirely.
    """
    factory = sessions(CONCURRENCY)
    project_id, memory_id = await seed_memory(factory, "cas-audit")

    async def attempt(index: int) -> ReviseResult:
        async with factory() as session, session.begin():
            return await revise(
                session,
                project_id,
                memory_id,
                expected_revision=1,
                content=f"attempt {index}",
                author_client="cursor",
            )

    await run_together(attempt, CONCURRENCY)

    async with factory() as session:
        counts = {
            row[0]: row[1]
            for row in (
                await session.execute(
                    text(
                        "SELECT outcome, count(*) FROM audit_events "
                        "WHERE memory_id = :m AND action = 'revise' GROUP BY outcome"
                    ),
                    {"m": memory_id},
                )
            ).all()
        }

    assert counts.get("ok") == 1
    assert counts.get("conflict") == CONCURRENCY - 1
