"""The stage-0 retrieval filter.

This module is the product. Everything after it - substring matching now,
full-text in Milestone 5, vectors in 7, fusion and budgeting in 8 - is relevance
tuning on top of a set that is already guaranteed correct.

Four conditions, and each one is load-bearing:

``project_id``
    Namespace isolation. Never optional; the function takes it positionally so
    there is no way to call it without one.

``status = 'ACTIVE'``
    Excludes SUPERSEDED and DELETED. This is the line that makes the
    stale-memory demo work: once "Redis is the queue" is retired, no ranking
    strategy can resurrect it, because no ranking strategy ever sees it.

``expires_at IS NULL OR expires_at > now()``
    Expiry evaluated at read time against the *database* clock. Not a stored
    status, so it cannot be stale; not the application clock, so two server
    processes cannot disagree about what has expired.

``is_current``
    The current revision only. Superseded revisions of a live memory stay in the
    append-only log for ``memory_history`` and are never retrieved.

There is exactly one way to see anything outside this set, and it is
``include_retired=True``, used only by history and debug paths. Grep for it: if
it appears in a normal retrieval path, that is a bug.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, func, or_, select

from memhub.domain.enums import MemoryStatus
from memhub.persistence.models import Memory, MemoryRevision


def current_revisions(
    project_id: uuid.UUID, *, include_retired: bool = False
) -> Select[tuple[Memory, MemoryRevision]]:
    """Select (memory, current revision) pairs visible to normal retrieval.

    ``include_retired`` drops the status, expiry and currency conditions but
    *never* the project condition. Isolation is not negotiable, even for debug
    paths.
    """
    stmt = select(Memory, MemoryRevision).join(
        MemoryRevision,
        (MemoryRevision.memory_id == Memory.id) & (MemoryRevision.project_id == Memory.project_id),
    )

    # Always applied, in every mode.
    stmt = stmt.where(Memory.project_id == project_id)

    if include_retired:
        return stmt

    return stmt.where(
        Memory.status == MemoryStatus.ACTIVE.value,
        or_(Memory.expires_at.is_(None), Memory.expires_at > func.now()),
        # NOTE: bare `is_current`, NOT `.is_(True)`.
        #
        # This looks like a style preference and is not. The full-text index is
        # partial - `... WHERE is_current` - and PostgreSQL will only use it if
        # it can prove the query's predicate implies the index's. It proves that
        # for a bare boolean, but *not* for `is_current IS TRUE`, because IS TRUE
        # is null-safe and therefore not the same expression.
        #
        # Written the other way, the planner silently falls back to a sequential
        # scan over every current revision. Verified: at 20k rows, `IS TRUE`
        # produced a Seq Scan and the bare column produced a Bitmap Index Scan.
        # tests/perf asserts on the plan for exactly this reason.
        MemoryRevision.is_current,
    )
