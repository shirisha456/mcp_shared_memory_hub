"""ORM models.

The invariants from the architecture document (section 13) are expressed here as
constraints and indexes, not as application logic. Nine of the fourteen are
enforced by the schema, which means they survive a bug in the service layer.

The two that most deserve attention:

``uq_memory_revisions_memory_id`` (partial, ``WHERE is_current``)
    At most one current revision per logical memory. Even if the Milestone 2
    compare-and-set were written wrongly, a second current revision raises
    ``23505`` rather than corrupting the corpus.

``fk_memories_superseded_by_id_project_id_memories`` (composite)
    A memory may only be superseded by a memory in the *same project*.
    Cross-project contamination is not prevented by a ``WHERE`` clause someone
    might forget - it is unrepresentable.

Deliberately absent until their milestones: ``content_tsv`` and its GIN index
(Milestone 5), ``memory_dedup_keys`` (3), ``idempotency_keys`` (2),
``memory_attestations`` (3), ``memory_embeddings`` and ``embedding_jobs`` (7),
``audit_events`` (2).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from memhub.persistence.base import Base

_SLUG_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,62}[a-z0-9]$"
_MEMORY_TYPES = "'DECISION','CONSTRAINT','FACT','TASK'"
_MEMORY_STATUSES = "'ACTIVE','SUPERSEDED','DELETED'"
_AUTHOR_KINDS = "'agent','human_confirmed','import'"


class Project(Base):
    """A memory namespace.

    Identity is ``id``, a server-issued UUID, and nothing else. ``slug`` is a
    stable human-facing key; git remotes and workspace paths are resolution
    aliases held in :class:`ProjectAlias`.
    """

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    archived_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        UniqueConstraint("slug"),
        CheckConstraint(f"slug ~ '{_SLUG_PATTERN}'", name="slug_format"),
    )


class ProjectAlias(Base):
    """A resolution hint, never an identity.

    The unique index spans ``(kind, value_norm)`` **globally** rather than
    per-project. That is the point: an alias value can then never resolve to two
    projects, so ambiguous resolution is impossible by construction rather than
    by careful query writing.
    """

    __tablename__ = "project_aliases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    value_norm: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("kind", "value_norm"),
        CheckConstraint("kind IN ('git_remote','workspace_path')", name="kind_known"),
        Index("ix_project_aliases_project_id", "project_id"),
    )


class Memory(Base):
    """The logical fact: identity and lifecycle only, never content.

    Mutable, but only in its lifecycle columns. All content lives in the
    append-only :class:`MemoryRevision` log.
    """

    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ACTIVE'"))
    current_revision_no: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    importance: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("50"))
    expires_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    superseded_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    deleted_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # Required so child tables can carry a composite FK that pins the project.
        UniqueConstraint("id", "project_id"),
        # Namespace isolation as a schema fact: superseding across projects
        # cannot be expressed. Nullable superseded_by_id means MATCH SIMPLE
        # skips the check while the column is NULL, which is what we want.
        ForeignKeyConstraint(
            ["superseded_by_id", "project_id"],
            ["memories.id", "memories.project_id"],
        ),
        CheckConstraint(f"type IN ({_MEMORY_TYPES})", name="type_known"),
        CheckConstraint(f"status IN ({_MEMORY_STATUSES})", name="status_known"),
        CheckConstraint("current_revision_no >= 1", name="revision_positive"),
        CheckConstraint("importance BETWEEN 0 AND 100", name="importance_range"),
        CheckConstraint("superseded_by_id IS DISTINCT FROM id", name="no_self_supersede"),
        CheckConstraint(
            "(status = 'SUPERSEDED') = (superseded_at IS NOT NULL)",
            name="superseded_consistent",
        ),
        CheckConstraint(
            "status <> 'SUPERSEDED' OR superseded_by_id IS NOT NULL",
            name="superseded_has_target",
        ),
        CheckConstraint(
            "(status = 'DELETED') = (deleted_at IS NOT NULL)", name="deleted_consistent"
        ),
        # The one type with a mandatory expiry. Without this, "currently
        # implementing X" outlives the work it describes.
        CheckConstraint("type <> 'TASK' OR expires_at IS NOT NULL", name="task_needs_ttl"),
        Index(
            "ix_memories_project_id_type_importance",
            "project_id",
            "type",
            text("importance DESC"),
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index(
            "ix_memories_expires_at",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL AND status = 'ACTIVE'"),
        ),
        Index(
            "ix_memories_superseded_by_id",
            "superseded_by_id",
            postgresql_where=text("superseded_by_id IS NOT NULL"),
        ),
    )


class MemoryRevision(Base):
    """Immutable content. Append-only: never updated, never deleted.

    ``PRIMARY KEY (memory_id, revision_no)`` is doing real work - it makes two
    concurrent writers unable to both create revision N, independently of
    whatever the service layer believes.
    """

    __tablename__ = "memory_revisions"

    memory_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    revision_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Denormalised so the composite FK below can pin the project, and so
    # project-scoped scans need no join.
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    hash_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1")
    )
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False)
    change_reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    author_client: Mapped[str] = mapped_column(Text, nullable=False)
    author_kind: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    # Generated by PostgreSQL, not by the application. A trigger or an
    # application-side write could drift from the content it indexes - a
    # generated column cannot, because there is no code path that writes
    # content without also producing the vector.
    content_tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', content)", persisted=True),
        nullable=False,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["memory_id", "project_id"],
            ["memories.id", "memories.project_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("revision_no >= 1", name="revision_positive"),
        CheckConstraint("length(content) BETWEEN 1 AND 8192", name="content_length"),
        CheckConstraint("cardinality(tags) <= 16", name="tags_bounded"),
        CheckConstraint(f"author_kind IN ({_AUTHOR_KINDS})", name="author_kind_known"),
        # INVARIANT 1: exactly one current revision per logical memory.
        Index(
            "uq_memory_revisions_memory_id",
            "memory_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index("ix_memory_revisions_project_id", "project_id"),
        # Partial on is_current. At steady state most rows are superseded
        # revisions that no search will ever read, so indexing them would
        # inflate the index and slow every insert for no benefit.
        Index(
            "ix_memory_revisions_content_tsv",
            "content_tsv",
            postgresql_using="gin",
            postgresql_where=text("is_current"),
        ),
        Index(
            "ix_memory_revisions_tags",
            "tags",
            postgresql_using="gin",
            postgresql_where=text("is_current"),
        ),
    )


class IdempotencyKey(Base):
    """Makes a retried write safe.

    The protocol is in ``memhub.services.idempotency``; what matters here is that
    the primary key is the whole mechanism. "Check whether the key exists, then
    insert" races - two retries can both pass the check. ``INSERT ... ON CONFLICT
    DO NOTHING`` against this key is a single atomic statement, so exactly one
    caller can ever claim it.

    ``request_fingerprint`` guards the other half: a key reused with a *different*
    payload must fail loudly rather than silently return the earlier result,
    which would answer a question the caller never asked.
    """

    __tablename__ = "idempotency_keys"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    client_request_id: Mapped[str] = mapped_column(Text, primary_key=True)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    request_fingerprint: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    expires_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now() + interval '7 days'"),
    )

    __table_args__ = (
        CheckConstraint("state IN ('IN_PROGRESS','COMPLETED')", name="state_known"),
        CheckConstraint("length(client_request_id) BETWEEN 8 AND 128", name="request_id_length"),
        # A completed claim without a stored response cannot be replayed, which
        # would silently turn a retry into a second write.
        CheckConstraint(
            "state <> 'COMPLETED' OR response IS NOT NULL", name="completed_has_response"
        ),
        # Supports the bounded GC sweep. Unbounded DELETE on a busy table is its
        # own outage, so the sweep is always LIMITed and needs this index.
        Index("ix_idempotency_keys_expires_at", "expires_at"),
    )


class AuditEvent(Base):
    """A durable record of what happened to a memory, and who did it.

    Distinct from the application log, and deliberately so. Application logs are
    operational and rotate away; this is part of the product - it is what
    ``memory_history`` will use to answer "who created this and what happened to
    it". Conflating the two means the audit trail disappears with log retention.

    Never contains memory content. Only identifiers, outcomes and sizes, so that
    the log can be read freely without exposing what the memories say.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    memory_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    revision_no: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    actor_client: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        CheckConstraint(
            "action IN ('remember','revise','forget','supersede','purge')",
            name="action_known",
        ),
        CheckConstraint(
            "outcome IN ('ok','conflict','dedup','replay','rejected')", name="outcome_known"
        ),
        # No foreign key to memories, on purpose: an audit row must survive the
        # thing it describes. An operator purge destroys a memory and every
        # revision of it, and the record that the purge happened has to outlive
        # that. A CASCADE here would erase the evidence along with the subject.
        Index("ix_audit_events_memory_id_at", "memory_id", text("at DESC")),
        Index("ix_audit_events_project_id_at", "project_id", text("at DESC")),
    )


class MemoryDedupKey(Base):
    """One row per distinct active fact in a project.

    **Why a separate table rather than a partial unique index on revisions.**
    The rule we want is "no two *active* memories in a project have identical
    normalised content". ``is_current`` lives on the revision but ``status``
    lives on the memory, and a unique index cannot span two tables. A dedicated
    table whose rows we insert on create and delete on forget or supersede gives
    a real unique constraint over exactly the right set - and makes deduplication
    a single race-free ``INSERT ... ON CONFLICT DO NOTHING`` rather than a
    check-then-insert.

    The lifecycle matters as much as the constraint: retiring a memory releases
    its key, so the same sentence can legitimately be asserted again later.

    ``hash_version`` is in the primary key so a change to the normaliser can be
    rolled out alongside the old one instead of as a big-bang re-hash.
    """

    __tablename__ = "memory_dedup_keys"

    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    hash_version: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    content_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    memory_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["memory_id", "project_id"],
            ["memories.id", "memories.project_id"],
            ondelete="CASCADE",
        ),
        Index("ix_memory_dedup_keys_memory_id", "memory_id"),
    )


class MemoryAttestation(Base):
    """Which clients have independently asserted this fact.

    A deduplicated write is evidence, not a nuisance. When Cursor states
    something Claude Desktop already stored, that second, independent assertion
    is a signal the fact is load-bearing - so instead of discarding the event we
    record it, and from Milestone 7 the corroboration count becomes a small
    ranking prior.

    Keyed on client name rather than call count so that one client retrying in a
    loop cannot manufacture corroboration; ``times_seen`` tracks repetition
    separately from ``COUNT(DISTINCT client_name)``.
    """

    __tablename__ = "memory_attestations"

    memory_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    client_name: Mapped[str] = mapped_column(Text, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    first_seen_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    times_seen: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["memory_id", "project_id"],
            ["memories.id", "memories.project_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("times_seen >= 1", name="times_seen_positive"),
    )
