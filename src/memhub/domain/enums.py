"""Domain enumerations.

Stored as ``text`` with ``CHECK`` constraints rather than PostgreSQL ``ENUM``
types: ``ALTER TYPE ... ADD VALUE`` is awkward under Alembic and value *removal*
is impossible, whereas a ``CHECK`` constraint is trivially alterable in a
migration. At this scale the storage difference is irrelevant.
"""

from __future__ import annotations

from enum import StrEnum


class MemoryType(StrEnum):
    """The four V1 memory types.

    A type exists only if it changes system behaviour - default TTL, ranking
    band, or context-budget quota. If it does not, it is a tag. That test is why
    ``OBSERVATION`` (a provenance distinction, see :class:`AuthorKind`),
    ``BUG``/``SOLUTION`` (tagged facts and tasks) and ``TEMPORARY_CONTEXT``
    (any type with an ``expires_at``) are not here.
    """

    DECISION = "DECISION"
    """A choice that was made, stated together with what it rules out."""

    CONSTRAINT = "CONSTRAINT"
    """A rule the project must not violate."""

    FACT = "FACT"
    """A durable statement about the project."""

    TASK = "TASK"
    """Short-lived working state: 'currently implementing X'.

    Deliberately narrow. It exists so a second client knows where you left off,
    and it carries no assignee, board, sub-task, estimate, or workflow state.
    Its distinct behaviour is exactly a mandatory short TTL plus (from later
    milestones) fast recency decay and the smallest context quota.
    """


class MemoryStatus(StrEnum):
    """Lifecycle of a logical memory.

    Note the absence of ``EXPIRED``. Expiry is derived at read time from
    ``expires_at <= now()``. A stored expiry status would need a sweeper to be
    true, and between expiry and sweep the column would lie.
    """

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    """Retired by a *different* memory. Set in Milestone 3."""
    DELETED = "DELETED"
    """Tombstoned. Content is retained; only an operator purge destroys it."""


class AuthorKind(StrEnum):
    """How a revision came to exist.

    This replaces an ``OBSERVATION`` memory type: 'the agent noticed this' versus
    'a human confirmed this' is a statement about provenance and trust, not about
    what kind of thing the memory is.
    """

    AGENT = "agent"
    HUMAN_CONFIRMED = "human_confirmed"
    IMPORT = "import"
