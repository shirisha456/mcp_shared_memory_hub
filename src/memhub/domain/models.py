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


RememberOutcome = Literal["created", "idempotent_replay"]
"""Milestone 3 adds "deduplicated".

A discriminator rather than an error, because domain outcomes are returned as
ordinary structured results - the model needs machine-readable data to branch
on, not a sentence to parse. ``idempotent_replay`` means "your retry did not
create anything, because your first attempt already landed"; the memory returned
is the one that first attempt created.
"""


@dataclass(frozen=True, slots=True)
class RememberResult:
    memory: MemoryView
    outcome: RememberOutcome


@dataclass(frozen=True, slots=True)
class ReviseSucceeded:
    """This caller won the compare-and-set."""

    memory: MemoryView
    previous_revision: int
    outcome: Literal["revised"] = "revised"


@dataclass(frozen=True, slots=True)
class ReviseReplayed:
    """This caller retried a request that had already been applied."""

    memory: MemoryView
    outcome: Literal["idempotent_replay"] = "idempotent_replay"


@dataclass(frozen=True, slots=True)
class ReviseConflicted:
    """Another writer got there first.

    Not an error. The request was well formed and the database evaluated it
    correctly; the answer is simply "no, and here is why". Everything needed to
    merge and retry in a single round trip is carried here: the current revision
    number, its content, and who changed it.
    """

    current: MemoryView
    expected_revision: int
    outcome: Literal["conflict"] = "conflict"


ReviseResult = ReviseSucceeded | ReviseReplayed | ReviseConflicted
"""A union rather than one nullable-everything struct.

Internally this means the type checker forces every caller to handle the
conflict branch - it is impossible to read ``.memory`` off a conflict by
accident. The MCP layer flattens it into a single output schema, because a
JSON-Schema ``anyOf`` is harder for a model to consume than one object with an
``outcome`` field.
"""


@dataclass(frozen=True, slots=True)
class SearchResult:
    memories: tuple[MemoryView, ...]
    total_considered: int
    """How many rows passed the stage-0 filter before ``limit`` was applied.

    Reported so a caller can tell "there are only 3 matches" apart from "there
    are 300 and you are seeing the first 10".
    """
