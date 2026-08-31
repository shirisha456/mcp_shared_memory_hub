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
    ForgetResult,
    MemoryHistory,
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
    outcome: Literal["created", "deduplicated", "idempotent_replay"] = Field(
        description=(
            "How the write resolved. 'created': a new memory was stored. "
            "'deduplicated': another client had already recorded this exact fact, "
            "so the existing memory is returned and your assertion was recorded as "
            "corroboration. 'idempotent_replay': this exact request had already "
            "been applied. Branch on this rather than assuming a new memory."
        )
    )
    memory: MemoryOut
    superseded: list[str] = Field(
        default_factory=list, description="Memories this assertion retired."
    )
    not_superseded: list[str] = Field(
        default_factory=list,
        description=(
            "Requested for supersession but not retired - already retired, deleted, "
            "or not in this project. Check this rather than assuming all succeeded."
        ),
    )
    attestation_count: int = Field(
        default=1,
        description=(
            "Distinct clients that have asserted this fact. Above 1 means it was "
            "independently corroborated."
        ),
    )

    @classmethod
    def of(cls, result: RememberResult) -> RememberOut:
        return cls(
            outcome=result.outcome,
            memory=MemoryOut.of(result.memory),
            superseded=[str(m) for m in result.superseded],
            not_superseded=[str(m) for m in result.not_superseded],
            attestation_count=result.attestation_count,
        )


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


class ForgetOut(BaseModel):
    outcome: Literal["forgotten", "already_forgotten"] = Field(
        description=(
            "'forgotten': the memory is now excluded from retrieval. "
            "'already_forgotten': it had already been retired, so nothing changed."
        )
    )
    memory_id: str
    note: str = Field(
        default=(
            "Tombstoned, not destroyed. Every revision remains readable through "
            "memory_history; only an operator can permanently erase content."
        )
    )

    @classmethod
    def of(cls, result: ForgetResult) -> ForgetOut:
        return cls(outcome=result.outcome, memory_id=str(result.memory_id))


class RevisionOut(BaseModel):
    revision_no: int
    content: str
    tags: list[str]
    is_current: bool
    change_reason: str | None
    author_client: str
    author_kind: str
    created_at: dt.datetime


class AttestationOut(BaseModel):
    client_name: str
    times_seen: int
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime


class SupersessionOut(BaseModel):
    memory_id: str
    content: str
    at: dt.datetime | None


class AuditOut(BaseModel):
    at: dt.datetime
    action: str
    outcome: str
    actor_client: str
    revision_no: int | None


class HistoryOut(BaseModel):
    """The full record for one memory, including what retrieval hides.

    This is the counterweight to stale-memory suppression: a retired memory
    leaves retrieval but never leaves the record, so it is always possible to ask
    what a project used to believe and when that changed.
    """

    memory: MemoryOut
    status: str = Field(
        description=(
            "ACTIVE memories appear in search. SUPERSEDED and DELETED do not, but "
            "remain fully readable here."
        )
    )
    revisions: list[RevisionOut] = Field(
        description="Every revision, oldest first. Content is never overwritten."
    )
    superseded_by: SupersessionOut | None = Field(
        default=None,
        description="The memory that replaced this one. Present only if retired.",
    )
    supersedes: list[SupersessionOut] = Field(
        default_factory=list, description="Memories this one retired."
    )
    attestations: list[AttestationOut] = Field(
        default_factory=list,
        description=(
            "Clients that independently asserted this fact. More than one distinct "
            "client is corroboration."
        ),
    )
    audit: list[AuditOut] = Field(default_factory=list, description="Recent events, newest first.")

    @classmethod
    def of(cls, history: MemoryHistory) -> HistoryOut:
        return cls(
            memory=MemoryOut.of(history.memory),
            status=history.memory.status.value,
            revisions=[
                RevisionOut(
                    revision_no=r.revision_no,
                    content=r.content,
                    tags=list(r.tags),
                    is_current=r.is_current,
                    change_reason=r.change_reason,
                    author_client=r.author_client,
                    author_kind=r.author_kind.value,
                    created_at=r.created_at,
                )
                for r in history.revisions
            ],
            superseded_by=(
                SupersessionOut(
                    memory_id=str(history.superseded_by.memory_id),
                    content=history.superseded_by.content,
                    at=history.superseded_by.at,
                )
                if history.superseded_by
                else None
            ),
            supersedes=[
                SupersessionOut(memory_id=str(s.memory_id), content=s.content, at=s.at)
                for s in history.supersedes
            ],
            attestations=[
                AttestationOut(
                    client_name=a.client_name,
                    times_seen=a.times_seen,
                    first_seen_at=a.first_seen_at,
                    last_seen_at=a.last_seen_at,
                )
                for a in history.attestations
            ],
            audit=[
                AuditOut(
                    at=e.at,
                    action=e.action,
                    outcome=e.outcome,
                    actor_client=e.actor_client,
                    revision_no=e.revision_no,
                )
                for e in history.audit
            ],
        )
