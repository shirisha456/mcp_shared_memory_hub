"""Memory persistence.

Every method takes ``project_id`` as its first parameter. Not as an optional
filter - as a required argument, so there is no signature in this class that can
express a cross-project read. Isolation is enforced three times over: here by
types, in the schema by composite foreign keys, and in the query by the stage-0
filter.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from memhub.domain.enums import AuthorKind, MemoryStatus, MemoryType
from memhub.domain.models import MemoryView
from memhub.persistence.models import Memory, MemoryRevision
from memhub.retrieval.filters import current_revisions


def to_view(memory: Memory, revision: MemoryRevision) -> MemoryView:
    """Map a (memory, revision) row pair to the domain type."""
    return MemoryView(
        memory_id=memory.id,
        project_id=memory.project_id,
        type=MemoryType(memory.type),
        status=MemoryStatus(memory.status),
        revision_no=revision.revision_no,
        content=revision.content,
        tags=tuple(revision.tags),
        importance=memory.importance,
        expires_at=memory.expires_at,
        author_client=revision.author_client,
        author_kind=AuthorKind(revision.author_kind),
        source=revision.source,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


class MemoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        project_id: uuid.UUID,
        *,
        memory_type: MemoryType,
        content: str,
        content_hash: bytes,
        hash_version: int,
        tags: Sequence[str],
        importance: int,
        expires_at: dt.datetime | None,
        author_client: str,
        author_kind: AuthorKind,
        source: str | None,
    ) -> tuple[Memory, MemoryRevision]:
        """Insert a logical memory and its first revision.

        Both rows are added inside the caller's transaction, so a memory can
        never exist without a current revision - the state that would make
        ``uq_memory_revisions_memory_id`` and the current-revision pointer
        disagree.
        """
        memory = Memory(
            project_id=project_id,
            type=memory_type.value,
            status=MemoryStatus.ACTIVE.value,
            current_revision_no=1,
            importance=importance,
            expires_at=expires_at,
        )
        self._session.add(memory)
        await self._session.flush()

        revision = MemoryRevision(
            memory_id=memory.id,
            project_id=project_id,
            revision_no=1,
            content=content,
            content_hash=content_hash,
            hash_version=hash_version,
            tags=list(tags),
            is_current=True,
            change_reason=None,
            source=source,
            author_client=author_client,
            author_kind=author_kind.value,
        )
        self._session.add(revision)
        await self._session.flush()
        await self._session.refresh(memory)
        await self._session.refresh(revision)
        return memory, revision

    async def get(
        self, project_id: uuid.UUID, memory_id: uuid.UUID, *, include_retired: bool = False
    ) -> tuple[Memory, MemoryRevision] | None:
        stmt = current_revisions(project_id, include_retired=include_retired).where(
            Memory.id == memory_id
        )
        if include_retired:
            stmt = stmt.where(MemoryRevision.is_current.is_(True))
        row = (await self._session.execute(stmt)).first()
        return (row[0], row[1]) if row else None

    def _apply_filters(
        self,
        stmt: Select[tuple[Memory, MemoryRevision]],
        *,
        query: str | None,
        types: Sequence[MemoryType] | None,
        tags: Sequence[str] | None,
    ) -> Select[tuple[Memory, MemoryRevision]]:
        if types:
            stmt = stmt.where(Memory.type.in_([t.value for t in types]))
        if tags:
            # Array containment: the memory must carry ALL requested tags.
            stmt = stmt.where(MemoryRevision.tags.contains(list(tags)))
        if query:
            # Milestone 1 is substring matching only, and deliberately so.
            # Full-text ranking arrives in Milestone 5, after the evaluation
            # harness exists to prove it is actually an improvement.
            stmt = stmt.where(MemoryRevision.content.ilike(f"%{query}%"))
        return stmt

    async def count(
        self,
        project_id: uuid.UUID,
        *,
        query: str | None = None,
        types: Sequence[MemoryType] | None = None,
        tags: Sequence[str] | None = None,
    ) -> int:
        base = self._apply_filters(
            current_revisions(project_id), query=query, types=types, tags=tags
        )
        stmt = select(func.count()).select_from(base.subquery())
        return int((await self._session.execute(stmt)).scalar_one())

    async def search(
        self,
        project_id: uuid.UUID,
        *,
        query: str | None = None,
        types: Sequence[MemoryType] | None = None,
        tags: Sequence[str] | None = None,
        limit: int = 10,
    ) -> list[tuple[Memory, MemoryRevision]]:
        stmt = self._apply_filters(
            current_revisions(project_id), query=query, types=types, tags=tags
        )
        # Deterministic total ordering. The trailing id is not decoration: without
        # it, rows tying on importance and timestamp come back in whatever order
        # the plan produced, which makes results unstable between identical calls
        # and impossible to snapshot-test.
        stmt = stmt.order_by(Memory.importance.desc(), Memory.created_at.desc(), Memory.id).limit(
            limit
        )
        return [(row[0], row[1]) for row in (await self._session.execute(stmt)).all()]
