"""Hybrid retrieval.

Runs the lexical and semantic retrievers over the same stage-0 filter, fuses
their rankings by position, and hydrates the winners.

Three things worth stating, because each is a decision rather than an
implementation detail.

**Both retrievers over-fetch.** Fusion can only rank what it is given, so each
side returns roughly three times the requested limit. Fetching exactly ``limit``
from each would mean a document ranked 11th lexically and 1st semantically never
reaches fusion at all - and that document is precisely the kind hybrid retrieval
exists to find.

**Semantic failure is not search failure.** If the embedder is unavailable, or
nothing has been embedded yet, the semantic ranking is empty and RRF degrades to
pure lexical ordering with no special case. The response says so rather than
pretending the search was complete.

**Suppression is upstream of all of it.** Both retrievers compose on
``current_revisions``, so no amount of vector similarity can surface a retired
memory. That is why widening the match in the previous milestone, and adding a
whole second retriever in this one, both left stale inclusion at exactly zero.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from memhub.domain.enums import MemoryType
from memhub.domain.models import MemoryView, SearchResult
from memhub.embeddings.base import EmbeddingError, EmbeddingPort
from memhub.observability import metrics as m
from memhub.persistence.repositories.memories import MemoryRepository, to_view
from memhub.persistence.repositories.truth import TruthRepository
from memhub.persistence.sqlstate import QUERY_CANCELED
from memhub.retrieval import fusion, semantic

_METRICS = m.get_metrics()

OVERFETCH = 3
"""How much wider than the requested limit each retriever reaches.

Three is a compromise: enough that a document ranked highly by one retriever and
middlingly by the other still reaches fusion, small enough that the cost stays
close to a single search.
"""


async def hybrid_search(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    query: str,
    embedder: EmbeddingPort,
    types: Sequence[MemoryType] | None = None,
    tags: Sequence[str] | None = None,
    limit: int = 10,
    max_distance: float = semantic.MAX_COSINE_DISTANCE,
) -> SearchResult:
    """Lexical and semantic retrieval, fused by rank."""
    repo = MemoryRepository(session)
    truth = TruthRepository(session)
    candidates = limit * OVERFETCH

    lexical_rows = await repo.search(
        project_id, query=query, types=types, tags=tags, limit=candidates
    )
    lexical_ranking = [memory.id for memory, _ in lexical_rows]

    semantic_ranking: list[uuid.UUID] = []
    degraded: str | None = None
    try:
        vector = await _embed_query(embedder, query)
        neighbours = await semantic.search_by_vector(
            session,
            project_id,
            query_vector=vector,
            model=embedder.model_name,
            limit=candidates,
            max_distance=max_distance,
        )
        semantic_ranking = [memory_id for memory_id, _ in neighbours]
    except EmbeddingError as exc:
        # The embedder being down must not take search down with it.
        degraded = f"lexical_only: {exc}"
        _METRICS.increment(m.EMBEDDING_FAILURES, value=1)
    except DBAPIError as exc:
        # The semantic leg is the expensive one - an HNSW probe over the whole
        # project - so it is the leg that hits ``statement_timeout`` first. When
        # it does, the lexical results are already in hand, and half an answer
        # beats an error.
        #
        # Nothing after this point issues another statement, which matters:
        # PostgreSQL has aborted the transaction, so any further query would
        # fail with 25P02. It holds because ``missing`` below is only ever
        # populated by semantic-only hits, and there are none when the semantic
        # leg produced nothing.
        if getattr(getattr(exc, "orig", None), "sqlstate", None) != QUERY_CANCELED:
            raise
        degraded = "lexical_only: semantic search exceeded the statement timeout"
        _METRICS.increment(m.EMBEDDING_FAILURES, value=1)

    fused = fusion.reciprocal_rank_fusion(
        {"lexical": lexical_ranking, "semantic": semantic_ranking}
    )[:limit]

    by_id = {memory.id: (memory, revision) for memory, revision in lexical_rows}
    missing = [result.memory_id for result in fused if result.memory_id not in by_id]
    for memory_id in missing:
        # Found only by the semantic retriever, so its row was never fetched.
        row = await repo.get(project_id, memory_id)
        if row is not None:
            by_id[memory_id] = row

    memories: list[MemoryView] = []
    for result in fused:
        if row := by_id.get(result.memory_id):
            memories.append(to_view(*row))

    coverage = await truth.semantic_coverage(project_id, model=embedder.model_name)
    _METRICS.increment(m.SEARCHES, strategy="hybrid")

    return SearchResult(
        memories=tuple(memories),
        total_considered=len(set(lexical_ranking) | set(semantic_ranking)),
        match_strategy="hybrid",
        semantic_coverage=coverage,
        degraded=degraded,
    )


async def _embed_query(embedder: EmbeddingPort, query: str) -> list[float]:
    """Embed the query text off the event loop.

    Inference is CPU-bound and synchronous. Running it inline would block every
    other request in this process for the duration, which on a stdio server means
    blocking the only client it has.
    """
    import asyncio

    vectors = await asyncio.to_thread(embedder.embed, [query])
    if not vectors:  # pragma: no cover - an adapter returning nothing for input
        raise EmbeddingError("embedder returned no vector for the query")
    return vectors[0]
