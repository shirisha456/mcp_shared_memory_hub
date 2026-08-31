"""The context builder, end to end.

The unit tests cover selection against constructed inputs. These cover it against
a real corpus through the real retrieval path, and assert the two properties the
architecture document states as guarantees:

* the budget is never exceeded, at any budget
* a retired memory appears in no brief, at any budget

The second is the one worth having. Stale suppression has survived widening the
match, adding a second retriever, and now a selection layer that reorders
everything - and it survives for the same reason each time, which is that all of
them compose on the stage-0 filter rather than each applying their own.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from memhub.context.tokens import HeuristicEstimator, usable_budget
from memhub.domain.enums import MemoryType
from memhub.domain.errors import ValidationFailedError
from memhub.domain.models import ProjectRef
from memhub.services.context import MAX_BUDGET, MIN_BUDGET, build_context
from memhub.services.memories import remember
from memhub.services.projects import use_project

pytestmark = pytest.mark.integration

BUDGETS = [100, 200, 500, 1000, 2000, 4000, 8000]

CORPUS = [
    (MemoryType.CONSTRAINT, "Credentials must never be written to logs or memory storage."),
    (MemoryType.CONSTRAINT, "No second datastore without a written capacity argument."),
    (MemoryType.DECISION, "PostgreSQL is the task queue, claimed with FOR UPDATE SKIP LOCKED."),
    (MemoryType.DECISION, "Alembic migrations maintain a single head so upgrades are ordered."),
    (MemoryType.DECISION, "Compare-and-set at READ COMMITTED replaced optimistic retry loops."),
    (MemoryType.FACT, "Connection pooling is bounded at ten with a two second timeout."),
    (MemoryType.FACT, "Structured JSON logging is emitted on stderr."),
    (MemoryType.FACT, "Python 3.12 or newer is required."),
    (MemoryType.TASK, "Currently implementing worker heartbeat logic."),
]


@pytest.fixture
async def project(db_session: AsyncSession) -> ProjectRef:
    ref = await use_project(db_session, slug="context-demo", create=True)
    for memory_type, content in CORPUS:
        await remember(db_session, ref.id, memory_type=memory_type, content=content)
    return ref


class TestTheBudgetContract:
    @pytest.mark.parametrize("budget", BUDGETS)
    async def test_never_exceeds_the_budget(
        self, db_session: AsyncSession, project: ProjectRef, budget: int
    ) -> None:
        built = await build_context(db_session, project.id, token_budget=budget)
        assert built.selection.tokens_used <= usable_budget(budget)

    async def test_the_budget_report_explains_itself(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """A caller who asked for 2000 and got 400 needs to know why.

        "The project has little to say" and "thirty memories did not fit" call
        for opposite responses, and a bare token count cannot distinguish them.
        """
        # Tight enough that the corpus genuinely cannot fit. At 300 the whole
        # nine-memory corpus fits in 252 tokens and nothing is dropped, which
        # tests the reporting path not at all.
        built = await build_context(db_session, project.id, token_budget=MIN_BUDGET)
        selection = built.selection

        assert selection.considered >= len(CORPUS)
        assert len(selection.selected) < selection.considered
        assert selection.dropped, "memories were left out with no reason recorded"
        assert built.estimator.startswith("heuristic")

    async def test_a_generous_budget_fits_everything(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        built = await build_context(db_session, project.id, token_budget=8000)
        assert len(built.memories) == len(CORPUS)

    @pytest.mark.parametrize("budget", [MIN_BUDGET - 1, 0, -100, MAX_BUDGET + 1])
    async def test_absurd_budgets_are_refused(
        self, db_session: AsyncSession, project: ProjectRef, budget: int
    ) -> None:
        with pytest.raises(ValidationFailedError, match="token_budget"):
            await build_context(db_session, project.id, token_budget=budget)


class TestSuppressionAtEveryBudget:
    async def test_a_retired_memory_appears_in_no_brief(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """The flagship assertion, now through a third retrieval layer.

        The retired memory is given maximum importance and its replacement the
        minimum, so any ranking-based suppression would leak. It does not,
        because selection can only choose from candidates the stage-0 filter
        already excluded it from.
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
            content="PostgreSQL replaced Redis for queueing in V1.",
            supersedes=[old.memory.memory_id],
            importance=10,
        )

        for budget in BUDGETS:
            for query in (None, "queue", "redis", "what queue is used"):
                built = await build_context(
                    db_session, project.id, query=query, token_budget=budget
                )
                assert "Redis is the task queue." not in built.brief, (
                    f"the retired memory reached the brief at budget={budget}, query={query!r}"
                )
                assert all(m.content != "Redis is the task queue." for m in built.memories)


class TestTheBrief:
    async def test_constraints_come_first(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """A reader about to change something needs the bounds before the detail."""
        built = await build_context(db_session, project.id, token_budget=4000)
        brief = built.brief
        assert brief.index("Constraints") < brief.index("Decisions")

    async def test_an_empty_project_says_so(self, db_session: AsyncSession) -> None:
        """A blank response reads as a failure; a stated absence is a fact."""
        empty = await use_project(db_session, slug="context-empty", create=True)
        built = await build_context(db_session, empty.id, token_budget=2000)

        assert "No memories recorded" in built.brief
        assert built.memories == ()

    async def test_provenance_is_visible(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """'A user confirmed this' and 'an agent noticed it' are different claims."""
        await remember(
            db_session,
            project.id,
            memory_type=MemoryType.DECISION,
            content="The retention window is ninety days, agreed with the team.",
            author_client="cursor",
        )
        built = await build_context(db_session, project.id, token_budget=4000)
        assert "recorded by" in built.brief

    async def test_identical_calls_give_identical_briefs(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """Without this the brief cannot be cached or snapshot-tested."""
        first = await build_context(db_session, project.id, token_budget=1000)
        second = await build_context(db_session, project.id, token_budget=1000)
        assert first.brief == second.brief


class TestEstimatorContract:
    async def test_the_estimate_is_biased_towards_over_counting(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """The asymmetry the whole design rests on.

        Exceeding the budget corrupts the caller's context window; under-filling
        wastes a little of it. Those are not comparable failures, so the
        estimator is tuned to fail in the cheap direction - it must never report
        fewer tokens than a ~4 chars/token reading of the same text.
        """
        estimator = HeuristicEstimator()
        for _, content in CORPUS:
            generous = len(content) / 4.0
            assert estimator.estimate(content) > generous

    async def test_the_estimator_is_named_in_the_response(
        self, db_session: AsyncSession, project: ProjectRef
    ) -> None:
        """The count is an approximation, so the caller is told what produced it."""
        built = await build_context(db_session, project.id, token_budget=2000)
        assert "heuristic" in built.estimator
