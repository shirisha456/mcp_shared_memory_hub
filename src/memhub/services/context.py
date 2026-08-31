"""Building a project brief within a token budget.

Ties retrieval to selection. Everything interesting is in
``memhub.context.builder``; this decides what to hand it and what to report back.

**Over-fetching is the decision worth noting.** The builder can only choose from
what it is given, and a budget of 2000 tokens might hold twenty short memories or
three long ones. Fetching exactly the requested count would let a memory that
happens to rank eleventh never be considered, even when the ten above it are
near-duplicates of each other. So retrieval pulls far wider than the budget could
possibly hold and lets selection do the work it exists for.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from memhub.context.builder import Candidate, Selection, select
from memhub.context.render import render_brief
from memhub.context.tokens import HeuristicEstimator, TokenEstimator
from memhub.domain.errors import ValidationFailedError
from memhub.domain.models import MemoryView
from memhub.embeddings.base import EmbeddingPort
from memhub.observability import metrics as m
from memhub.persistence.repositories.memories import MemoryRepository, to_view
from memhub.persistence.repositories.projects import ProjectRepository
from memhub.services import memories as memory_service
from memhub.services import retrieval as retrieval_service

_METRICS = m.get_metrics()

CANDIDATE_POOL = 60
"""How many memories retrieval offers the selector.

Comfortably more than any realistic budget can hold, so selection is choosing
rather than accepting whatever it was handed. Bounded because scoring and the
pairwise similarity in MMR both grow with the pool, and past a point the extra
candidates are ones no budget would ever reach.
"""

MIN_BUDGET = 100
MAX_BUDGET = 32_000


@dataclass(frozen=True, slots=True)
class ProjectContext:
    brief: str
    memories: tuple[MemoryView, ...]
    selection: Selection
    estimator: str
    semantic_coverage: float | None
    degraded: str | None


async def build_context(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    query: str | None = None,
    token_budget: int = 2000,
    embedder: EmbeddingPort | None = None,
    estimator: TokenEstimator | None = None,
) -> ProjectContext:
    """Assemble the most useful brief that fits in ``token_budget``.

    With a ``query`` the candidates are ranked against it; without one they are
    the project's most important memories, which is what a client wants at the
    start of a session before it knows what it will be asked.
    """
    if not MIN_BUDGET <= token_budget <= MAX_BUDGET:
        raise ValidationFailedError(
            f"token_budget must be between {MIN_BUDGET} and {MAX_BUDGET}, got "
            f"{token_budget}. Below the floor nothing useful fits; above the "
            "ceiling you are not budgeting.",
            token_budget=token_budget,
        )

    counter = estimator or HeuristicEstimator()
    project = await ProjectRepository(session).get_by_id(project_id)
    slug = project.slug if project else str(project_id)

    coverage: float | None = None
    degraded: str | None = None

    if query and embedder is not None:
        found = await retrieval_service.hybrid_search(
            session, project_id, query=query, embedder=embedder, limit=CANDIDATE_POOL
        )
        views = list(found.memories)
        coverage, degraded = found.semantic_coverage, found.degraded
    elif query:
        found = await memory_service.search(session, project_id, query=query, limit=CANDIDATE_POOL)
        views = list(found.memories)
    else:
        # No query: browse by importance. There is nothing to be relevant to, so
        # "what matters most in this project" is the only sensible ordering.
        rows = await MemoryRepository(session).search(project_id, limit=CANDIDATE_POOL)
        views = [to_view(memory, revision) for memory, revision in rows]

    candidates = [
        Candidate(
            memory=view,
            # Rank position is the score. Retrieval has already applied
            # relevance, recency and importance, and re-deriving a score here
            # would mean two ranking systems that can disagree.
            score=1.0 / (index + 1),
            tokens=counter.estimate(view.content),
        )
        for index, view in enumerate(views)
    ]

    selection = select(candidates, budget=token_budget, estimator=counter)

    _METRICS.observe(m.CONTEXT_TOKENS, selection.tokens_used)
    _METRICS.observe(m.CONTEXT_UTILISATION, selection.utilisation)
    for reason, count in selection.dropped.items():
        _METRICS.increment(m.CONTEXT_DROPPED, value=count, reason=reason)

    return ProjectContext(
        brief=render_brief(selection.selected, project_slug=slug),
        memories=tuple(c.memory for c in selection.selected),
        selection=selection,
        estimator=counter.name,
        semantic_coverage=coverage,
        degraded=degraded,
    )
