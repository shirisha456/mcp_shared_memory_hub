"""Memory write and read services.

Milestone 3 scope:

*In* - create, revise via compare-and-set, idempotent writes, deduplication with
attestation, supersession, tombstoning, history, audit trail, and structured
retrieval behind the stage-0 filter.

*Out* - full-text ranking (Milestone 5), semantic search (7), context budgeting
(8).

The write path now resolves in one of three ways, and the caller is told which:
the content is new (``created``), another client already asserted it
(``deduplicated``), or this exact request already ran (``idempotent_replay``).
Those are three different things and conflating any two of them loses
information the caller needs.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from memhub.domain.enums import AuthorKind, MemoryType
from memhub.domain.errors import MemoryNotFoundError
from memhub.domain.models import (
    ForgetResult,
    MemoryHistory,
    MemoryView,
    RememberResult,
    ReviseConflicted,
    ReviseReplayed,
    ReviseResult,
    ReviseSucceeded,
    SearchResult,
)
from memhub.domain.normalize import HASH_VERSION, content_hash
from memhub.domain.validation import (
    resolve_expiry,
    validate_content,
    validate_importance,
    validate_limit,
    validate_tags,
)
from memhub.observability import metrics as m
from memhub.persistence.repositories.audit import AuditRepository
from memhub.persistence.repositories.memories import MemoryRepository, to_view
from memhub.persistence.repositories.truth import TruthRepository
from memhub.retrieval import lexical
from memhub.services import idempotency

_METRICS = m.get_metrics()


async def remember(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    memory_type: MemoryType,
    content: str,
    tags: Sequence[str] | None = None,
    importance: int | None = None,
    expires_at: dt.datetime | None = None,
    supersedes: Sequence[uuid.UUID] | None = None,
    source: str | None = None,
    author_client: str = "unknown",
    author_kind: AuthorKind = AuthorKind.AGENT,
    client_request_id: str | None = None,
    request_id: str | None = None,
    embedding_model: str | None = None,
    now: dt.datetime | None = None,
) -> RememberResult:
    """Record a new memory, optionally retiring the ones it replaces.

    **Supersession is folded in here rather than being its own operation**, and
    that is a correctness decision, not an API-size one. Retiring a fact and
    asserting its replacement are a single atomic act: doing them as two calls
    creates a window in which the old fact is gone and the new one does not yet
    exist, and a client reading during that window would see the project as
    having no opinion about something it has a firm opinion about.

    With a ``client_request_id``, a retry arriving after the original committed
    replays the original result rather than creating a second memory. Without
    one, a retry creates a duplicate - which is why the tool description asks
    for a key.

    ``now`` is injectable so TTL policy is testable without sleeping. The
    *stored* timestamps still come from the database clock, so two server
    processes can never disagree about ordering.
    """
    reference_time = now or dt.datetime.now(dt.UTC)

    clean_content = validate_content(content)
    clean_tags = validate_tags(list(tags) if tags else None)
    clean_importance = validate_importance(importance, memory_type)
    expiry = resolve_expiry(expires_at, memory_type, now=reference_time)

    if client_request_id is not None:
        outcome = await idempotency.claim(
            session,
            project_id=project_id,
            client_request_id=client_request_id,
            operation="remember",
            request_fingerprint=idempotency.fingerprint(
                {
                    "type": memory_type.value,
                    "content": clean_content,
                    "tags": list(clean_tags),
                    "importance": clean_importance,
                }
            ),
        )
        if isinstance(outcome, idempotency.Replayed):
            replayed = await _replay_memory(session, project_id, outcome.response)
            _METRICS.increment(m.WRITES, type=memory_type.value, outcome="idempotent_replay")
            _METRICS.increment(m.IDEMPOTENT_REPLAYS, operation="remember")
            return RememberResult(memory=replayed, outcome="idempotent_replay")

    repo = MemoryRepository(session)
    truth = TruthRepository(session)
    audit = AuditRepository(session)
    digest = content_hash(clean_content)

    # Deduplication, on a SAVEPOINT.
    #
    # The dedup key carries a foreign key to the memory, so the memory row has
    # to exist before the key can be claimed - which means the insert is
    # speculative. A savepoint is what makes that safe: if the key turns out to
    # be taken, only the speculative insert is undone. The enclosing transaction
    # is untouched, because it belongs to the caller and may already contain an
    # idempotency claim that must survive.
    #
    # Rolling back the whole session here instead would silently discard that
    # claim, and would break any caller managing its own transaction.
    savepoint = await session.begin_nested()

    memory, revision = await repo.create(
        project_id,
        memory_type=memory_type,
        content=clean_content,
        content_hash=digest,
        hash_version=HASH_VERSION,
        tags=clean_tags,
        importance=clean_importance,
        expires_at=expiry,
        author_client=author_client,
        author_kind=author_kind,
        source=source,
    )

    claimed = await truth.claim_dedup_key(
        project_id, content_hash=digest, hash_version=HASH_VERSION, memory_id=memory.id
    )

    if claimed is None:
        # Another client already asserted this fact. Do not create a second
        # memory - but do not throw the event away either: a second, independent
        # assertion is evidence the fact is load-bearing.
        await savepoint.rollback()

        holder = await truth.dedup_key_holder(
            project_id, content_hash=digest, hash_version=HASH_VERSION
        )
        if holder is None:  # pragma: no cover - the key was released concurrently
            raise MemoryNotFoundError("Deduplication key vanished mid-transaction.")

        return await _deduplicate(
            session,
            project_id,
            holder,
            memory_type=memory_type,
            author_client=author_client,
            request_id=request_id,
        )

    await savepoint.commit()

    superseded, not_superseded = await _apply_supersession(
        session,
        project_id,
        winner_id=memory.id,
        targets=supersedes or (),
        author_client=author_client,
        request_id=request_id,
    )

    if embedding_model is not None:
        # Same transaction as the revision. Either both exist or neither does -
        # which is the difference between an outbox and a dual write.
        await truth.enqueue_embedding(project_id, memory.id, revision_no=1, model=embedding_model)

    attestations = await truth.attest(project_id, memory.id, client_name=author_client)

    await audit.record(
        action="remember",
        outcome="ok",
        actor_client=author_client,
        project_id=project_id,
        memory_id=memory.id,
        revision_no=1,
        request_id=request_id,
        type=memory_type.value,
        content_length=len(clean_content),
        superseded_count=len(superseded),
    )

    if client_request_id is not None:
        await idempotency.complete(
            session,
            project_id=project_id,
            client_request_id=client_request_id,
            response={"memory_id": str(memory.id), "revision_no": 1},
        )

    _METRICS.increment(m.WRITES, type=memory_type.value, outcome="created")
    if superseded:
        _METRICS.increment(m.SUPERSESSIONS, value=len(superseded))

    return RememberResult(
        memory=to_view(memory, revision),
        outcome="created",
        superseded=tuple(superseded),
        not_superseded=tuple(not_superseded),
        attestation_count=attestations,
    )


async def _deduplicate(
    session: AsyncSession,
    project_id: uuid.UUID,
    holder_id: uuid.UUID,
    *,
    memory_type: MemoryType,
    author_client: str,
    request_id: str | None,
) -> RememberResult:
    """Return the existing memory and record the corroboration.

    The speculative insert has already been undone by the savepoint rollback, so
    the memory that was created and rejected leaves no trace. This function does
    not commit - the enclosing transaction belongs to the caller.
    """
    truth = TruthRepository(session)
    existing = await get_memory(session, project_id, holder_id)
    if existing is None:  # pragma: no cover - retired between rollback and re-read
        raise MemoryNotFoundError(
            f"Deduplication points at memory {holder_id}, which is no longer active.",
            memory_id=str(holder_id),
        )

    attestations = await truth.attest(project_id, holder_id, client_name=author_client)
    await AuditRepository(session).record(
        action="remember",
        outcome="dedup",
        actor_client=author_client,
        project_id=project_id,
        memory_id=holder_id,
        revision_no=existing.revision_no,
        request_id=request_id,
        attestation_count=attestations,
    )

    _METRICS.increment(m.WRITES, type=memory_type.value, outcome="deduplicated")
    _METRICS.increment(m.DEDUPLICATIONS)
    return RememberResult(memory=existing, outcome="deduplicated", attestation_count=attestations)


async def _apply_supersession(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    winner_id: uuid.UUID,
    targets: Sequence[uuid.UUID],
    author_client: str,
    request_id: str | None,
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Retire the memories this assertion replaces.

    Targets are locked in **ascending id order**. Every write path in the system
    takes memory row locks in a single consistent order, which is what makes the
    whole thing deadlock-free: two clients superseding overlapping sets cannot
    form a cycle.

    A target that was already retired, deleted, or absent is reported back rather
    than silently skipped. Telling a caller "I retired 3 memories" when only 2
    existed is a lie they would act on.
    """
    if not targets:
        return [], []

    truth = TruthRepository(session)
    audit = AuditRepository(session)
    retired: list[uuid.UUID] = []
    skipped: list[uuid.UUID] = []

    for target in sorted(set(targets)):
        if target == winner_id:
            skipped.append(target)
            continue
        if await truth.supersede(project_id, target, winner_id=winner_id):
            retired.append(target)
            await audit.record(
                action="supersede",
                outcome="ok",
                actor_client=author_client,
                project_id=project_id,
                memory_id=target,
                request_id=request_id,
                superseded_by=str(winner_id),
            )
        else:
            skipped.append(target)

    return retired, skipped


async def forget(
    session: AsyncSession,
    project_id: uuid.UUID,
    memory_id: uuid.UUID,
    *,
    reason: str | None = None,
    author_client: str = "unknown",
    request_id: str | None = None,
) -> ForgetResult:
    """Tombstone a memory.

    Reversible and content-preserving: this sets a status, it does not delete
    anything. Every revision stays in the log so ``memory_history`` can still
    explain what the memory said and when it stopped applying.

    Forgetting an already-retired memory is an idempotent no-op, not an error.
    """
    truth = TruthRepository(session)
    if not await truth.exists(project_id, memory_id):
        raise MemoryNotFoundError(
            f"No memory {memory_id} in this project.", memory_id=str(memory_id)
        )

    forgotten = await truth.forget(project_id, memory_id)
    await AuditRepository(session).record(
        action="forget",
        outcome="ok" if forgotten else "rejected",
        actor_client=author_client,
        project_id=project_id,
        memory_id=memory_id,
        request_id=request_id,
        reason=reason,
        already_retired=not forgotten,
    )
    _METRICS.increment(m.FORGETS, outcome="forgotten" if forgotten else "already_forgotten")
    return ForgetResult(
        memory_id=memory_id,
        outcome="forgotten" if forgotten else "already_forgotten",
    )


async def history(
    session: AsyncSession, project_id: uuid.UUID, memory_id: uuid.UUID
) -> MemoryHistory:
    """Everything the system knows about one memory, retired or not.

    This is the counterweight to stale-memory suppression. Retirement removes a
    memory from retrieval; it must not remove it from the record. Without this,
    the system would simply be deleting inconvenient history and there would be
    no way to ask why something changed.

    Note the ``include_retired=True``: this is one of only two places that flag
    is used, and it is the reason normal retrieval can be unconditional about
    what it excludes.
    """
    repo = MemoryRepository(session)
    truth = TruthRepository(session)

    row = await repo.get(project_id, memory_id, include_retired=True)
    if row is None:
        raise MemoryNotFoundError(
            f"No memory {memory_id} in this project.", memory_id=str(memory_id)
        )

    return MemoryHistory(
        memory=to_view(row[0], row[1]),
        revisions=tuple(await truth.revisions(memory_id)),
        superseded_by=await truth.superseded_by(project_id, memory_id),
        supersedes=tuple(await truth.supersedes(project_id, memory_id)),
        attestations=tuple(await truth.attestations(memory_id)),
        audit=tuple(await truth.audit_trail(memory_id)),
    )


async def revise(
    session: AsyncSession,
    project_id: uuid.UUID,
    memory_id: uuid.UUID,
    *,
    expected_revision: int,
    content: str,
    tags: Sequence[str] | None = None,
    change_reason: str | None = None,
    source: str | None = None,
    author_client: str = "unknown",
    author_kind: AuthorKind = AuthorKind.AGENT,
    client_request_id: str | None = None,
    request_id: str | None = None,
    embedding_model: str | None = None,
) -> ReviseResult:
    """Refine an existing memory, if nobody else changed it first.

    A conflict is **not an error**. The request was well formed and the database
    evaluated it correctly; the answer is "no, and here is the current state".
    The caller gets the current revision number, its content and who wrote it -
    everything needed to merge and retry in one round trip.

    Note the ordering: idempotency is claimed *before* the compare-and-set. A
    retry of an already-applied revise must replay, not conflict. Without that,
    a client whose connection dropped after a successful write would be told it
    lost a race it actually won.
    """
    clean_content = validate_content(content)
    clean_tags = validate_tags(list(tags) if tags else None)

    if client_request_id is not None:
        outcome = await idempotency.claim(
            session,
            project_id=project_id,
            client_request_id=client_request_id,
            operation="revise",
            request_fingerprint=idempotency.fingerprint(
                {
                    "memory_id": str(memory_id),
                    "expected_revision": expected_revision,
                    "content": clean_content,
                    "tags": list(clean_tags),
                }
            ),
        )
        if isinstance(outcome, idempotency.Replayed):
            replayed = await _replay_memory(session, project_id, outcome.response)
            _METRICS.increment(m.REVISIONS, outcome="idempotent_replay")
            _METRICS.increment(m.IDEMPOTENT_REPLAYS, operation="revise")
            return ReviseReplayed(memory=replayed)

    repo = MemoryRepository(session)
    truth = TruthRepository(session)
    existing = await repo.get(project_id, memory_id)
    if existing is None:
        raise MemoryNotFoundError(
            f"No active memory {memory_id} in this project.", memory_id=str(memory_id)
        )

    new_revision = await repo.compare_and_set(
        project_id,
        memory_id,
        expected_revision=expected_revision,
        content=clean_content,
        content_hash=content_hash(clean_content),
        hash_version=HASH_VERSION,
        tags=clean_tags,
        change_reason=change_reason,
        author_client=author_client,
        author_kind=author_kind,
        source=source,
    )

    audit = AuditRepository(session)

    if new_revision is None:
        # Re-read to report what actually won. This read happens after the failed
        # CAS, so it sees the committed state that beat us.
        current = await repo.get(project_id, memory_id)
        if current is None:  # pragma: no cover - retired between the two reads
            raise MemoryNotFoundError(
                f"No active memory {memory_id} in this project.", memory_id=str(memory_id)
            )
        await audit.record(
            action="revise",
            outcome="conflict",
            actor_client=author_client,
            project_id=project_id,
            memory_id=memory_id,
            revision_no=current[0].current_revision_no,
            request_id=request_id,
            expected_revision=expected_revision,
        )
        _METRICS.increment(m.REVISIONS, outcome="conflict")
        _METRICS.increment(m.CONFLICTS)
        return ReviseConflicted(
            current=to_view(current[0], current[1]), expected_revision=expected_revision
        )

    if embedding_model is not None:
        await truth.enqueue_embedding(
            project_id, memory_id, revision_no=new_revision, model=embedding_model
        )

    await audit.record(
        action="revise",
        outcome="ok",
        actor_client=author_client,
        project_id=project_id,
        memory_id=memory_id,
        revision_no=new_revision,
        request_id=request_id,
        previous_revision=expected_revision,
        content_length=len(clean_content),
    )

    if client_request_id is not None:
        await idempotency.complete(
            session,
            project_id=project_id,
            client_request_id=client_request_id,
            response={"memory_id": str(memory_id), "revision_no": new_revision},
        )

    updated = await repo.get(project_id, memory_id)
    if updated is None:  # pragma: no cover - we just wrote it
        raise MemoryNotFoundError(f"Memory {memory_id} vanished mid-transaction.")

    _METRICS.increment(m.REVISIONS, outcome="ok")
    return ReviseSucceeded(
        memory=to_view(updated[0], updated[1]), previous_revision=expected_revision
    )


async def _replay_memory(
    session: AsyncSession, project_id: uuid.UUID, response: dict[str, Any]
) -> MemoryView:
    """Rehydrate the memory a stored idempotent response points at."""
    memory_id = uuid.UUID(str(response["memory_id"]))
    view = await get_memory(session, project_id, memory_id)
    if view is None:  # pragma: no cover - would mean the memory was purged
        raise MemoryNotFoundError(
            f"Idempotent replay refers to memory {memory_id}, which no longer exists.",
            memory_id=str(memory_id),
        )
    return view


async def search(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    query: str | None = None,
    types: Sequence[MemoryType] | None = None,
    tags: Sequence[str] | None = None,
    limit: int | None = None,
) -> SearchResult:
    """Retrieve active memories, ranked by relevance and priors.

    **Two-stage matching.** PostgreSQL's ``websearch_to_tsquery`` joins bare
    terms with AND, so "connection pool size" demands all three lexemes and
    misses a memory saying "connection pooling is bounded at 10". Natural
    language is full of this, and it is what a model sends: the measured
    all-terms baseline scored 0.000 nDCG on queries as ordinary as "migration
    rules".

    So a query that finds nothing is retried with any-term matching. Precision is
    preserved for queries that do match strictly - the widening only happens when
    the alternative is returning nothing - and relevance still orders the result,
    since a memory matching three terms outranks one matching a single term.

    Queries using explicit syntax are never rewritten: ``queueing -redis`` means
    what it says, and turning it into an OR would invert the caller's intent.

    The strategy that produced the results is reported rather than hidden, so a
    caller reading loosely matched results knows that is what they are.
    """
    clean_limit = validate_limit(limit)
    clean_tags = validate_tags(list(tags) if tags else None)
    clean_query = query.strip() if query and query.strip() else None

    repo = MemoryRepository(session)

    async def run(text_query: str | None) -> tuple[tuple[MemoryView, ...], int]:
        rows = await repo.search(
            project_id, query=text_query, types=types, tags=clean_tags, limit=clean_limit
        )
        total = await repo.count(project_id, query=text_query, types=types, tags=clean_tags)
        return tuple(to_view(memory, revision) for memory, revision in rows), total

    memories, total = await run(clean_query)
    if memories or clean_query is None:
        return SearchResult(memories=memories, total_considered=total, match_strategy="all_terms")

    widened = lexical.any_term_query(clean_query)
    if widened is None:
        return SearchResult(memories=memories, total_considered=total, match_strategy="all_terms")

    memories, total = await run(widened)
    _METRICS.increment(m.SEARCH_WIDENED)
    return SearchResult(memories=memories, total_considered=total, match_strategy="any_term")


async def get_memory(
    session: AsyncSession, project_id: uuid.UUID, memory_id: uuid.UUID
) -> MemoryView | None:
    repo = MemoryRepository(session)
    row = await repo.get(project_id, memory_id)
    return to_view(row[0], row[1]) if row else None
