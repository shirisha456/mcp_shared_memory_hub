"""Memory write and read services.

Milestone 1 scope, stated so the boundaries are visible rather than implied:

*In* - create a memory at revision 1; retrieve by structured filters and
substring match, through the stage-0 filter.

*Out* - revision (Milestone 2), idempotency (2), deduplication (3),
supersession (3), full-text ranking (5), semantic search (7), context budgeting
(8). ``content_hash`` is computed on write today because it is ``NOT NULL`` on
an append-only table and backfilling it later would need a data migration - but
nothing reads it yet.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from memhub.domain.enums import AuthorKind, MemoryType
from memhub.domain.models import MemoryView, RememberResult, SearchResult
from memhub.domain.normalize import HASH_VERSION, content_hash
from memhub.domain.validation import (
    resolve_expiry,
    validate_content,
    validate_importance,
    validate_limit,
    validate_tags,
)
from memhub.persistence.repositories.memories import MemoryRepository, to_view


async def remember(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    memory_type: MemoryType,
    content: str,
    tags: Sequence[str] | None = None,
    importance: int | None = None,
    expires_at: dt.datetime | None = None,
    source: str | None = None,
    author_client: str = "unknown",
    author_kind: AuthorKind = AuthorKind.AGENT,
    now: dt.datetime | None = None,
) -> RememberResult:
    """Record a new memory.

    ``now`` is injectable purely so TTL policy is testable without sleeping. It
    defaults to the current UTC time; note that the *stored* timestamps
    (``created_at``, ``updated_at``) still come from the database clock, so two
    server processes can never disagree about ordering.
    """
    reference_time = now or dt.datetime.now(dt.UTC)

    clean_content = validate_content(content)
    clean_tags = validate_tags(list(tags) if tags else None)
    clean_importance = validate_importance(importance, memory_type)
    expiry = resolve_expiry(expires_at, memory_type, now=reference_time)

    repo = MemoryRepository(session)
    memory, revision = await repo.create(
        project_id,
        memory_type=memory_type,
        content=clean_content,
        content_hash=content_hash(clean_content),
        hash_version=HASH_VERSION,
        tags=clean_tags,
        importance=clean_importance,
        expires_at=expiry,
        author_client=author_client,
        author_kind=author_kind,
        source=source,
    )
    return RememberResult(memory=to_view(memory, revision), outcome="created")


async def search(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    query: str | None = None,
    types: Sequence[MemoryType] | None = None,
    tags: Sequence[str] | None = None,
    limit: int | None = None,
) -> SearchResult:
    """Retrieve active memories.

    No relevance ranking. Results are ordered by importance, then recency, then
    id - a deterministic total order, so identical calls return byte-identical
    results. Ranking arrives in Milestone 5, *after* Milestone 6 builds the
    evaluation harness that can prove it is an improvement.
    """
    clean_limit = validate_limit(limit)
    clean_tags = validate_tags(list(tags) if tags else None)
    clean_query = query.strip() if query and query.strip() else None

    repo = MemoryRepository(session)
    rows = await repo.search(
        project_id, query=clean_query, types=types, tags=clean_tags, limit=clean_limit
    )
    total = await repo.count(project_id, query=clean_query, types=types, tags=clean_tags)

    return SearchResult(
        memories=tuple(to_view(memory, revision) for memory, revision in rows),
        total_considered=total,
    )


async def get_memory(
    session: AsyncSession, project_id: uuid.UUID, memory_id: uuid.UUID
) -> MemoryView | None:
    repo = MemoryRepository(session)
    row = await repo.get(project_id, memory_id)
    return to_view(row[0], row[1]) if row else None
