"""Tool output schemas.

These are Pydantic models because the MCP SDK derives each tool's
``outputSchema`` from its return annotation and emits ``structuredContent``
automatically. That matters more than convenience: a declared output schema is
what lets a client validate our responses, and what makes the golden-manifest
test in ``tests/protocol`` able to catch an accidental API change.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field

from memhub.domain.models import (
    MemoryView,
    ProjectRef,
    RememberResult,
    ReviseReplayed,
    ReviseResult,
    ReviseSucceeded,
    SearchResult,
)


class ProjectOut(BaseModel):
    project_id: str
    slug: str
    display_name: str
    created: bool = Field(
        description="True only if this call created the project. Resolution never creates."
    )

    @classmethod
    def of(cls, ref: ProjectRef) -> ProjectOut:
        return cls(
            project_id=str(ref.id),
            slug=ref.slug,
            display_name=ref.display_name,
            created=ref.created,
        )


class MemoryOut(BaseModel):
    memory_id: str
    project_id: str
    type: str
    status: str
    revision_no: int
    content: str
    tags: list[str]
    importance: int
    expires_at: dt.datetime | None
    author_client: str
    author_kind: str
    source: str | None
    created_at: dt.datetime
    updated_at: dt.datetime

    @classmethod
    def of(cls, view: MemoryView) -> MemoryOut:
        return cls(
            memory_id=str(view.memory_id),
            project_id=str(view.project_id),
            type=view.type.value,
            status=view.status.value,
            revision_no=view.revision_no,
            content=view.content,
            tags=list(view.tags),
            importance=view.importance,
            expires_at=view.expires_at,
            author_client=view.author_client,
            author_kind=view.author_kind.value,
            source=view.source,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class RememberOut(BaseModel):
    outcome: Literal["created", "idempotent_replay"] = Field(
        description=(
            "How the write resolved. 'created': a new memory was stored. "
            "'idempotent_replay': this exact request had already been applied, so "
            "nothing new was written and the original memory is returned. Branch "
            "on this rather than assuming a memory was newly created."
        )
    )
    memory: MemoryOut

    @classmethod
    def of(cls, result: RememberResult) -> RememberOut:
        return cls(outcome=result.outcome, memory=MemoryOut.of(result.memory))


class SearchOut(BaseModel):
    results: list[MemoryOut]
    returned: int
    total_matched: int = Field(
        description=(
            "Matches passing the filter before 'limit' was applied. Lets a caller "
            "tell 'there are only 3' apart from 'there are 300, here are 10'."
        )
    )
    filtered_out: str = Field(
        default=("superseded, deleted and expired memories are excluded from normal retrieval"),
        description="States what normal retrieval never returns.",
    )

    @classmethod
    def of(cls, result: SearchResult) -> SearchOut:
        return cls(
            results=[MemoryOut.of(view) for view in result.memories],
            returned=len(result.memories),
            total_matched=result.total_considered,
        )


class ReviseOut(BaseModel):
    """One flat shape for all three revise outcomes.

    The domain models these as a union, which forces internal callers to handle
    the conflict branch. Here it is flattened, because a JSON-Schema ``anyOf`` is
    harder for a model to consume than a single object with an ``outcome`` field
    it can branch on.

    On a conflict, ``memory`` holds the revision that *won*, not the one the
    caller tried to write - so a single round trip carries everything needed to
    merge and retry.
    """

    outcome: Literal["revised", "conflict", "idempotent_replay"] = Field(
        description=(
            "'revised': your change was applied. 'conflict': another client "
            "changed this memory first - 'memory' below is the current state and "
            "'current_revision' is the number to retry with. 'idempotent_replay': "
            "this request had already been applied; nothing new was written."
        )
    )
    memory: MemoryOut
    previous_revision: int | None = Field(
        default=None, description="On success, the revision this replaced."
    )
    current_revision: int | None = Field(
        default=None,
        description="On conflict, the revision that won. Retry with this value.",
    )
    expected_revision: int | None = Field(
        default=None, description="On conflict, the revision you sent."
    )
    guidance: str | None = Field(default=None, description="On conflict, what to do next.")

    @classmethod
    def of(cls, result: ReviseResult) -> ReviseOut:
        if isinstance(result, ReviseSucceeded):
            return cls(
                outcome="revised",
                memory=MemoryOut.of(result.memory),
                previous_revision=result.previous_revision,
            )
        if isinstance(result, ReviseReplayed):
            return cls(outcome="idempotent_replay", memory=MemoryOut.of(result.memory))
        return cls(
            outcome="conflict",
            memory=MemoryOut.of(result.current),
            current_revision=result.current.revision_no,
            expected_revision=result.expected_revision,
            guidance=(
                f"Another client changed this memory to revision "
                f"{result.current.revision_no} (by "
                f"{result.current.author_client}) while you held revision "
                f"{result.expected_revision}. Read the content above, merge your "
                f"change into it, and call memory_revise again with "
                f"expected_revision={result.current.revision_no}. Do not simply "
                "resend your original text: that would discard their change."
            ),
        )
