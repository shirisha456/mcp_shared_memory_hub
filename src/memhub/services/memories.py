"""Memory write and read services.

Milestone 2 scope:

*In* - create at revision 1; revise via compare-and-set; idempotent writes;
audit trail; structured filter and substring retrieval.

*Out* - deduplication and supersession (Milestone 3), full-text ranking (5),
semantic search (7), context budgeting (8). ``content_hash`` is computed on
every write but nothing reads it yet: the column is ``NOT NULL`` on an
append-only table, so backfilling it later would need a data migration.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from memhub.domain.enums import AuthorKind, MemoryType
from memhub.domain.errors import MemoryNotFoundError
from memhub.domain.models import (
    MemoryView,
    RememberResult,
    ReviseConflicted,
    ReviseReplayed,
    ReviseResult,
    ReviseSucceeded,
    SearchResult,
)
from memhub.domain.normalize import HASH_VERSION, content_hash
from memhub.domain.validation import (
    resolve_expiry,
    validate_content,
    validate_importance,
    validate_limit,
    validate_tags,
)
from memhub.observability import metrics as m
from memhub.persistence.repositories.audit import AuditRepository
from memhub.persistence.repositories.memories import MemoryRepository, to_view
from memhub.services import idempotency

_METRICS = m.get_metrics()


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
    client_request_id: str | None = None,
    request_id: str | None = None,
    now: dt.datetime | None = None,
) -> RememberResult:
    """Record a new memory.

    With a ``client_request_id``, a retry that arrives after the original
    committed replays the original result rather than creating a second memory.
    Without one, a retry creates a duplicate - which is why the tool description
    asks for a key.

    ``now`` is injectable so TTL policy is testable without sleeping. The
    *stored* timestamps still come from the database clock, so two server
    processes can never disagree about ordering.
    """
    reference_time = now or dt.datetime.now(dt.UTC)

    clean_content = validate_content(content)
    clean_tags = validate_tags(list(tags) if tags else None)
    clean_importance = validate_importance(importance, memory_type)
    expiry = resolve_expiry(expires_at, memory_type, now=reference_time)

    if client_request_id is not None:
        outcome = await idempotency.claim(
            session,
            project_id=project_id,
            client_request_id=client_request_id,
            operation="remember",
            request_fingerprint=idempotency.fingerprint(
                {
                    "type": memory_type.value,
                    "content": clean_content,
                    "tags": list(clean_tags),
                    "importance": clean_importance,
                }
            ),
        )
        if isinstance(outcome, idempotency.Replayed):
            replayed = await _replay_memory(session, project_id, outcome.response)
            _METRICS.increment(m.WRITES, type=memory_type.value, outcome="idempotent_replay")
            _METRICS.increment(m.IDEMPOTENT_REPLAYS, operation="remember")
            return RememberResult(memory=replayed, outcome="idempotent_replay")

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

    await AuditRepository(session).record(
        action="remember",
        outcome="ok",
        actor_client=author_client,
        project_id=project_id,
        memory_id=memory.id,
        revision_no=1,
        request_id=request_id,
        type=memory_type.value,
        content_length=len(clean_content),
    )

    if client_request_id is not None:
        await idempotency.complete(
            session,
            project_id=project_id,
            client_request_id=client_request_id,
            response={"memory_id": str(memory.id), "revision_no": 1},
        )

    _METRICS.increment(m.WRITES, type=memory_type.value, outcome="created")
    return RememberResult(memory=to_view(memory, revision), outcome="created")


async def revise(
    session: AsyncSession,
    project_id: uuid.UUID,
    memory_id: uuid.UUID,
    *,
    expected_revision: int,
    content: str,
    tags: Sequence[str] | None = None,
    change_reason: str | None = None,
    source: str | None = None,
    author_client: str = "unknown",
    author_kind: AuthorKind = AuthorKind.AGENT,
    client_request_id: str | None = None,
    request_id: str | None = None,
) -> ReviseResult:
    """Refine an existing memory, if nobody else changed it first.

    A conflict is **not an error**. The request was well formed and the database
    evaluated it correctly; the answer is "no, and here is the current state".
    The caller gets the current revision number, its content and who wrote it -
    everything needed to merge and retry in one round trip.

    Note the ordering: idempotency is claimed *before* the compare-and-set. A
    retry of an already-applied revise must replay, not conflict. Without that,
    a client whose connection dropped after a successful write would be told it
    lost a race it actually won.
    """
    clean_content = validate_content(content)
    clean_tags = validate_tags(list(tags) if tags else None)

    if client_request_id is not None:
        outcome = await idempotency.claim(
            session,
            project_id=project_id,
            client_request_id=client_request_id,
            operation="revise",
            request_fingerprint=idempotency.fingerprint(
                {
                    "memory_id": str(memory_id),
                    "expected_revision": expected_revision,
                    "content": clean_content,
                    "tags": list(clean_tags),
                }
            ),
        )
        if isinstance(outcome, idempotency.Replayed):
            replayed = await _replay_memory(session, project_id, outcome.response)
            _METRICS.increment(m.REVISIONS, outcome="idempotent_replay")
            _METRICS.increment(m.IDEMPOTENT_REPLAYS, operation="revise")
            return ReviseReplayed(memory=replayed)

    repo = MemoryRepository(session)
    existing = await repo.get(project_id, memory_id)
    if existing is None:
        raise MemoryNotFoundError(
            f"No active memory {memory_id} in this project.", memory_id=str(memory_id)
        )

    new_revision = await repo.compare_and_set(
        project_id,
        memory_id,
        expected_revision=expected_revision,
        content=clean_content,
        content_hash=content_hash(clean_content),
        hash_version=HASH_VERSION,
        tags=clean_tags,
        change_reason=change_reason,
        author_client=author_client,
        author_kind=author_kind,
        source=source,
    )

    audit = AuditRepository(session)

    if new_revision is None:
        # Re-read to report what actually won. This read happens after the failed
        # CAS, so it sees the committed state that beat us.
        current = await repo.get(project_id, memory_id)
        if current is None:  # pragma: no cover - retired between the two reads
            raise MemoryNotFoundError(
                f"No active memory {memory_id} in this project.", memory_id=str(memory_id)
            )
        await audit.record(
            action="revise",
            outcome="conflict",
            actor_client=author_client,
            project_id=project_id,
            memory_id=memory_id,
            revision_no=current[0].current_revision_no,
            request_id=request_id,
            expected_revision=expected_revision,
        )
        _METRICS.increment(m.REVISIONS, outcome="conflict")
        _METRICS.increment(m.CONFLICTS)
        return ReviseConflicted(
            current=to_view(current[0], current[1]), expected_revision=expected_revision
        )

    await audit.record(
        action="revise",
        outcome="ok",
        actor_client=author_client,
        project_id=project_id,
        memory_id=memory_id,
        revision_no=new_revision,
        request_id=request_id,
        previous_revision=expected_revision,
        content_length=len(clean_content),
    )

    if client_request_id is not None:
        await idempotency.complete(
            session,
            project_id=project_id,
            client_request_id=client_request_id,
            response={"memory_id": str(memory_id), "revision_no": new_revision},
        )

    updated = await repo.get(project_id, memory_id)
    if updated is None:  # pragma: no cover - we just wrote it
        raise MemoryNotFoundError(f"Memory {memory_id} vanished mid-transaction.")

    _METRICS.increment(m.REVISIONS, outcome="ok")
    return ReviseSucceeded(
        memory=to_view(updated[0], updated[1]), previous_revision=expected_revision
    )


async def _replay_memory(
    session: AsyncSession, project_id: uuid.UUID, response: dict[str, Any]
) -> MemoryView:
    """Rehydrate the memory a stored idempotent response points at."""
    memory_id = uuid.UUID(str(response["memory_id"]))
    view = await get_memory(session, project_id, memory_id)
    if view is None:  # pragma: no cover - would mean the memory was purged
        raise MemoryNotFoundError(
            f"Idempotent replay refers to memory {memory_id}, which no longer exists.",
            memory_id=str(memory_id),
        )
    return view


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
    id - a deterministic total order, so identical calls return identical
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
