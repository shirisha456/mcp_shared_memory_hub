"""Memory write and retrieval.

Two groups matter beyond the happy path:

*Isolation* - a memory recorded in one project must be invisible from another,
proved by querying rather than asserted.

*Stage-0 filtering* - expired memories must vanish from retrieval with no sweeper
running, because expiry is derived at read time. This is the mechanism that
Milestone 3's stale-memory suppression is built on, so it is worth proving now
while it is simple.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from memhub.domain.enums import AuthorKind, MemoryType
from memhub.domain.errors import ValidationFailedError
from memhub.domain.models import ProjectRef
from memhub.persistence.models import Memory
from memhub.services.memories import get_memory, remember, search
from memhub.services.projects import use_project

pytestmark = pytest.mark.integration


@pytest.fixture
async def project(db_session: AsyncSession) -> ProjectRef:
    return await use_project(db_session, slug="ai-agent-control-plane", create=True)


@pytest.fixture
async def other_project(db_session: AsyncSession) -> ProjectRef:
    return await use_project(db_session, slug="expense-tracker", create=True)


class TestRemember:
    async def test_creates_at_revision_one(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        result = await remember(
            db_session,
            project.id,
            memory_type=MemoryType.DECISION,
            content="PostgreSQL is the task queue. Redis is intentionally excluded from V1.",
            tags=["queue", "postgres"],
            source="architecture discussion",
            author_client="claude-desktop",
        )
        assert result.outcome == "created"
        assert result.memory.revision_no == 1
        assert result.memory.type is MemoryType.DECISION
        assert result.memory.tags == ("queue", "postgres")
        assert result.memory.author_client == "claude-desktop"
        assert result.memory.author_kind is AuthorKind.AGENT

    async def test_importance_defaults_by_type(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        decision = await remember(
            db_session, project.id, memory_type=MemoryType.DECISION, content="a decision"
        )
        fact = await remember(db_session, project.id, memory_type=MemoryType.FACT, content="a fact")
        assert decision.memory.importance > fact.memory.importance

    async def test_task_gets_an_automatic_expiry(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        result = await remember(
            db_session,
            project.id,
            memory_type=MemoryType.TASK,
            content="Currently implementing worker heartbeat logic.",
        )
        assert result.memory.expires_at is not None

    async def test_durable_types_have_no_expiry(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        result = await remember(
            db_session, project.id, memory_type=MemoryType.CONSTRAINT, content="No Redis in V1."
        )
        assert result.memory.expires_at is None

    async def test_oversized_content_is_rejected(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        with pytest.raises(ValidationFailedError):
            await remember(db_session, project.id, memory_type=MemoryType.FACT, content="x" * 9000)

    async def test_content_hash_is_stored(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """Computed from Milestone 1 even though deduplication is Milestone 3:
        the column is NOT NULL on an append-only table, so backfilling it later
        would need a data migration."""
        result = await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="hashed"
        )
        row = (
            await db_session.execute(
                text(
                    "SELECT content_hash, hash_version FROM memory_revisions WHERE memory_id = :mid"
                ),
                {"mid": result.memory.memory_id},
            )
        ).one()
        assert len(row[0]) == 32  # sha256
        assert row[1] == 1


class TestSearch:
    async def test_finds_by_substring(self, db_session: AsyncSession, project: ProjectRef) -> None:
        await remember(
            db_session,
            project.id,
            memory_type=MemoryType.DECISION,
            content="PostgreSQL is the task queue.",
        )
        await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="Python 3.12 minimum."
        )

        found = await search(db_session, project.id, query="postgresql")
        assert [m.content for m in found.memories] == ["PostgreSQL is the task queue."]

    async def test_filters_by_type(self, db_session: AsyncSession, project: ProjectRef) -> None:
        await remember(db_session, project.id, memory_type=MemoryType.DECISION, content="d")
        await remember(db_session, project.id, memory_type=MemoryType.FACT, content="f")

        found = await search(db_session, project.id, types=[MemoryType.DECISION])
        assert [m.type for m in found.memories] == [MemoryType.DECISION]

    async def test_tag_filter_requires_all_tags(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="both", tags=["a", "b"]
        )
        await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="one", tags=["a"]
        )

        found = await search(db_session, project.id, tags=["a", "b"])
        assert [m.content for m in found.memories] == ["both"]

    async def test_ordering_is_deterministic(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """Identical calls must return identical order.

        Without the trailing id in the ORDER BY, rows tying on importance and
        timestamp come back in whatever order the plan produced - unstable
        between calls and impossible to snapshot-test.
        """
        for i in range(10):
            await remember(
                db_session,
                project.id,
                memory_type=MemoryType.FACT,
                content=f"fact {i}",
                importance=50,
            )
        first = await search(db_session, project.id, limit=10)
        second = await search(db_session, project.id, limit=10)
        assert [m.memory_id for m in first.memories] == [m.memory_id for m in second.memories]

    async def test_higher_importance_ranks_first(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="low", importance=10
        )
        await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="high", importance=90
        )
        found = await search(db_session, project.id)
        assert [m.content for m in found.memories] == ["high", "low"]

    async def test_total_matched_reports_beyond_the_limit(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        for i in range(5):
            await remember(db_session, project.id, memory_type=MemoryType.FACT, content=f"fact {i}")
        found = await search(db_session, project.id, limit=2)
        assert len(found.memories) == 2
        assert found.total_considered == 5

    async def test_limit_is_bounded(self, db_session: AsyncSession, project: ProjectRef) -> None:
        with pytest.raises(ValidationFailedError):
            await search(db_session, project.id, limit=1000)


class TestProjectIsolation:
    async def test_memories_do_not_leak_between_projects(
        self,
        db_session: AsyncSession,
        project: ProjectRef,
        other_project: ProjectRef,
    ) -> None:
        """ai-agent-control-plane must never see expense-tracker's memories."""
        await remember(
            db_session,
            project.id,
            memory_type=MemoryType.DECISION,
            content="PostgreSQL is the task queue.",
        )
        await remember(
            db_session,
            other_project.id,
            memory_type=MemoryType.DECISION,
            content="SQLite is the task queue.",
        )

        found = await search(db_session, project.id, query="queue")
        assert [m.content for m in found.memories] == ["PostgreSQL is the task queue."]

        other = await search(db_session, other_project.id, query="queue")
        assert [m.content for m in other.memories] == ["SQLite is the task queue."]

    async def test_get_across_projects_returns_nothing(
        self,
        db_session: AsyncSession,
        project: ProjectRef,
        other_project: ProjectRef,
    ) -> None:
        """Deliberately indistinguishable from 'does not exist'.

        Confirming that an id exists in another project would leak across the
        isolation boundary.
        """
        created = await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="secret to project a"
        )
        assert await get_memory(db_session, other_project.id, created.memory.memory_id) is None
        assert await get_memory(db_session, project.id, created.memory.memory_id) is not None

    async def test_unknown_memory_id_returns_none(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        assert await get_memory(db_session, project.id, uuid.uuid4()) is None


class TestExpiry:
    async def test_expired_memories_vanish_from_retrieval(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """No sweeper runs. Expiry is derived at read time from the database
        clock, so the moment expires_at passes, the memory stops being
        retrievable - there is no window in which a stored status lies."""
        created = await remember(
            db_session,
            project.id,
            memory_type=MemoryType.TASK,
            content="Currently implementing worker heartbeat logic.",
        )
        assert (await search(db_session, project.id)).total_considered == 1

        await db_session.execute(
            update(Memory)
            .where(Memory.id == created.memory.memory_id)
            .values(expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
        )

        assert (await search(db_session, project.id)).total_considered == 0
        assert await get_memory(db_session, project.id, created.memory.memory_id) is None

    async def test_the_row_still_exists_after_expiry(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """Invisible to retrieval, but not destroyed - history must survive."""
        created = await remember(
            db_session,
            project.id,
            memory_type=MemoryType.TASK,
            content="short lived",
        )
        await db_session.execute(
            update(Memory)
            .where(Memory.id == created.memory.memory_id)
            .values(expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
        )
        still_there = (
            await db_session.execute(
                text("SELECT count(*) FROM memories WHERE id = :mid"),
                {"mid": created.memory.memory_id},
            )
        ).scalar_one()
        assert still_there == 1
