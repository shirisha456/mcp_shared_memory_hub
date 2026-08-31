"""Full-text retrieval.

What changes here is *ordering*, not what is visible: the stage-0 filter is
untouched, so everything the previous milestones guaranteed about superseded,
deleted and expired memories still holds. These tests cover the two things
full-text adds - matching on word forms rather than substrings, and ranking by
relevance scaled by priors - plus the ways the old substring behaviour differed.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from memhub.domain.enums import MemoryType
from memhub.domain.models import ProjectRef
from memhub.persistence.models import Memory
from memhub.services.memories import remember, search
from memhub.services.projects import use_project

pytestmark = pytest.mark.integration


@pytest.fixture
async def project(db_session: AsyncSession) -> ProjectRef:
    return await use_project(db_session, slug="fts", create=True)


class TestMatching:
    async def test_stemming_finds_related_word_forms(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """The main thing full-text buys over substring matching.

        A substring search for "queueing" cannot find "queues", and a developer
        asking about queues should not have to guess which form was recorded.
        """
        await remember(
            db_session,
            project.id,
            memory_type=MemoryType.DECISION,
            content="PostgreSQL queues tasks using FOR UPDATE SKIP LOCKED.",
        )
        for term in ("queue", "queues", "queueing"):
            found = await search(db_session, project.id, query=term)
            assert found.returned_count() == 1, f"{term!r} matched nothing"

    async def test_stemming_is_imperfect_and_this_is_why_semantic_search_is_coming(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """A documented limitation, pinned so it cannot surprise us later.

        The English Snowball stemmer reduces "queue", "queues" and "queueing" to
        the lexeme ``queue``, but "queued" to ``queu`` - so a query for "queued"
        does **not** match a memory that says "queues". Nothing is misconfigured;
        that is simply what the stemmer does.

        This is the honest argument for semantic retrieval, and it is worth more
        as a failing case we can point at than as an assertion in a design
        document. Milestone 6 will measure how often it costs us, and Milestone 7
        will add the vector retriever that covers it. Recording the baseline
        weakness first is the whole reason the evaluation harness comes before
        the embeddings.
        """
        await remember(
            db_session,
            project.id,
            memory_type=MemoryType.DECISION,
            content="PostgreSQL queues tasks using FOR UPDATE SKIP LOCKED.",
        )
        found = await search(db_session, project.id, query="queued")
        assert found.returned_count() == 0

    async def test_stop_words_do_not_match_everything(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """Substring search for "is" matched nearly every memory. Full-text
        discards stop words, so the query becomes empty and matches nothing."""
        await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="Python is required."
        )
        found = await search(db_session, project.id, query="is")
        assert found.returned_count() == 0

    async def test_word_boundaries_are_respected(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """Substring matching returned "postgres" for a query of "gres".

        Full-text indexes lexemes, so a fragment is not a match - which removes a
        whole class of nonsense results.
        """
        await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="We use postgres."
        )
        assert (await search(db_session, project.id, query="gres")).returned_count() == 0
        assert (await search(db_session, project.id, query="postgres")).returned_count() == 1

    async def test_multi_word_queries_require_all_terms(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="Redis caches sessions."
        )
        await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="Postgres stores tasks."
        )
        found = await search(db_session, project.id, query="redis sessions")
        assert [m.content for m in found.memories] == ["Redis caches sessions."]

    async def test_negation_is_supported(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """websearch_to_tsquery understands the syntax a model actually writes."""
        await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="Postgres for queueing."
        )
        await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="Redis for queueing."
        )
        found = await search(db_session, project.id, query="queueing -redis")
        assert [m.content for m in found.memories] == ["Postgres for queueing."]

    async def test_malformed_query_syntax_does_not_raise(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """A model will eventually send something odd.

        to_tsquery would raise a syntax error and turn that into a tool failure.
        websearch_to_tsquery accepts whatever it is given, which is why it is the
        one used.
        """
        await remember(db_session, project.id, memory_type=MemoryType.FACT, content="Something.")
        for odd in ("&&&", '"unclosed', "()", "| or and", "!!!"):
            await search(db_session, project.id, query=odd)


class TestRanking:
    async def test_better_matches_rank_higher(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        await remember(
            db_session,
            project.id,
            memory_type=MemoryType.FACT,
            content="The task queue is PostgreSQL.",
            importance=50,
        )
        await remember(
            db_session,
            project.id,
            memory_type=MemoryType.FACT,
            content="Unrelated note that happens to mention a queue somewhere in passing.",
            importance=50,
        )
        found = await search(db_session, project.id, query="task queue")
        assert found.memories[0].content == "The task queue is PostgreSQL."

    async def test_importance_breaks_a_relevance_tie(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """Relevance answers "does this match". Importance answers "does this
        matter". Identical text, different weight."""
        await remember(
            db_session,
            project.id,
            memory_type=MemoryType.FACT,
            content="The queue is PostgreSQL, note A.",
            importance=10,
        )
        await remember(
            db_session,
            project.id,
            memory_type=MemoryType.FACT,
            content="The queue is PostgreSQL, note B.",
            importance=95,
        )
        found = await search(db_session, project.id, query="queue postgresql")
        assert found.memories[0].importance == 95

    async def test_a_stale_task_falls_behind_an_old_decision(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """The type-dependent half-life, which is why recency is not one global
        decay rate.

        A TASK from three weeks ago is almost certainly finished. A DECISION from
        a year ago may be the most important thing in the corpus. A single decay
        curve would have to be wrong for one of them.
        """
        old_decision = await remember(
            db_session,
            project.id,
            memory_type=MemoryType.DECISION,
            content="The queue is PostgreSQL.",
        )
        stale_task = await remember(
            db_session,
            project.id,
            memory_type=MemoryType.TASK,
            content="Currently changing the queue implementation.",
        )

        # Age both by the same amount using the database clock.
        for memory_id, age in ((old_decision, 365), (stale_task, 21)):
            await db_session.execute(
                update(Memory)
                .where(Memory.id == memory_id.memory.memory_id)
                .values(created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=age))
            )

        found = await search(db_session, project.id, query="queue")
        assert found.memories[0].type is MemoryType.DECISION, (
            "a three-week-old TASK outranked a year-old DECISION - the "
            "type-dependent half-life is not being applied"
        )

    async def test_results_are_deterministic(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """Identical calls must return an identical order, or nothing about the
        ranking can be snapshot-tested."""
        for i in range(20):
            await remember(
                db_session,
                project.id,
                memory_type=MemoryType.FACT,
                content=f"The queue handles case {i}.",
                importance=50,
            )
        first = await search(db_session, project.id, query="queue", limit=20)
        second = await search(db_session, project.id, query="queue", limit=20)
        assert [m.memory_id for m in first.memories] == [m.memory_id for m in second.memories]

    async def test_browsing_without_a_query_orders_by_importance(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """With no query there is nothing to be relevant to."""
        await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="Low.", importance=10
        )
        await remember(
            db_session, project.id, memory_type=MemoryType.FACT, content="High.", importance=90
        )
        found = await search(db_session, project.id)
        assert [m.content for m in found.memories] == ["High.", "Low."]


class TestFilteringStillHolds:
    async def test_ranking_cannot_resurrect_a_retired_memory(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """The guarantee that must survive every retrieval change.

        Full-text ranking sits entirely on top of the stage-0 filter, so a
        superseded memory is not merely ranked low - it is never a candidate.
        """
        old = await remember(
            db_session,
            project.id,
            memory_type=MemoryType.FACT,
            content="Redis is the task queue.",
            importance=100,
        )
        await remember(
            db_session,
            project.id,
            memory_type=MemoryType.DECISION,
            content="PostgreSQL is the task queue now.",
            supersedes=[old.memory.memory_id],
            importance=10,
        )
        # Even with the retired memory at maximum importance and the current one
        # at minimum, and querying the retired wording directly.
        for query in ("redis", "task queue", "redis queue"):
            found = await search(db_session, project.id, query=query, limit=100)
            assert "Redis is the task queue." not in [m.content for m in found.memories]

    async def test_full_text_respects_project_isolation(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        other = await use_project(db_session, slug="fts-other", create=True)
        await remember(
            db_session, other.id, memory_type=MemoryType.FACT, content="Queue lives elsewhere."
        )
        found = await search(db_session, project.id, query="queue")
        assert found.returned_count() == 0
