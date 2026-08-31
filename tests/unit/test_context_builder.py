"""Budgeted selection.

Pure, so every property can be checked against inputs constructed to isolate it.
The first group is the contract; the rest are the reasons the pipeline has the
shape it does.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from memhub.context.builder import (
    DUPLICATE_SIMILARITY,
    Candidate,
    select,
    similarity,
)
from memhub.context.tokens import HeuristicEstimator, usable_budget
from memhub.domain.enums import AuthorKind, MemoryStatus, MemoryType
from memhub.domain.models import MemoryView

ESTIMATOR = HeuristicEstimator()
NOW = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def memory(
    content: str,
    *,
    memory_type: MemoryType = MemoryType.FACT,
    importance: int = 50,
    seed: int = 0,
) -> MemoryView:
    return MemoryView(
        memory_id=uuid.UUID(int=seed),
        project_id=uuid.UUID(int=999),
        type=memory_type,
        status=MemoryStatus.ACTIVE,
        revision_no=1,
        content=content,
        tags=(),
        importance=importance,
        expires_at=None,
        author_client="claude-desktop",
        author_kind=AuthorKind.AGENT,
        source=None,
        created_at=NOW,
        updated_at=NOW,
    )


def candidate(
    content: str,
    *,
    score: float = 1.0,
    memory_type: MemoryType = MemoryType.FACT,
    seed: int = 0,
) -> Candidate:
    return Candidate(
        memory=memory(content, memory_type=memory_type, seed=seed),
        score=score,
        tokens=ESTIMATOR.estimate(content),
    )


class TestTheBudgetContract:
    """Never exceed. That is the guarantee everything else is subordinate to."""

    @pytest.mark.parametrize("budget", [100, 250, 500, 1000, 2000, 4000, 8000])
    def test_never_exceeds_the_budget(self, budget: int) -> None:
        pool = [
            candidate(f"Memory number {i} with some content to give it weight.", seed=i)
            for i in range(200)
        ]
        result = select(pool, budget=budget, estimator=ESTIMATOR)
        assert result.tokens_used <= budget

    @pytest.mark.parametrize("budget", [500, 1000, 2000, 4000])
    def test_stays_within_the_safety_margin_too(self, budget: int) -> None:
        """Not merely under the budget - under the margin-adjusted budget.

        The estimator is wrong by some amount on every call. The margin is what
        turns "usually fits" into "fits".
        """
        pool = [candidate(f"Content {i}. " * 5, seed=i) for i in range(100)]
        result = select(pool, budget=budget, estimator=ESTIMATOR)
        assert result.tokens_used <= usable_budget(budget)

    def test_a_memory_too_large_to_ever_fit_is_dropped_and_counted(self) -> None:
        """Silently omitting it would look identical to it not existing."""
        pool = [candidate("x" * 100_000, seed=1), candidate("Short and useful.", seed=2)]
        result = select(pool, budget=500, estimator=ESTIMATOR)

        assert result.dropped.get("too_large_alone") == 1
        assert [c.memory.content for c in result.selected] == ["Short and useful."]

    def test_an_empty_pool_is_not_an_error(self) -> None:
        result = select([], budget=2000, estimator=ESTIMATOR)
        assert result.selected == ()
        assert result.tokens_used == 0


class TestQuotas:
    def test_one_loud_type_cannot_crowd_out_the_others(self) -> None:
        """The reason quotas exist.

        Fifty facts and two constraints, with the facts ranked higher. Pure
        greedy relevance would spend the whole budget on facts and drop the
        constraints - and a brief that omits "never do X" to make room for
        trivia is worse than useless.
        """
        pool = [
            candidate(f"Fact number {i} about the system.", score=1.0, seed=i) for i in range(50)
        ]
        pool += [
            candidate(
                "Credentials must never be logged.",
                score=0.01,
                memory_type=MemoryType.CONSTRAINT,
                seed=100,
            ),
            candidate(
                "No second datastore without a capacity argument.",
                score=0.01,
                memory_type=MemoryType.CONSTRAINT,
                seed=101,
            ),
        ]
        result = select(pool, budget=1000, estimator=ESTIMATOR)
        kinds = {c.memory.type for c in result.selected}
        assert MemoryType.CONSTRAINT in kinds, (
            "constraints were crowded out by higher-scoring facts - the quota is not being applied"
        )

    def test_an_unused_share_is_redistributed(self) -> None:
        """A project with no open tasks should spend that 15% on decisions
        rather than leaving it on the floor.

        Note the deliberately varied wording. An earlier version of this test
        used "Decision {i} about the architecture", and every pair of those
        shares four of five content words - so the diversity pass correctly
        rejected them as restatements and the test failed for a reason that had
        nothing to do with redistribution.
        """
        topics = [
            "PostgreSQL was chosen over Redis for durable task queueing",
            "Alembic migrations must maintain exactly one head",
            "Timestamps originate from the database clock, never the application",
            "Connection pooling is bounded with a fail-fast acquire timeout",
            "Structured JSON logging goes to stderr, leaving stdout for protocol",
            "Compare-and-set at READ COMMITTED replaced optimistic retry loops",
            "The outbox pattern decouples enrichment from the write path",
            "Reciprocal rank fusion combines keyword and vector rankings",
            "Content hashing deduplicates independently asserted facts",
            "Partial indexes exclude retired revisions from the search index",
        ]
        pool = [
            candidate(text, memory_type=MemoryType.DECISION, seed=i)
            for i, text in enumerate(topics)
        ]

        # A budget small enough that the 40% decision quota genuinely binds: the
        # pool costs more than the quota allows, so without redistribution most
        # of it would be dropped while the constraint, fact and task shares sat
        # unspent.
        budget = 400
        spendable = usable_budget(budget)
        quota_alone = int(spendable * 0.40)
        pool_cost = sum(c.tokens for c in pool)
        assert quota_alone < pool_cost <= spendable, "the test budget must make the quota bind"

        result = select(pool, budget=budget, estimator=ESTIMATOR)
        assert result.tokens_used > quota_alone, (
            f"used {result.tokens_used} tokens, no more than the {quota_alone} the "
            "decision quota allows on its own - the unused shares were not released"
        )


class TestDiversity:
    def test_near_duplicates_are_rejected(self) -> None:
        """Three phrasings of one decision should not cost the budget three times."""
        pool = [
            candidate("PostgreSQL is the task queue for this project.", seed=1),
            candidate("PostgreSQL is the task queue for this project now.", seed=2),
            candidate("The task queue for this project is PostgreSQL.", seed=3),
            candidate("Python 3.12 or newer is required to build.", seed=4),
        ]
        result = select(pool, budget=2000, estimator=ESTIMATOR)

        assert result.dropped.get("too_similar", 0) >= 1
        contents = [c.memory.content for c in result.selected]
        assert "Python 3.12 or newer is required to build." in contents, (
            "the distinct memory should survive; only the restatements go"
        )

    def test_genuinely_different_memories_all_survive(self) -> None:
        """Diversity must not become deduplication of unrelated things."""
        pool = [
            candidate("PostgreSQL is the task queue.", seed=1),
            candidate("Python 3.12 or newer is required.", seed=2),
            candidate("Migrations must maintain a single head.", seed=3),
            candidate("Logs are JSON on stderr.", seed=4),
        ]
        result = select(pool, budget=4000, estimator=ESTIMATOR)
        assert len(result.selected) == 4
        assert "too_similar" not in result.dropped

    def test_the_similarity_threshold_is_where_it_claims(self) -> None:
        near = similarity(
            "PostgreSQL is the task queue for this project.",
            "The task queue for this project is PostgreSQL.",
        )
        far = similarity("PostgreSQL is the task queue.", "Python 3.12 or newer is required.")
        assert near >= DUPLICATE_SIMILARITY
        assert far < DUPLICATE_SIMILARITY


class TestValueDensity:
    def test_a_verbose_memory_must_earn_its_space(self) -> None:
        """Ranking by score alone lets one long memory eat the budget.

        Here a long item and four short ones have the same score. The short ones
        deliver four facts for the same cost, and that is what should be chosen.
        """
        pool = [candidate("Padding. " * 120, score=1.0, seed=1)]
        pool += [
            candidate(text, score=1.0, seed=10 + i)
            for i, text in enumerate(
                [
                    "Python 3.12 is the minimum supported runtime.",
                    "Migrations run through Alembic with one head.",
                    "Logs are emitted as JSON on stderr.",
                    "The connection pool holds ten connections.",
                ]
            )
        ]
        result = select(pool, budget=600, estimator=ESTIMATOR)
        assert len(result.selected) >= 3, (
            "the long memory consumed the budget that four short ones could have "
            "filled with four separate facts"
        )


class TestDeterminism:
    def test_identical_input_gives_identical_output(self) -> None:
        """Without this the brief cannot be snapshot-tested or safely cached."""
        pool = [candidate(f"Memory {i} with distinct content here.", seed=i) for i in range(30)]
        first = select(pool, budget=1500, estimator=ESTIMATOR)
        second = select(pool, budget=1500, estimator=ESTIMATOR)
        assert [c.memory.memory_id for c in first.selected] == [
            c.memory.memory_id for c in second.selected
        ]

    def test_input_order_does_not_change_the_result(self) -> None:
        """Retrieval order should not leak into selection through the tiebreak."""
        pool = [candidate(f"Memory {i} with distinct content here.", seed=i) for i in range(20)]
        forward = select(pool, budget=1500, estimator=ESTIMATOR)
        backward = select(list(reversed(pool)), budget=1500, estimator=ESTIMATOR)
        assert {c.memory.memory_id for c in forward.selected} == {
            c.memory.memory_id for c in backward.selected
        }

    def test_constraints_are_rendered_first(self) -> None:
        """A reader about to change something needs the bounds before the detail."""
        pool = [
            candidate("A plain fact.", memory_type=MemoryType.FACT, seed=1),
            candidate("Currently implementing X.", memory_type=MemoryType.TASK, seed=2),
            candidate("Never log credentials.", memory_type=MemoryType.CONSTRAINT, seed=3),
            candidate("We chose PostgreSQL.", memory_type=MemoryType.DECISION, seed=4),
        ]
        result = select(pool, budget=4000, estimator=ESTIMATOR)
        assert [c.memory.type for c in result.selected] == [
            MemoryType.CONSTRAINT,
            MemoryType.DECISION,
            MemoryType.FACT,
            MemoryType.TASK,
        ]
