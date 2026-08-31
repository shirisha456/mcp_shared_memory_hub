"""The embedding outbox.

The claim in the architecture document is that embedding generation belongs
outside the write transaction, driven by a transactional outbox. These tests are
what makes that a property of the system rather than a paragraph:

* a write commits with its job row, atomically, or not at all
* a write succeeds even when the embedder is broken
* the worker's SKIP LOCKED claim lets two workers share a queue safely
* failure backs off and eventually gives up loudly rather than retrying forever
* an unembedded memory is invisible to semantic search and *visible* in the
  coverage number, never a silent hole

Run with the deterministic fake embedder. These test plumbing, not quality -
quality is measured separately in ``tests/eval`` with a real model.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from memhub.domain.enums import MemoryType
from memhub.embeddings.base import EmbeddingError
from memhub.embeddings.fake import HashEmbedder
from memhub.embeddings.worker import MAX_ATTEMPTS, EmbeddingWorker
from memhub.persistence.engine import create_session_factory
from memhub.persistence.repositories.truth import TruthRepository
from memhub.services.memories import remember, revise
from memhub.services.projects import use_project

pytestmark = pytest.mark.integration

MODEL = HashEmbedder().model_name


class BrokenEmbedder:
    """Always fails, to exercise the degradation path."""

    def __init__(self, *, name: str = MODEL) -> None:
        self._name = name
        self.calls = 0

    @property
    def model_name(self) -> str:
        return self._name

    @property
    def dimension(self) -> int:
        return 384

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        raise EmbeddingError("the embedding service is unavailable")


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


async def seed(session: AsyncSession, slug: str, contents: list[str]) -> uuid.UUID:
    project = await use_project(session, slug=slug, create=True)
    for content in contents:
        await remember(
            session,
            project.id,
            memory_type=MemoryType.FACT,
            content=content,
            embedding_model=MODEL,
        )
    return project.id


class TestEnqueue:
    async def test_a_write_enqueues_a_job_in_the_same_transaction(
        self, db_session: AsyncSession
    ) -> None:
        project_id = await seed(db_session, "outbox-enqueue", ["PostgreSQL is the queue."])

        pending = (
            await db_session.execute(
                text(
                    "SELECT count(*) FROM embedding_jobs "
                    "WHERE project_id = :p AND state = 'PENDING'"
                ),
                {"p": project_id},
            )
        ).scalar_one()
        assert pending == 1

    async def test_a_rolled_back_write_leaves_no_job(self, engine: AsyncEngine) -> None:
        """The property that makes this an outbox rather than a dual write.

        If the job could outlive a rolled-back write, the worker would eventually
        try to embed a revision that does not exist.
        """
        factory = create_session_factory(engine)
        async with factory() as session:
            project = await use_project(session, slug="outbox-rollback", create=True)
            await session.commit()

        async with factory() as session:
            await remember(
                session,
                project.id,
                memory_type=MemoryType.FACT,
                content="This write is abandoned.",
                embedding_model=MODEL,
            )
            await session.rollback()

        async with factory() as session:
            jobs = (
                await session.execute(
                    text("SELECT count(*) FROM embedding_jobs WHERE project_id = :p"),
                    {"p": project.id},
                )
            ).scalar_one()
            memories = (
                await session.execute(
                    text("SELECT count(*) FROM memories WHERE project_id = :p"),
                    {"p": project.id},
                )
            ).scalar_one()
        assert (jobs, memories) == (0, 0)

    async def test_a_revision_gets_its_own_job(self, db_session: AsyncSession) -> None:
        """A new revision has new content, so the old vector no longer describes it."""
        project = await use_project(db_session, slug="outbox-revise", create=True)
        created = await remember(
            db_session,
            project.id,
            memory_type=MemoryType.FACT,
            content="First version.",
            embedding_model=MODEL,
        )
        await revise(
            db_session,
            project.id,
            created.memory.memory_id,
            expected_revision=1,
            content="Second version.",
            embedding_model=MODEL,
        )

        revisions = [
            row[0]
            for row in (
                await db_session.execute(
                    text(
                        "SELECT revision_no FROM embedding_jobs "
                        "WHERE memory_id = :m ORDER BY revision_no"
                    ),
                    {"m": created.memory.memory_id},
                )
            ).all()
        ]
        assert revisions == [1, 2]


class TestWorker:
    async def test_drains_the_queue_and_stores_vectors(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        async with sessions() as session:
            project_id = await seed(
                session, "outbox-drain", [f"Memory number {i}." for i in range(20)]
            )
            await session.commit()

        outcome = await EmbeddingWorker(sessions, HashEmbedder()).drain()
        assert outcome.embedded == 20
        assert outcome.failed == 0

        async with sessions() as session:
            vectors = (
                await session.execute(
                    text("SELECT count(*) FROM memory_embeddings WHERE project_id = :p"),
                    {"p": project_id},
                )
            ).scalar_one()
            pending = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM embedding_jobs "
                        "WHERE project_id = :p AND state = 'PENDING'"
                    ),
                    {"p": project_id},
                )
            ).scalar_one()
        assert vectors == 20
        assert pending == 0

    async def test_running_again_does_nothing(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        async with sessions() as session:
            await seed(session, "outbox-idle", ["Only one memory."])
            await session.commit()

        worker = EmbeddingWorker(sessions, HashEmbedder())
        await worker.drain()
        assert (await worker.run_once()).idle

    async def test_two_workers_never_process_the_same_job(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """What SKIP LOCKED buys.

        Both server processes run a worker against one queue. Without SKIP
        LOCKED, the second would block on the first's locks and the pair would
        run at the speed of one; with it, each takes a disjoint batch. Either
        way, a job must never be embedded twice - which the primary key on
        (memory_id, revision_no, model) also enforces underneath.
        """
        async with sessions() as session:
            project_id = await seed(
                session, "outbox-skip-locked", [f"Concurrent memory {i}." for i in range(40)]
            )
            await session.commit()

        workers = [EmbeddingWorker(sessions, HashEmbedder(), batch_size=8) for _ in range(4)]
        outcomes = await asyncio.gather(*(w.drain() for w in workers))

        for outcome in outcomes:
            assert outcome.failed == 0
        assert sum(o.embedded for o in outcomes) == 40, "a job was processed twice or skipped"

        async with sessions() as session:
            vectors = (
                await session.execute(
                    text("SELECT count(*) FROM memory_embeddings WHERE project_id = :p"),
                    {"p": project_id},
                )
            ).scalar_one()
        assert vectors == 40


class TestFailure:
    async def test_a_broken_embedder_does_not_break_writes(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The reason embedding is outside the write transaction.

        Inline generation would make a model outage a *write* outage: you could
        no longer record a decision because an embedder was down.
        """
        async with sessions() as session:
            project_id = await seed(session, "outbox-broken", ["Written regardless."])
            await session.commit()

        outcome = await EmbeddingWorker(sessions, BrokenEmbedder()).run_once()
        assert outcome.failed == 1
        assert outcome.embedded == 0

        async with sessions() as session:
            memories = (
                await session.execute(
                    text("SELECT count(*) FROM memories WHERE project_id = :p"),
                    {"p": project_id},
                )
            ).scalar_one()
        assert memories == 1, "the memory must exist even though its vector does not"

    async def test_failure_backs_off_rather_than_spinning(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        async with sessions() as session:
            project_id = await seed(session, "outbox-backoff", ["Will fail."])
            await session.commit()

        embedder = BrokenEmbedder()
        await EmbeddingWorker(sessions, embedder).run_once()

        async with sessions() as session:
            # Scoped to this project. The module database is shared, so an
            # unfiltered LIMIT 1 would pick up a completed job from another test.
            row = (
                await session.execute(
                    text(
                        "SELECT attempts, state, next_attempt_at > now() AS deferred, "
                        "last_error FROM embedding_jobs WHERE project_id = :p"
                    ),
                    {"p": project_id},
                )
            ).one()
        assert row.attempts == 1
        assert row.state == "PENDING"
        assert row.deferred is True, "a failed job must not be immediately retryable"
        assert "unavailable" in row.last_error

        # The backoff is real: an immediate second pass finds nothing to claim
        # for this model, because the only pending job is deferred.
        assert (await EmbeddingWorker(sessions, embedder).run_once()).idle

    async def test_repeated_failure_ends_in_dead_not_an_infinite_retry(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Bounded because a job retrying forever is an endless log of one error
        that buries everything else."""
        async with sessions() as session:
            project_id = await seed(session, "outbox-dead", ["Will fail permanently."])
            await session.commit()

        worker = EmbeddingWorker(sessions, BrokenEmbedder())
        for _ in range(MAX_ATTEMPTS):
            async with sessions() as session:
                # Clear only this project's backoff, so the loop drives one job
                # to DEAD rather than disturbing anything else in the database.
                await session.execute(
                    text("UPDATE embedding_jobs SET next_attempt_at = now() WHERE project_id = :p"),
                    {"p": project_id},
                )
                await session.commit()
            await worker.run_once()

        async with sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT state, attempts, last_error FROM embedding_jobs "
                        "WHERE project_id = :p"
                    ),
                    {"p": project_id},
                )
            ).one()
        assert row.state == "DEAD"
        assert row.attempts == MAX_ATTEMPTS
        assert row.last_error, "a dead job must record why, or it cannot be diagnosed"


class TestCoverage:
    async def test_coverage_reports_the_pending_window_honestly(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Eventual consistency, stated rather than hidden.

        Between a write and the worker catching up, a memory is findable by full
        text but not semantically. A caller needs to be able to tell "the semantic
        half saw everything" from "it saw half", because those are very different
        results wearing the same shape.
        """
        async with sessions() as session:
            project_id = await seed(session, "outbox-coverage", [f"Memory {i}." for i in range(4)])
            await session.commit()

        async with sessions() as session:
            assert await TruthRepository(session).semantic_coverage(project_id, model=MODEL) == 0.0

        await EmbeddingWorker(sessions, HashEmbedder()).drain()

        async with sessions() as session:
            assert await TruthRepository(session).semantic_coverage(project_id, model=MODEL) == 1.0

    async def test_partial_coverage_is_reported_as_a_fraction(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        async with sessions() as session:
            project_id = await seed(session, "outbox-partial", [f"Memory {i}." for i in range(4)])
            await session.commit()

        await EmbeddingWorker(sessions, HashEmbedder(), batch_size=2).run_once()

        async with sessions() as session:
            coverage = await TruthRepository(session).semantic_coverage(project_id, model=MODEL)
        assert coverage == pytest.approx(0.5)

    async def test_an_empty_project_is_fully_covered(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Zero of zero is complete, not zero - otherwise a new project would
        report a coverage failure it cannot possibly fix."""
        async with sessions() as session:
            project = await use_project(session, slug="outbox-empty", create=True)
            await session.commit()
        async with sessions() as session:
            assert await TruthRepository(session).semantic_coverage(project.id, model=MODEL) == 1.0
