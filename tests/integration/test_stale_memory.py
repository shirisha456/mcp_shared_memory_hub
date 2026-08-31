"""The stale-memory demo, as a test.

This is the thesis of the project, executed rather than claimed. A demo that is
also a test cannot rot.

The scenario: a project once used Redis as its task queue and now uses
PostgreSQL. Both statements were true when written. Only one is true now. A
retrieval-only system - vector store, grep over markdown, a notes database -
returns both and lets similarity decide, which is exactly wrong: similarity has
no opinion about which fact is current, and the stale phrasing often matches the
query *better*.

The assertion that matters is not "the right answer ranks first". It is **"the
wrong answer is absent at every limit"**. Ranking can bury a stale fact; only
structure can exclude it. If suppression were a ranking effect it would leak the
moment someone asked for more results.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from memhub.domain.enums import MemoryStatus, MemoryType
from memhub.domain.models import ProjectRef
from memhub.services.memories import forget, history, remember, revise, search
from memhub.services.projects import use_project

pytestmark = pytest.mark.integration

REDIS = "Redis is the task queue."
POSTGRES = "PostgreSQL is the source of truth for task state and queueing. Redis was removed in V1."


@pytest.fixture
async def project(db_session: AsyncSession) -> ProjectRef:
    return await use_project(db_session, slug="ai-agent-control-plane", create=True)


class TestSupersession:
    async def test_the_retired_fact_leaves_retrieval_entirely(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """The flagship assertion.

        Note the loop over limits. Asserting only that PostgreSQL ranks first
        would pass even if Redis were merely ranked lower - and it would then
        leak the moment a caller asked for more results. Absence at every limit
        is what proves suppression is structural rather than a ranking accident.
        """
        old = await remember(
            db_session,
            project.id,
            memory_type=MemoryType.FACT,
            content=REDIS,
            author_client="claude-desktop",
        )

        new = await remember(
            db_session,
            project.id,
            memory_type=MemoryType.DECISION,
            content=POSTGRES,
            supersedes=[old.memory.memory_id],
            author_client="cursor",
        )
        assert new.superseded == (old.memory.memory_id,)
        assert new.not_superseded == ()

        for limit in (1, 2, 5, 10, 50, 100):
            for query in ("queue", "task queue", "redis", None):
                found = await search(db_session, project.id, query=query, limit=limit)
                contents = [m.content for m in found.memories]
                assert REDIS not in contents, (
                    f"the superseded Redis memory surfaced at limit={limit}, "
                    f"query={query!r}. Suppression must be structural, not a "
                    "ranking effect."
                )

    async def test_querying_the_stale_term_still_finds_only_the_current_answer(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """The hardest case for a similarity-based system.

        Searching for "redis" matches the retired memory far better than the
        current one. A vector store would return the retired fact first. Here the
        stage-0 filter removes it before ranking is even consulted, so the only
        thing that can match is the current decision - which does mention Redis,
        correctly, as something that was removed.
        """
        old = await remember(db_session, project.id, memory_type=MemoryType.FACT, content=REDIS)
        await remember(
            db_session,
            project.id,
            memory_type=MemoryType.DECISION,
            content=POSTGRES,
            supersedes=[old.memory.memory_id],
        )

        found = await search(db_session, project.id, query="redis")
        assert [m.content for m in found.memories] == [POSTGRES]

    async def test_history_still_shows_the_retired_fact(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """Suppression must not become deletion.

        Retirement removes a memory from retrieval; it must not remove it from
        the record. Without this, the system is just deleting inconvenient
        history and nobody can ask why the project changed its mind.
        """
        old = await remember(
            db_session,
            project.id,
            memory_type=MemoryType.FACT,
            content=REDIS,
            author_client="claude-desktop",
        )
        new = await remember(
            db_session,
            project.id,
            memory_type=MemoryType.DECISION,
            content=POSTGRES,
            supersedes=[old.memory.memory_id],
            author_client="cursor",
        )

        record = await history(db_session, project.id, old.memory.memory_id)

        assert record.memory.status is MemoryStatus.SUPERSEDED
        assert record.memory.content == REDIS
        assert record.superseded_by is not None
        assert record.superseded_by.memory_id == new.memory.memory_id
        assert record.superseded_by.content == POSTGRES
        assert record.superseded_by.at is not None
        # Provenance survives: it is still visible who asserted the retired fact.
        assert record.revisions[0].author_client == "claude-desktop"

        forward = await history(db_session, project.id, new.memory.memory_id)
        assert [s.memory_id for s in forward.supersedes] == [old.memory.memory_id]

    async def test_one_memory_can_retire_several(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """The many-to-one shape that distinguishes supersession from revision.

        A revision chain cannot express this: three separate facts, each with its
        own author and timestamp, all replaced by one consolidating decision.
        """
        olds = [
            await remember(
                db_session,
                project.id,
                memory_type=MemoryType.FACT,
                content=f"Queue detail number {i}.",
            )
            for i in range(3)
        ]
        new = await remember(
            db_session,
            project.id,
            memory_type=MemoryType.DECISION,
            content="Consolidated queue design: PostgreSQL, FOR UPDATE SKIP LOCKED.",
            supersedes=[o.memory.memory_id for o in olds],
        )

        assert len(new.superseded) == 3
        found = await search(db_session, project.id, limit=100)
        assert [m.content for m in found.memories] == [
            "Consolidated queue design: PostgreSQL, FOR UPDATE SKIP LOCKED."
        ]

    async def test_already_retired_targets_are_reported_not_silently_skipped(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """ "I retired 2 memories" when only 1 existed is a lie the caller acts on."""
        old = await remember(db_session, project.id, memory_type=MemoryType.FACT, content=REDIS)
        await forget(db_session, project.id, old.memory.memory_id)

        new = await remember(
            db_session,
            project.id,
            memory_type=MemoryType.DECISION,
            content=POSTGRES,
            supersedes=[old.memory.memory_id],
        )
        assert new.superseded == ()
        assert new.not_superseded == (old.memory.memory_id,)

    async def test_supersession_cannot_cross_a_project(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """Reported as not-superseded rather than raising.

        The composite foreign key makes the state unrepresentable; the
        compare-and-set is project-scoped, so the attempt simply matches no rows.
        Two layers, and the caller gets a clear answer either way.
        """
        other = await use_project(db_session, slug="expense-tracker", create=True)
        foreign = await remember(
            db_session, other.id, memory_type=MemoryType.FACT, content="Belongs elsewhere."
        )

        new = await remember(
            db_session,
            project.id,
            memory_type=MemoryType.DECISION,
            content=POSTGRES,
            supersedes=[foreign.memory.memory_id],
        )
        assert new.not_superseded == (foreign.memory.memory_id,)

        # The foreign memory is untouched in its own project.
        still_there = await search(db_session, other.id)
        assert [m.content for m in still_there.memories] == ["Belongs elsewhere."]


class TestForget:
    async def test_forgotten_memories_leave_retrieval(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        created = await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="Temporary note."
        )
        assert (await search(db_session, project.id)).total_considered == 1

        result = await forget(db_session, project.id, created.memory.memory_id)
        assert result.outcome == "forgotten"
        assert (await search(db_session, project.id)).total_considered == 0

    async def test_forgetting_twice_is_a_no_op(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """Not an error: forgetting something twice is not a mistake."""
        created = await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="Temporary note."
        )
        first = await forget(db_session, project.id, created.memory.memory_id)
        second = await forget(db_session, project.id, created.memory.memory_id)

        assert first.outcome == "forgotten"
        assert second.outcome == "already_forgotten"

    async def test_content_survives_forgetting(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """Tombstone, not destruction. Only an operator purge erases content."""
        created = await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="Temporary note."
        )
        await forget(db_session, project.id, created.memory.memory_id)

        record = await history(db_session, project.id, created.memory.memory_id)
        assert record.memory.status is MemoryStatus.DELETED
        assert record.revisions[0].content == "Temporary note."

    async def test_a_revised_memory_can_be_forgotten_and_keeps_all_revisions(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        created = await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="First version."
        )
        await revise(
            db_session,
            project.id,
            created.memory.memory_id,
            expected_revision=1,
            content="Second version.",
        )
        await forget(db_session, project.id, created.memory.memory_id)

        record = await history(db_session, project.id, created.memory.memory_id)
        assert [r.content for r in record.revisions] == ["First version.", "Second version."]
        assert [r.is_current for r in record.revisions] == [False, True]


class TestRetirementReleasesContent:
    async def test_a_retired_sentence_can_be_asserted_again(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """The reason the dedup key is a separate table with its own lifecycle.

        If retiring a memory did not release its content hash, the sentence would
        be permanently unstorable - including when a decision is legitimately
        reversed and the project goes back to what it said before.
        """
        first = await remember(db_session, project.id, memory_type=MemoryType.FACT, content=REDIS)
        await forget(db_session, project.id, first.memory.memory_id)

        again = await remember(db_session, project.id, memory_type=MemoryType.FACT, content=REDIS)
        assert again.outcome == "created"
        assert again.memory.memory_id != first.memory.memory_id
        assert [m.content for m in (await search(db_session, project.id)).memories] == [REDIS]
