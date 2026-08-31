"""Domain result types.

Frozen dataclasses, not ORM rows. Services return these so that callers - the
MCP layer, tests, a future HTTP layer - never hold a live SQLAlchemy identity
bound to a closed session, and cannot accidentally mutate persistent state by
assigning to an attribute.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Literal

from memhub.domain.enums import AuthorKind, MemoryStatus, MemoryType


@dataclass(frozen=True, slots=True)
class ProjectRef:
    """A resolved project namespace."""

    id: uuid.UUID
    slug: str
    display_name: str
    created: bool
    """True only when this call created the project. Resolution never creates."""


@dataclass(frozen=True, slots=True)
class MemoryView:
    """A logical memory at its current revision."""

    memory_id: uuid.UUID
    project_id: uuid.UUID
    type: MemoryType
    status: MemoryStatus
    revision_no: int
    content: str
    tags: tuple[str, ...]
    importance: int
    expires_at: dt.datetime | None
    author_client: str
    author_kind: AuthorKind
    source: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


RememberOutcome = Literal["created"]
"""Milestone 3 adds "deduplicated"; Milestone 2 adds "idempotent_replay".

Modelled as a discriminator from the start because domain outcomes are returned
as ordinary structured results, not as errors - the model needs machine-readable
data to branch on, not a sentence to parse.
"""


@dataclass(frozen=True, slots=True)
class RememberResult:
    memory: MemoryView
    outcome: RememberOutcome


@dataclass(frozen=True, slots=True)
class SearchResult:
    memories: tuple[MemoryView, ...]
    total_considered: int
    """How many rows passed the stage-0 filter before ``limit`` was applied.

    Reported so a caller can tell "there are only 3 matches" apart from "there
    are 300 and you are seeing the first 10".
    """
