"""Per-type behavioural policy.

This table is the justification for the type system existing at all: a memory
type earns its place only by changing at least one of these values. Ranking
weights and context quotas join this table in Milestones 5 and 8 respectively;
only the fields Milestone 1 actually uses are declared, so nothing here is dead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from memhub.domain.enums import MemoryType


@dataclass(frozen=True, slots=True)
class TypePolicy:
    default_ttl: timedelta | None
    """Applied when the caller supplies no ``expires_at``."""

    max_ttl: timedelta | None
    """Ceiling on a caller-supplied ``expires_at``. ``None`` means unbounded."""

    base_importance: int
    """Default importance, 0-100, when the caller does not specify one."""


TYPE_POLICY: dict[MemoryType, TypePolicy] = {
    MemoryType.CONSTRAINT: TypePolicy(
        default_ttl=None,
        max_ttl=None,
        base_importance=80,
    ),
    MemoryType.DECISION: TypePolicy(
        default_ttl=None,
        max_ttl=None,
        base_importance=70,
    ),
    MemoryType.FACT: TypePolicy(
        default_ttl=None,
        max_ttl=None,
        base_importance=50,
    ),
    # TASK is the one type with a mandatory TTL, enforced by a CHECK constraint
    # as well as here. Without it, "currently implementing X" accumulates
    # forever and the corpus fills with work that finished months ago - the
    # first step towards becoming a bad issue tracker.
    MemoryType.TASK: TypePolicy(
        default_ttl=timedelta(days=7),
        max_ttl=timedelta(days=30),
        base_importance=40,
    ),
}

MAX_CONTENT_LENGTH = 8192
MAX_TAGS = 16
MAX_TAG_LENGTH = 32
MAX_SEARCH_LIMIT = 100
DEFAULT_SEARCH_LIMIT = 10
