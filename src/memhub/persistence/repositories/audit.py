"""Audit trail.

Never records memory content. Identifiers, outcomes and sizes only, so the audit
log can be read freely - by an operator, or by ``memory_history`` - without
exposing what the memories actually say. That restriction is also what lets the
log survive an operator purge: there is nothing in it to redact.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from memhub.persistence.models import AuditEvent


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        action: str,
        outcome: str,
        actor_client: str,
        project_id: uuid.UUID | None = None,
        memory_id: uuid.UUID | None = None,
        revision_no: int | None = None,
        request_id: str | None = None,
        **detail: Any,
    ) -> None:
        """Append one event, inside the caller's transaction.

        Sharing the transaction is the point: an audit row that commits
        separately from the thing it describes can disagree with it. A rolled
        back write must leave no trace of having succeeded.
        """
        self._session.add(
            AuditEvent(
                action=action,
                outcome=outcome,
                actor_client=actor_client,
                project_id=project_id,
                memory_id=memory_id,
                revision_no=revision_no,
                request_id=request_id,
                detail=detail,
            )
        )
        await self._session.flush()
