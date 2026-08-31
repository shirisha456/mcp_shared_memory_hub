"""The failure matrix, as tests.

Architecture section 9 lists what this system does when things go wrong. A list
like that is a claim until something checks it, and the rows most worth checking
are the ones where the honest answer is "it degrades" rather than "it works" -
because a degradation that was never exercised is just an untested code path with
optimistic prose attached.

Rows covered elsewhere are not repeated here: concurrent writes in
``tests/concurrency``, duplicate writes and retries in the idempotency tests, the
embedder being down in ``test_embedding_outbox``, malformed input in
``tests/protocol``. This module covers what was left: the database being absent
or slow, transactions that must not partially apply, schema drift, and the
destructive operator path.

``docs/failure-modes.md`` maps every row of the matrix to the test that covers it.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from memhub.cli.admin import collect_garbage, purge, status
from memhub.config import Settings
from memhub.domain.enums import MemoryType
from memhub.domain.models import ProjectRef
from memhub.persistence.engine import create_engine
from memhub.persistence.schema import (
    SchemaMismatchError,
    applied_revision,
    expected_revision,
    verify,
)
from memhub.services.memories import forget, history, remember, search
from memhub.services.projects import use_project

pytestmark = [pytest.mark.integration, pytest.mark.failure]


@pytest.fixture
async def project(db_session: AsyncSession) -> ProjectRef:
    return await use_project(db_session, slug="failure-modes", create=True)


class TestDatabaseUnavailable:
    async def test_connecting_to_a_dead_database_fails_fast(self) -> None:
        """The bounded pool timeout, from the caller's side.

        A server whose database has gone away must say so quickly. Blocking
        indefinitely turns one outage into a hung client, and on stdio a hung
        server is indistinguishable from a crashed one.
        """
        settings = Settings(
            database_url="postgresql+asyncpg://memhub:memhub@localhost:59999/memhub",
            db_pool_timeout_s=1.0,
        )
        engine = create_engine(settings)
        try:
            with pytest.raises((OperationalError, DBAPIError, OSError)):
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    async def test_a_statement_timeout_is_enforced_by_the_server(
        self, db_session: AsyncSession
    ) -> None:
        """A runaway query is killed by PostgreSQL, not merely abandoned.

        An application-side timeout leaves the backend burning CPU on a query
        nobody is waiting for any more. Setting it server-side means the work
        actually stops.
        """
        await db_session.execute(text("SET LOCAL statement_timeout = '100ms'"))
        with pytest.raises(DBAPIError) as exc:
            await db_session.execute(text("SELECT pg_sleep(3)"))
        assert "canceling statement" in str(exc.value).lower()

    async def test_the_configured_timeout_reaches_the_backend(self, engine: AsyncEngine) -> None:
        settings = Settings()
        async with engine.connect() as conn:
            applied = (
                await conn.execute(
                    text("SELECT setting FROM pg_settings WHERE name = 'statement_timeout'")
                )
            ).scalar_one()
        assert int(applied) == settings.db_statement_timeout_ms


class TestPartialWrites:
    async def test_a_failed_write_leaves_nothing_behind(self, engine: AsyncEngine) -> None:
        """One transaction, so there is no half-written memory to find.

        ``remember`` touches five tables. If a failure part-way could commit some
        of them, the invariant suite would start finding memories with no current
        revision - and no code path would ever repair them.
        """
        from memhub.persistence.engine import create_session_factory

        sessions = create_session_factory(engine)
        async with sessions() as session:
            ref = await use_project(session, slug="failure-partial", create=True)
            await session.commit()

        async with sessions() as session:
            await remember(
                session,
                ref.id,
                memory_type=MemoryType.FACT,
                content="This write will be abandoned.",
                embedding_model="fake-hash-v1",
            )
            # Whatever went wrong, the effect is the same as never having started.
            await session.rollback()

        async with sessions() as session:
            counts = (
                await session.execute(
                    text(
                        "SELECT (SELECT count(*) FROM memories WHERE project_id = :p) AS m,"
                        "       (SELECT count(*) FROM memory_revisions WHERE project_id = :p) AS r,"
                        "       (SELECT count(*) FROM embedding_jobs WHERE project_id = :p) AS j,"
                        "       (SELECT count(*) FROM memory_dedup_keys WHERE project_id = :p) AS d"
                    ),
                    {"p": ref.id},
                )
            ).one()
        assert (counts.m, counts.r, counts.j, counts.d) == (0, 0, 0, 0)

    async def test_a_constraint_violation_does_not_leave_an_orphan(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """The database refusing a write must undo the whole attempt.

        A TASK without an expiry is rejected by a CHECK constraint. What matters
        is that the rejection leaves no partially-inserted rows.
        """
        from sqlalchemy.exc import IntegrityError

        before = (
            await db_session.execute(
                text("SELECT count(*) FROM memories WHERE project_id = :p"),
                {"p": project.id},
            )
        ).scalar_one()

        savepoint = await db_session.begin_nested()
        with pytest.raises(IntegrityError):
            await db_session.execute(
                text(
                    "INSERT INTO memories (id, project_id, type, status, "
                    "current_revision_no, importance) VALUES "
                    "(:i, :p, 'TASK', 'ACTIVE', 1, 40)"
                ),
                {"i": uuid.uuid4(), "p": project.id},
            )
        await savepoint.rollback()

        after = (
            await db_session.execute(
                text("SELECT count(*) FROM memories WHERE project_id = :p"),
                {"p": project.id},
            )
        ).scalar_one()
        assert after == before


class TestSchemaDrift:
    async def test_a_migrated_database_verifies(self, engine: AsyncEngine) -> None:
        assert await verify(engine) == expected_revision()

    async def test_the_applied_revision_is_readable(self, engine: AsyncEngine) -> None:
        assert await applied_revision(engine) == expected_revision()

    async def test_an_unmigrated_database_is_refused_with_the_remedy(
        self, settings: Settings, test_database: str
    ) -> None:
        """Refusing to start is the loud failure; the quiet one is worse.

        Without this the server starts happily and fails on the first query that
        touches a missing column, which the client reports as the tool being
        broken. Nobody looks at migrations.
        """
        import asyncpg

        from tests.conftest import _asyncpg_dsn

        url = settings.sqlalchemy_url
        blank = f"{test_database}_blank"
        admin = await asyncpg.connect(dsn=_asyncpg_dsn(url, "postgres"))
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{blank}"')
            await admin.execute(f'CREATE DATABASE "{blank}"')
        finally:
            await admin.close()

        engine = create_engine(
            settings, url=url.set(database=blank).render_as_string(hide_password=False)
        )
        try:
            with pytest.raises(SchemaMismatchError, match="alembic upgrade head"):
                await verify(engine)
        finally:
            await engine.dispose()
            admin = await asyncpg.connect(dsn=_asyncpg_dsn(url, "postgres"))
            try:
                await admin.execute(f'DROP DATABASE IF EXISTS "{blank}"')
            finally:
                await admin.close()

    async def test_an_unknown_revision_is_refused_as_too_new(self, engine: AsyncEngine) -> None:
        """The more dangerous direction, and the one easy to get wrong.

        A database migrated by newer code mostly works - until this process
        writes a row that newer constraints were added to prevent. So it is
        refused, and the message says to upgrade the code rather than downgrade
        the database.
        """
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE alembic_version SET version_num = '9999'"))
        try:
            with pytest.raises(SchemaMismatchError, match="does not know about"):
                await verify(engine)
        finally:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE alembic_version SET version_num = :v"),
                    {"v": expected_revision()},
                )


class TestOperatorPurge:
    async def test_purge_actually_erases_content(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """The one operation that destroys rather than hides.

        Soft delete is the right default and the wrong tool for the case this
        exists for: a credential recorded by mistake is still in the database
        after a tombstone.
        """
        secret = "The staging API key is sk-live-DO-NOT-COMMIT-abc123."
        created = await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content=secret
        )
        memory_id = created.memory.memory_id

        # Forget alone hides it. The content is still there.
        await forget(db_session, project.id, memory_id)
        still_present = (
            await db_session.execute(
                text("SELECT count(*) FROM memory_revisions WHERE content = :c"),
                {"c": secret},
            )
        ).scalar_one()
        assert still_present == 1, (
            "forget is a tombstone, not an erasure - which is exactly why the "
            "tool description points at the operator purge for leaked secrets"
        )

        await purge(
            db_session, project_id=project.id, memory_id=memory_id, reason="leaked credential"
        )

        gone = (
            await db_session.execute(
                text("SELECT count(*) FROM memory_revisions WHERE content = :c"),
                {"c": secret},
            )
        ).scalar_one()
        assert gone == 0

    async def test_purge_clears_every_derived_table(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """A partial erasure of a leaked credential is not an erasure."""
        created = await remember(
            db_session,
            project.id,
            memory_type=MemoryType.FACT,
            content="Another secret to remove entirely.",
            embedding_model="fake-hash-v1",
        )
        memory_id = created.memory.memory_id

        await purge(db_session, project_id=project.id, memory_id=memory_id, reason="test")

        for table in (
            "memory_revisions",
            "memory_dedup_keys",
            "memory_attestations",
            "embedding_jobs",
            "memory_embeddings",
        ):
            left = (
                await db_session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE memory_id = :m"),
                    {"m": memory_id},
                )
            ).scalar_one()
            assert left == 0, f"{table} still holds rows for a purged memory"

    async def test_the_audit_record_outlives_the_memory(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """Why audit_events has no foreign key to memories.

        A CASCADE would erase the evidence along with the subject, and "this was
        purged, by whom, and why" is precisely what must survive.
        """
        created = await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="To be purged."
        )
        memory_id = created.memory.memory_id
        await purge(
            db_session, project_id=project.id, memory_id=memory_id, reason="regulatory request"
        )

        events = (
            await db_session.execute(
                text(
                    "SELECT action, outcome, actor_client, detail FROM audit_events "
                    "WHERE memory_id = :m AND action = 'purge'"
                ),
                {"m": memory_id},
            )
        ).all()
        assert len(events) == 1
        assert events[0].actor_client == "operator"
        assert events[0].detail["reason"] == "regulatory request"

    async def test_earlier_audit_detail_is_redacted(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """The audit trail must not become the place the secret survives."""
        created = await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="Sensitive."
        )
        memory_id = created.memory.memory_id
        await purge(db_session, project_id=project.id, memory_id=memory_id, reason="test")

        rows = (
            await db_session.execute(
                text("SELECT detail FROM audit_events WHERE memory_id = :m AND action <> 'purge'"),
                {"m": memory_id},
            )
        ).all()
        assert all(row.detail == {"redacted": True} for row in rows)

    async def test_purging_a_superseding_memory_releases_its_dependents(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """The foreign key would otherwise refuse the delete halfway through.

        A memory retired *by* the one being purged points at it. Purge has to
        release those first, or it fails after having already destroyed several
        tables' worth of rows.
        """
        old = await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="The old fact."
        )
        new = await remember(
            db_session,
            project.id,
            memory_type=MemoryType.DECISION,
            content="The replacement fact.",
            supersedes=[old.memory.memory_id],
        )

        await purge(
            db_session, project_id=project.id, memory_id=new.memory.memory_id, reason="test"
        )

        record = await history(db_session, project.id, old.memory.memory_id)
        assert record.superseded_by is None
        assert (await search(db_session, project.id)).returned_count() == 0

    async def test_purging_an_unknown_memory_is_refused(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        with pytest.raises(SystemExit, match="no memory"):
            await purge(db_session, project_id=project.id, memory_id=uuid.uuid4(), reason="test")

    async def test_purge_is_project_scoped(self, db_session: AsyncSession) -> None:
        """Even the destructive path cannot reach across a project boundary."""
        a = await use_project(db_session, slug="purge-scope-a", create=True)
        b = await use_project(db_session, slug="purge-scope-b", create=True)
        created = await remember(
            db_session, a.id, memory_type=MemoryType.FACT, content="Belongs to A."
        )

        with pytest.raises(SystemExit, match="no memory"):
            await purge(
                db_session, project_id=b.id, memory_id=created.memory.memory_id, reason="test"
            )


class TestRetention:
    async def test_garbage_collection_never_removes_a_memory(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """Retention that silently deleted project knowledge would be
        indistinguishable from data loss, so it only touches machinery."""
        await remember(db_session, project.id, memory_type=MemoryType.FACT, content="Keep me.")
        before = (
            await db_session.execute(
                text("SELECT count(*) FROM memories WHERE project_id = :p"), {"p": project.id}
            )
        ).scalar_one()

        await collect_garbage(db_session)

        after = (
            await db_session.execute(
                text("SELECT count(*) FROM memories WHERE project_id = :p"), {"p": project.id}
            )
        ).scalar_one()
        assert after == before

    async def test_expired_idempotency_keys_are_collected(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        await remember(
            db_session,
            project.id,
            memory_type=MemoryType.FACT,
            content="With a key.",
            client_request_id="gc-key-000001",
        )
        await db_session.execute(
            text("UPDATE idempotency_keys SET expires_at = now() - interval '1 day'")
        )

        removed = await collect_garbage(db_session)
        assert removed["idempotency_keys"] >= 1

    async def test_status_reports_the_operational_picture(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """What an operator needs to answer "is anything stuck?"."""
        await remember(
            db_session,
            project.id,
            memory_type=MemoryType.FACT,
            content="Pending an embedding.",
            embedding_model="fake-hash-v1",
        )
        summary = await status(db_session)

        assert summary["active"] >= 1
        assert summary["embed_pending"] >= 1
        assert "embed_dead" in summary
