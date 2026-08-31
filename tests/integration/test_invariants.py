"""Database invariants.

Two kinds of test here, and both matter.

*Violation attempts.* Try to write a state the design forbids and assert the
database refuses. These prove the constraints are not just declared in the ORM
but actually deployed - a migration that silently failed to create an index
would pass every other test in the suite and fail these.

*The invariant suite.* A set of queries that must return zero rows. From
Milestone 2 this runs after every concurrency test; having it here first means
it is ready, and it already catches a service-layer bug that leaves the
current-revision pointer disagreeing with the revision log.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from memhub.domain.enums import MemoryType
from memhub.domain.models import ProjectRef
from memhub.services.memories import remember
from memhub.services.projects import use_project

pytestmark = pytest.mark.integration


# Every query must return zero rows. Named so a failure reads as the broken
# invariant, not as "a query returned something".
INVARIANT_QUERIES: dict[str, str] = {
    "more than one current revision per memory": """
        SELECT memory_id FROM memory_revisions WHERE is_current
        GROUP BY memory_id HAVING count(*) > 1
    """,
    "current-revision pointer disagrees with the log": """
        SELECT m.id FROM memories m
        JOIN memory_revisions r ON r.memory_id = m.id AND r.is_current
        WHERE r.revision_no <> m.current_revision_no
    """,
    "gap in a revision sequence": """
        SELECT memory_id FROM memory_revisions
        GROUP BY memory_id HAVING max(revision_no) <> count(*)
    """,
    "memory with no current revision": """
        SELECT m.id FROM memories m
        WHERE NOT EXISTS (
            SELECT 1 FROM memory_revisions r
            WHERE r.memory_id = m.id AND r.is_current
        )
    """,
    "superseded without a target": """
        SELECT id FROM memories
        WHERE status = 'SUPERSEDED' AND superseded_by_id IS NULL
    """,
    "revision in a different project from its memory": """
        SELECT r.memory_id FROM memory_revisions r
        JOIN memories m ON m.id = r.memory_id
        WHERE r.project_id <> m.project_id
    """,
    "supersession crossing a project boundary": """
        SELECT a.id FROM memories a
        JOIN memories b ON b.id = a.superseded_by_id
        WHERE a.project_id <> b.project_id
    """,
    "TASK without an expiry": """
        SELECT id FROM memories WHERE type = 'TASK' AND expires_at IS NULL
    """,
}


async def assert_invariants_hold(session: AsyncSession) -> None:
    for description, query in INVARIANT_QUERIES.items():
        rows = (await session.execute(text(query))).all()
        assert rows == [], f"Invariant violated - {description}: {rows}"


@pytest.fixture
async def project(db_session: AsyncSession) -> ProjectRef:
    return await use_project(db_session, slug="invariants", create=True)


class TestConstraintsAreDeployed:
    """Proof that the schema, not the code, is doing the enforcing.

    Each test bypasses the service layer entirely and writes raw SQL, because
    the point is what happens when application logic is wrong or absent.
    """

    async def test_two_current_revisions_are_rejected(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """INVARIANT 1, enforced by uq_memory_revisions_memory_id (partial).

        This is the constraint that makes Milestone 2's compare-and-set safe
        even if the compare-and-set itself were written wrongly.
        """
        created = await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="original"
        )
        with pytest.raises(IntegrityError) as exc:
            await db_session.execute(
                text(
                    "INSERT INTO memory_revisions (memory_id, project_id, revision_no, "
                    "content, content_hash, hash_version, tags, is_current, "
                    "author_client, author_kind) VALUES (:mid, :pid, 2, 'second', "
                    "'\\x00'::bytea, 1, '{}', true, 'test', 'agent')"
                ),
                {"mid": created.memory.memory_id, "pid": project.id},
            )
        assert "uq_memory_revisions_memory_id" in str(exc.value)

    async def test_duplicate_revision_number_is_rejected(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """INVARIANT 2, enforced by PRIMARY KEY (memory_id, revision_no).

        Two concurrent writers cannot both produce revision N.
        """
        created = await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="original"
        )
        with pytest.raises(IntegrityError) as exc:
            await db_session.execute(
                text(
                    "INSERT INTO memory_revisions (memory_id, project_id, revision_no, "
                    "content, content_hash, hash_version, tags, is_current, "
                    "author_client, author_kind) VALUES (:mid, :pid, 1, 'clash', "
                    "'\\x00'::bytea, 1, '{}', false, 'test', 'agent')"
                ),
                {"mid": created.memory.memory_id, "pid": project.id},
            )
        assert "pk_memory_revisions" in str(exc.value)

    async def test_cross_project_supersession_is_unrepresentable(
        self, db_session: AsyncSession
    ) -> None:
        """INVARIANT 4, enforced by the composite foreign key.

        Namespace isolation is not a WHERE clause someone might forget - the
        state simply cannot be written.
        """
        a = await use_project(db_session, slug="proj-alpha", create=True)
        b = await use_project(db_session, slug="proj-beta", create=True)
        in_a = await remember(db_session, a.id, memory_type=MemoryType.FACT, content="in a")
        in_b = await remember(db_session, b.id, memory_type=MemoryType.FACT, content="in b")

        with pytest.raises(IntegrityError) as exc:
            await db_session.execute(
                text(
                    "UPDATE memories SET status='SUPERSEDED', superseded_at=now(), "
                    "superseded_by_id=:other WHERE id=:mine"
                ),
                {"other": in_b.memory.memory_id, "mine": in_a.memory.memory_id},
            )
        assert "fk_memories_superseded_by_id_project_id_memories" in str(exc.value)

    async def test_memory_cannot_supersede_itself(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """INVARIANT 5. A self-supersession is a cycle of length one, and it
        would make the lineage walk in memory_history never terminate."""
        created = await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="self"
        )
        with pytest.raises(IntegrityError) as exc:
            await db_session.execute(
                text(
                    "UPDATE memories SET status='SUPERSEDED', superseded_at=now(), "
                    "superseded_by_id=id WHERE id=:mid"
                ),
                {"mid": created.memory.memory_id},
            )
        assert "ck_memories_no_self_supersede" in str(exc.value)

    async def test_superseded_status_requires_a_target(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """INVARIANT 6. A memory retired by nothing is unretrievable and
        unexplainable - history could not say what replaced it."""
        created = await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="orphan"
        )
        with pytest.raises(IntegrityError) as exc:
            await db_session.execute(
                text("UPDATE memories SET status='SUPERSEDED', superseded_at=now() WHERE id=:mid"),
                {"mid": created.memory.memory_id},
            )
        assert "ck_memories_superseded_has_target" in str(exc.value)

    async def test_task_without_expiry_is_rejected(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """INVARIANT 11. The schema-level guard against TASK becoming an
        issue tracker: working state that never expires is not working state."""
        with pytest.raises(IntegrityError) as exc:
            await db_session.execute(
                text(
                    "INSERT INTO memories (id, project_id, type, status, "
                    "current_revision_no, importance) "
                    "VALUES (:id, :pid, 'TASK', 'ACTIVE', 1, 40)"
                ),
                {"id": uuid.uuid4(), "pid": project.id},
            )
        assert "ck_memories_task_needs_ttl" in str(exc.value)

    async def test_unknown_memory_type_is_rejected(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        with pytest.raises(IntegrityError) as exc:
            await db_session.execute(
                text(
                    "INSERT INTO memories (id, project_id, type, status, "
                    "current_revision_no, importance) "
                    "VALUES (:id, :pid, 'OBSERVATION', 'ACTIVE', 1, 50)"
                ),
                {"id": uuid.uuid4(), "pid": project.id},
            )
        assert "ck_memories_type_known" in str(exc.value)

    async def test_oversized_content_is_rejected_by_the_database(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """The service validator gives the good message; the constraint is the
        guarantee. This test proves the guarantee exists independently."""
        memory_id = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO memories (id, project_id, type, status, "
                "current_revision_no, importance) "
                "VALUES (:id, :pid, 'FACT', 'ACTIVE', 1, 50)"
            ),
            {"id": memory_id, "pid": project.id},
        )
        with pytest.raises(IntegrityError) as exc:
            await db_session.execute(
                text(
                    "INSERT INTO memory_revisions (memory_id, project_id, revision_no, "
                    "content, content_hash, hash_version, tags, is_current, "
                    "author_client, author_kind) VALUES (:mid, :pid, 1, :content, "
                    "'\\x00'::bytea, 1, '{}', true, 'test', 'agent')"
                ),
                {"mid": memory_id, "pid": project.id, "content": "x" * 9000},
            )
        assert "ck_memory_revisions_content_length" in str(exc.value)


class TestInvariantSuite:
    async def test_holds_on_an_empty_database(self, db_session: AsyncSession) -> None:
        await assert_invariants_hold(db_session)

    async def test_holds_after_a_realistic_workload(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        other = await use_project(db_session, slug="second-project", create=True)
        for i in range(20):
            await remember(
                db_session,
                project.id if i % 2 else other.id,
                memory_type=[MemoryType.FACT, MemoryType.DECISION, MemoryType.TASK][i % 3],
                content=f"memory {i}",
                tags=[f"tag{i % 4}"],
            )
        await assert_invariants_hold(db_session)
