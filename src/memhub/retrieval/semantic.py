"""Vector search.

Cosine distance over the HNSW index, restricted to one embedding model and to the
same stage-0 filter every other retrieval path uses. A retired memory is no more
reachable through a vector than through a keyword.

**The filtered-ANN problem, which is the interesting part.** An HNSW scan walks
the graph collecting ``ef_search`` nearest neighbours and *then* the planner
applies the ``WHERE`` clause. With a restrictive filter - one project out of
many, active only - a scan can return 40 neighbours of which 3 survive, and
silently under-return: the query looks like it worked and quietly missed most of
what it should have found.

pgvector 0.8 added iterative index scans for exactly this: the scan continues
until enough rows survive filtering. It is enabled per query rather than
globally, because it costs more when the filter is not selective.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, text
from sqlalchemy.ext.asyncio import AsyncSession

from memhub.persistence.models import Memory, MemoryEmbedding, MemoryRevision
from memhub.retrieval.filters import current_revisions

MAX_COSINE_DISTANCE = 0.35
"""Beyond this, a "nearest neighbour" is not a neighbour.

**The measured reason this exists.** Without a threshold, hybrid retrieval scored
nDCG 0.881 against full text's 0.803 - and precision *collapsed* from 0.691 to
0.113, while queries the corpus cannot answer went from correctly returning
nothing to always returning ten unrelated memories.

That is not a tuning detail, it is how approximate nearest neighbour search
works: it returns the k closest vectors whether or not anything is close. Asked
about Kubernetes over a corpus that never mentions it, the index cheerfully
returns the ten least-unrelated memories it has.

For a context budget that is actively harmful - an irrelevant result does not
merely add noise, it displaces something useful. The value was chosen by sweeping
it against the evaluation corpus rather than picked by taste; see
``docs/eval/results.md``.
"""

ITERATIVE_SCAN_MODE = "relaxed_order"
"""``relaxed_order`` rather than ``strict_order``.

Strict ordering guarantees results come back in exact distance order at
noticeably higher cost. We do not need it: the vector ranking is one input to a
rank-fusion step that re-orders everything anyway, so paying for exactness the
next stage discards would be wasted.
"""


def semantic_candidates(
    project_id: uuid.UUID,
    *,
    model: str,
) -> Select[tuple[Memory, MemoryRevision, float]]:
    """(memory, revision, distance) for the current revisions that have vectors.

    An inner join, not an outer one: a memory without a vector is not a semantic
    candidate at all. That is the correct behaviour during the window after a
    write and before the outbox catches up - it is simply absent from this
    retriever, contributes nothing to fusion, and is still findable by full text.
    ``semantic_coverage`` tells the caller how much of the corpus that is.
    """
    return (
        current_revisions(project_id)
        .join(
            MemoryEmbedding,
            (MemoryEmbedding.memory_id == MemoryRevision.memory_id)
            & (MemoryEmbedding.revision_no == MemoryRevision.revision_no)
            & (MemoryEmbedding.model == model),
        )
        .add_columns(MemoryEmbedding.embedding.cosine_distance(text(":query_vector")).label("d"))
    )


async def search_by_vector(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    query_vector: Sequence[float],
    model: str,
    limit: int,
    max_distance: float = MAX_COSINE_DISTANCE,
) -> list[tuple[uuid.UUID, float]]:
    """Nearest neighbours, as (memory_id, cosine distance) in ascending order.

    Returns ids and distances rather than rows because the only consumer is rank
    fusion, which needs an ordering and nothing else. Hydrating full rows here
    would fetch content for candidates that fusion may well discard.
    """
    await session.execute(text(f"SET LOCAL hnsw.iterative_scan = {ITERATIVE_SCAN_MODE}"))

    rows = await session.execute(
        text(
            """
            SELECT m.id,
                   e.embedding <=> CAST(:vec AS vector) AS distance
              FROM memories m
              JOIN memory_revisions r
                ON r.memory_id = m.id AND r.project_id = m.project_id
              JOIN memory_embeddings e
                ON e.memory_id = r.memory_id
               AND e.revision_no = r.revision_no
               AND e.model = :model
             WHERE m.project_id = :pid
               AND m.status = 'ACTIVE'
               AND (m.expires_at IS NULL OR m.expires_at > now())
               AND r.is_current
               AND (e.embedding <=> CAST(:vec AS vector)) <= :max_distance
             ORDER BY distance
             LIMIT :limit
            """
        ),
        {
            "vec": str(list(query_vector)),
            "model": model,
            "pid": project_id,
            "limit": limit,
            "max_distance": max_distance,
        },
    )
    return [(row.id, float(row.distance)) for row in rows.all()]
