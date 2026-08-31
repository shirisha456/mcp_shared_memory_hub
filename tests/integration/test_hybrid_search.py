"""Hybrid retrieval, over the fake embedder.

These check the *mechanics* - both retrievers contribute, fusion rehydrates
results only one of them found, degradation is reported, and the stage-0 filter
still holds. They deliberately do not check quality: the fake embedder carries no
semantic signal, so any nDCG measured here would be noise. Quality is measured in
``tests/eval`` against a real model.

The last group is the one that matters most. Adding a whole second retriever is
exactly the kind of change that could quietly reintroduce retired memories, and
it does not - because both retrievers compose on the same filter rather than
each applying their own.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from memhub.domain.enums import MemoryType
from memhub.domain.models import ProjectRef
from memhub.embeddings.base import EmbeddingError
from memhub.embeddings.fake import HashEmbedder
from memhub.embeddings.worker import EmbeddingWorker
from memhub.persistence.engine import create_session_factory
from memhub.services.memories import remember
from memhub.services.projects import use_project
from memhub.services.retrieval import hybrid_search

pytestmark = pytest.mark.integration

MODEL = HashEmbedder().model_name


class BrokenEmbedder:
    @property
    def model_name(self) -> str:
        return MODEL

    @property
    def dimension(self) -> int:
        return 384

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbeddingError("model unavailable")


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


async def seeded_project(
    sessions: async_sessionmaker[AsyncSession],
    slug: str,
    contents: list[str],
    *,
    embed: bool = True,
) -> ProjectRef:
    async with sessions() as session:
        project = await use_project(session, slug=slug, create=True)
        for content in contents:
            await remember(
                session,
                project.id,
                memory_type=MemoryType.FACT,
                content=content,
                embedding_model=MODEL,
            )
        await session.commit()
    if embed:
        await EmbeddingWorker(sessions, HashEmbedder()).drain()
    return project


class TestMechanics:
    async def test_returns_results_and_reports_full_coverage(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        project = await seeded_project(
            sessions,
            "hybrid-basic",
            ["PostgreSQL is the task queue.", "Redis caches sessions.", "Python 3.12 minimum."],
        )
        async with sessions() as session:
            found = await hybrid_search(session, project.id, query="queue", embedder=HashEmbedder())

        assert found.match_strategy == "hybrid"
        assert found.semantic_coverage == pytest.approx(1.0)
        assert found.degraded is None
        assert found.returned_count() > 0

    async def test_a_semantic_only_hit_is_hydrated(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Fusion may promote a memory the lexical retriever never returned.

        Its row was therefore never fetched, and the service has to go back for
        it. Missing this would drop exactly the results hybrid retrieval exists
        to add.
        """
        project = await seeded_project(
            sessions, "hybrid-hydrate", [f"Unrelated memory number {i}." for i in range(30)]
        )
        async with sessions() as session:
            found = await hybrid_search(
                session, project.id, query="nothing matches this lexically", embedder=HashEmbedder()
            )

        # The fake embedder gives arbitrary neighbours, but every returned result
        # must still be a fully populated memory rather than a bare id.
        for memory in found.memories:
            assert memory.content
            assert memory.project_id == project.id

    async def test_coverage_is_reported_when_embedding_is_behind(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The eventual-consistency window, made visible.

        A caller has to be able to tell "the semantic half saw everything" from
        "it saw none of it" - those are very different answers wearing the same
        shape.
        """
        project = await seeded_project(
            sessions, "hybrid-uncovered", ["Written but not yet embedded."], embed=False
        )
        async with sessions() as session:
            found = await hybrid_search(
                session, project.id, query="written", embedder=HashEmbedder()
            )

        assert found.semantic_coverage == pytest.approx(0.0)
        # Still findable: full text does not depend on the outbox.
        assert found.returned_count() == 1


class TestDegradation:
    async def test_a_broken_embedder_degrades_to_lexical_and_says_so(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Search must survive the embedder being down.

        And the caller must be told, rather than receiving a quietly worse answer
        that looks identical to a good one.
        """
        project = await seeded_project(
            sessions, "hybrid-degraded", ["PostgreSQL is the task queue."]
        )
        async with sessions() as session:
            found = await hybrid_search(
                session, project.id, query="queue", embedder=BrokenEmbedder()
            )

        assert found.returned_count() == 1
        assert found.degraded is not None
        assert "lexical_only" in found.degraded


class TestSuppressionStillHolds:
    async def test_a_second_retriever_cannot_resurrect_a_retired_memory(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The guarantee that has to survive every retrieval change.

        A vector index is the most likely place for a retired memory to leak
        back, because similarity has no opinion about what is current. It cannot
        happen here: the semantic retriever composes on the same stage-0 filter,
        so a superseded memory is not ranked low, it is not a candidate.
        """
        async with sessions() as session:
            project = await use_project(session, slug="hybrid-suppression", create=True)
            old = await remember(
                session,
                project.id,
                memory_type=MemoryType.FACT,
                content="Redis is the task queue.",
                importance=100,
                embedding_model=MODEL,
            )
            await remember(
                session,
                project.id,
                memory_type=MemoryType.DECISION,
                content="PostgreSQL is the task queue now. Redis was removed.",
                supersedes=[old.memory.memory_id],
                importance=10,
                embedding_model=MODEL,
            )
            await session.commit()

        # Embed everything, including the memory that was later retired.
        await EmbeddingWorker(sessions, HashEmbedder()).drain()

        async with sessions() as session:
            for query in ("redis", "task queue", "what queue is used"):
                found = await hybrid_search(
                    session, project.id, query=query, embedder=HashEmbedder(), limit=50
                )
                contents = [m.content for m in found.memories]
                assert "Redis is the task queue." not in contents, (
                    f"the superseded memory came back for {query!r} - the semantic "
                    "retriever is not composing on the stage-0 filter"
                )

    async def test_hybrid_respects_project_isolation(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await seeded_project(sessions, "hybrid-iso-a", ["Belongs to project A."])
        b = await seeded_project(sessions, "hybrid-iso-b", ["Belongs to project B."])

        async with sessions() as session:
            found = await hybrid_search(
                session, b.id, query="belongs project", embedder=HashEmbedder(), limit=50
            )
        assert [m.content for m in found.memories] == ["Belongs to project B."]
