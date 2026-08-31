"""The outbox worker.

Claims pending embedding jobs, generates vectors, and stores them. The claim is
``SELECT ... FOR UPDATE SKIP LOCKED``, which is what lets workers in *both*
server processes drain the same queue without contending and without ever
processing the same job twice.

There is a pleasing self-reference worth stating plainly: this project uses
PostgreSQL as a durable job queue, which is exactly the architectural decision
used as the running example throughout its own documentation, and exactly why
Redis is not a dependency.

**Failure is expected and bounded.** An embedder that is down must never stop a
memory being written, so a failed job is retried with exponential backoff and,
after enough attempts, marked DEAD and left alone. A dead job is visible in the
table and countable as a metric - never a silent hole in the index. Search still
works throughout: a memory with no vector simply does not appear in the semantic
candidate list, and ``semantic_coverage`` reports how much of the corpus that is.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memhub.embeddings.base import EmbeddingError, EmbeddingPort
from memhub.observability import metrics as m
from memhub.persistence.engine import session_scope

logger = logging.getLogger(__name__)
_METRICS = m.get_metrics()

MAX_ATTEMPTS = 5
"""After this many failures a job is DEAD.

Bounded because an embedder that has failed five times is not going to succeed on
the sixth, and a job retrying forever is an infinite log of the same error that
hides everything else.
"""

BASE_BACKOFF = dt.timedelta(seconds=2)


CLAIM_JOBS = text(
    """
    SELECT j.id, j.memory_id, j.revision_no, j.project_id, j.attempts, r.content
      FROM embedding_jobs j
      JOIN memory_revisions r
        ON r.memory_id = j.memory_id AND r.revision_no = j.revision_no
     WHERE j.state = 'PENDING'
       AND j.model = :model
       AND j.next_attempt_at <= now()
     ORDER BY j.next_attempt_at
       FOR UPDATE OF j SKIP LOCKED
     LIMIT :batch_size
    """
)
"""Claim a batch.

``SKIP LOCKED`` is the whole mechanism: a worker steps over rows another
transaction already holds instead of blocking on them, so N workers process N
disjoint batches with no coordination beyond the database.

``FOR UPDATE OF j`` locks only the job row. Locking the joined revision too would
make embedding contend with revision writes for no reason.
"""


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    claimed: int
    embedded: int
    failed: int
    dead: int

    @property
    def idle(self) -> bool:
        return self.claimed == 0


class EmbeddingWorker:
    """Drains the outbox for one model."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: EmbeddingPort,
        *,
        batch_size: int = 16,
    ) -> None:
        self._sessions = session_factory
        self._embedder = embedder
        self._batch_size = batch_size

    async def run_once(self) -> BatchOutcome:
        """Claim and process one batch.

        The whole batch is one transaction: claim, embed, store, mark done. If
        anything raises, the transaction rolls back and the jobs return to
        PENDING - so a crash mid-batch loses no work and leaves no vector
        half-written.
        """
        async with session_scope(self._sessions) as session:
            rows = (
                await session.execute(
                    CLAIM_JOBS,
                    {"model": self._embedder.model_name, "batch_size": self._batch_size},
                )
            ).all()

            if not rows:
                return BatchOutcome(0, 0, 0, 0)

            try:
                vectors = await asyncio.to_thread(
                    self._embedder.embed, [row.content for row in rows]
                )
            except EmbeddingError as exc:
                return await self._defer(session, rows, str(exc))

            for row, vector in zip(rows, vectors, strict=True):
                await session.execute(
                    text(
                        "INSERT INTO memory_embeddings "
                        "(memory_id, revision_no, model, project_id, dim, embedding) "
                        "VALUES (:mid, :rev, :model, :pid, :dim, :vec) "
                        "ON CONFLICT (memory_id, revision_no, model) DO NOTHING"
                    ),
                    {
                        "mid": row.memory_id,
                        "rev": row.revision_no,
                        "model": self._embedder.model_name,
                        "pid": row.project_id,
                        "dim": self._embedder.dimension,
                        "vec": str(vector),
                    },
                )

            await session.execute(
                text("UPDATE embedding_jobs SET state = 'DONE' WHERE id = ANY(:ids)"),
                {"ids": [row.id for row in rows]},
            )

            _METRICS.increment(m.EMBEDDINGS, value=len(rows), outcome="ok")
            return BatchOutcome(claimed=len(rows), embedded=len(rows), failed=0, dead=0)

    async def _defer(
        self, session: AsyncSession, rows: Sequence[Row[Any]], reason: str
    ) -> BatchOutcome:
        """Back off, or give up after enough attempts.

        Exponential with jitter would be better under many workers; with the two
        this design has, plain exponential is enough and simpler to reason about.
        """
        dead = 0
        for row in rows:
            attempts = row.attempts + 1
            if attempts >= MAX_ATTEMPTS:
                await session.execute(
                    text(
                        "UPDATE embedding_jobs SET state='DEAD', attempts=:a, "
                        "last_error=:e WHERE id=:id"
                    ),
                    {"a": attempts, "e": reason[:2000], "id": row.id},
                )
                dead += 1
            else:
                delay = BASE_BACKOFF * (2 ** (attempts - 1))
                await session.execute(
                    text(
                        "UPDATE embedding_jobs SET attempts=:a, last_error=:e, "
                        "next_attempt_at = now() + :delay WHERE id=:id"
                    ),
                    {
                        "a": attempts,
                        "e": reason[:2000],
                        "delay": delay,
                        "id": row.id,
                    },
                )

        logger.warning(
            "embedding batch failed",
            extra={"count": len(rows), "dead": dead, "reason": reason[:200]},
        )
        _METRICS.increment(m.EMBEDDINGS, value=len(rows), outcome="failed")
        if dead:
            _METRICS.increment(m.EMBEDDING_FAILURES, value=dead)
        return BatchOutcome(claimed=len(rows), embedded=0, failed=len(rows), dead=dead)

    async def drain(self, *, max_batches: int = 1000) -> BatchOutcome:
        """Process until the queue is empty or the batch budget is spent.

        Bounded so a caller cannot accidentally block forever on a queue that is
        being refilled as fast as it drains.
        """
        totals = [0, 0, 0, 0]
        for _ in range(max_batches):
            outcome = await self.run_once()
            if outcome.idle:
                break
            totals[0] += outcome.claimed
            totals[1] += outcome.embedded
            totals[2] += outcome.failed
            totals[3] += outcome.dead
        return BatchOutcome(*totals)
