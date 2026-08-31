"""Deduplication, attestation, retirement and lineage.

Everything here exists to answer one question the rest of the system depends on:
**which facts are currently true?** Retirement is what makes the answer change,
the dedup key is what stops the same answer being stored twice, and the lineage
queries are what let a human ask why it changed.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from memhub.domain.models import AttestationView, AuditEntry, RevisionView, SupersessionLink
from memhub.persistence.models import (
    AuditEvent,
    Memory,
    MemoryAttestation,
    MemoryDedupKey,
    MemoryRevision,
)
from memhub.persistence.sql import CAS_FORGET, CAS_SUPERSEDE, CLAIM_DEDUP_KEY, load

MAX_LINEAGE_DEPTH = 64
"""Guard on the supersession walk.

The schema forbids self-supersession and the write path is a compare-and-set, so
a cycle should be impossible. A bounded walk means that if one ever appeared,
history would return a truncated answer instead of hanging the server.
"""


class TruthRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- deduplication ---------------------------------------------------
    async def claim_dedup_key(
        self,
        project_id: uuid.UUID,
        *,
        content_hash: bytes,
        hash_version: int,
        memory_id: uuid.UUID,
    ) -> uuid.UUID | None:
        """Claim the key for this content. ``None`` means someone else holds it."""
        claimed = (
            await self._session.execute(
                load(CLAIM_DEDUP_KEY),
                {
                    "project_id": project_id,
                    "hash_version": hash_version,
                    "content_hash": content_hash,
                    "memory_id": memory_id,
                },
            )
        ).scalar_one_or_none()
        return uuid.UUID(str(claimed)) if claimed else None

    async def dedup_key_holder(
        self, project_id: uuid.UUID, *, content_hash: bytes, hash_version: int
    ) -> uuid.UUID | None:
        stmt = select(MemoryDedupKey.memory_id).where(
            MemoryDedupKey.project_id == project_id,
            MemoryDedupKey.hash_version == hash_version,
            MemoryDedupKey.content_hash == content_hash,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def release_dedup_keys(self, project_id: uuid.UUID, memory_id: uuid.UUID) -> None:
        """Free a retired memory's content for re-assertion.

        Without this, retiring "Redis is the queue" would permanently block
        anyone from ever storing that sentence again - including legitimately,
        if the decision were later reversed.
        """
        await self._session.execute(
            delete(MemoryDedupKey).where(
                MemoryDedupKey.project_id == project_id,
                MemoryDedupKey.memory_id == memory_id,
            )
        )

    # -- attestation -----------------------------------------------------
    async def attest(self, project_id: uuid.UUID, memory_id: uuid.UUID, *, client_name: str) -> int:
        """Record that a client asserted this fact, and return the corroboration count.

        Upsert rather than insert-or-check: this runs on the deduplication path,
        where two clients may be asserting simultaneously.

        The returned count is ``COUNT(DISTINCT client_name)``, not the number of
        calls - so one client retrying in a loop cannot manufacture the
        appearance of independent corroboration.
        """
        stmt = pg_insert(MemoryAttestation).values(
            memory_id=memory_id, project_id=project_id, client_name=client_name
        )
        await self._session.execute(
            stmt.on_conflict_do_update(
                index_elements=["memory_id", "client_name"],
                set_={
                    "times_seen": MemoryAttestation.__table__.c.times_seen + 1,
                    "last_seen_at": func.now(),
                },
            )
        )
        await self._session.flush()
        return await self.attestation_count(memory_id)

    async def attestation_count(self, memory_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(MemoryAttestation.memory_id == memory_id)
        return int((await self._session.execute(stmt)).scalar_one())

    async def attestations(self, memory_id: uuid.UUID) -> list[AttestationView]:
        stmt = (
            select(MemoryAttestation)
            .where(MemoryAttestation.memory_id == memory_id)
            .order_by(MemoryAttestation.first_seen_at)
        )
        return [
            AttestationView(
                client_name=row.client_name,
                times_seen=row.times_seen,
                first_seen_at=row.first_seen_at,
                last_seen_at=row.last_seen_at,
            )
            for row in (await self._session.execute(stmt)).scalars()
        ]

    # -- retirement ------------------------------------------------------
    async def supersede(
        self, project_id: uuid.UUID, memory_id: uuid.UUID, *, winner_id: uuid.UUID
    ) -> bool:
        """Retire one memory in favour of another. False if it was already retired."""
        retired = (
            await self._session.execute(
                load(CAS_SUPERSEDE),
                {"memory_id": memory_id, "project_id": project_id, "winner_id": winner_id},
            )
        ).scalar_one_or_none()
        if retired is None:
            return False
        await self.release_dedup_keys(project_id, memory_id)
        return True

    async def forget(self, project_id: uuid.UUID, memory_id: uuid.UUID) -> bool:
        """Tombstone a memory. False if it was already retired."""
        removed = (
            await self._session.execute(
                load(CAS_FORGET), {"memory_id": memory_id, "project_id": project_id}
            )
        ).scalar_one_or_none()
        if removed is None:
            return False
        await self.release_dedup_keys(project_id, memory_id)
        return True

    async def exists(self, project_id: uuid.UUID, memory_id: uuid.UUID) -> bool:
        """Whether the memory exists in this project, in any status."""
        stmt = select(Memory.id).where(Memory.id == memory_id, Memory.project_id == project_id)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    # -- lineage ---------------------------------------------------------
    async def revisions(self, memory_id: uuid.UUID) -> list[RevisionView]:
        stmt = (
            select(MemoryRevision)
            .where(MemoryRevision.memory_id == memory_id)
            .order_by(MemoryRevision.revision_no)
        )
        from memhub.domain.enums import AuthorKind

        return [
            RevisionView(
                revision_no=row.revision_no,
                content=row.content,
                tags=tuple(row.tags),
                is_current=row.is_current,
                change_reason=row.change_reason,
                author_client=row.author_client,
                author_kind=AuthorKind(row.author_kind),
                created_at=row.created_at,
            )
            for row in (await self._session.execute(stmt)).scalars()
        ]

    async def superseded_by(
        self, project_id: uuid.UUID, memory_id: uuid.UUID
    ) -> SupersessionLink | None:
        """The memory that retired this one, if any."""
        stmt = (
            select(Memory.superseded_by_id, Memory.superseded_at, MemoryRevision.content)
            .select_from(Memory)
            .outerjoin(
                MemoryRevision,
                (MemoryRevision.memory_id == Memory.superseded_by_id) & MemoryRevision.is_current,
            )
            .where(Memory.id == memory_id, Memory.project_id == project_id)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None or row[0] is None:
            return None
        return SupersessionLink(memory_id=row[0], content=row[2] or "", at=row[1])

    async def supersedes(
        self, project_id: uuid.UUID, memory_id: uuid.UUID
    ) -> list[SupersessionLink]:
        """The memories this one retired."""
        stmt = (
            select(Memory.id, Memory.superseded_at, MemoryRevision.content)
            .select_from(Memory)
            .join(
                MemoryRevision,
                (MemoryRevision.memory_id == Memory.id) & MemoryRevision.is_current,
            )
            .where(Memory.superseded_by_id == memory_id, Memory.project_id == project_id)
            .order_by(Memory.superseded_at)
        )
        return [
            SupersessionLink(memory_id=row[0], content=row[2], at=row[1])
            for row in (await self._session.execute(stmt)).all()
        ]

    async def lineage(self, project_id: uuid.UUID, memory_id: uuid.UUID) -> list[uuid.UUID]:
        """Walk forward from a retired memory to the one that is current now.

        A recursive CTE with an explicit depth cap. The cap is not defensive
        paranoia about the data - the schema forbids self-supersession - it is so
        that a corrupted chain degrades into a truncated answer rather than a
        server that stops responding.
        """
        stmt = text(
            """
            WITH RECURSIVE chain(id, next_id, depth) AS (
                SELECT m.id, m.superseded_by_id, 0
                  FROM memories m
                 WHERE m.id = :memory_id AND m.project_id = :project_id
                UNION ALL
                SELECT m.id, m.superseded_by_id, chain.depth + 1
                  FROM memories m
                  JOIN chain ON m.id = chain.next_id
                 WHERE chain.depth < :max_depth
                   AND m.project_id = :project_id
            )
            SELECT id FROM chain ORDER BY depth
            """
        )
        rows = await self._session.execute(
            stmt,
            {
                "memory_id": memory_id,
                "project_id": project_id,
                "max_depth": MAX_LINEAGE_DEPTH,
            },
        )
        return [row[0] for row in rows.all()]

    async def audit_trail(self, memory_id: uuid.UUID, *, limit: int = 50) -> list[AuditEntry]:
        stmt = (
            select(AuditEvent)
            .where(AuditEvent.memory_id == memory_id)
            .order_by(AuditEvent.at.desc(), AuditEvent.id.desc())
            .limit(limit)
        )
        return [
            AuditEntry(
                at=row.at,
                action=row.action,
                outcome=row.outcome,
                actor_client=row.actor_client,
                revision_no=row.revision_no,
            )
            for row in (await self._session.execute(stmt)).scalars()
        ]
