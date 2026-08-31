"""Idempotent writes.

**Idempotency is not deduplication.** They get conflated constantly, and keeping
them apart is a design decision:

============  ==========================================  ==============================
              Idempotency (here)                          Deduplication (Milestone 3)
============  ==========================================  ==============================
Trigger       *one* client retries *the same request*     *two* clients assert the same fact
Key           ``client_request_id``, caller-generated     normalised content hash
Right answer  replay the original response                point at the existing memory,
                                                          and record a second attestation
Without it    a network retry creates a duplicate memory  the corpus fills with near-copies
============  ==========================================  ==============================

Not every write needs a key. ``memory_revise`` is already safe without one - the
compare-and-set makes a duplicate write impossible, because a retry of a
committed revise simply fails the version check. The key exists there to turn a
confusing answer ("conflict", when in fact *you* were the one who succeeded)
into a clear one ("this already landed, here it is"). Knowing when idempotency
is required for *correctness* versus for *ergonomics* is the actual insight.

``project_use`` needs no key at all: the unique slug already makes a retry a
no-op.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from memhub.domain.errors import MemhubError, ValidationFailedError
from memhub.persistence.models import IdempotencyKey
from memhub.persistence.sql import CLAIM_IDEMPOTENCY, WAIT_IDEMPOTENCY, load

MIN_KEY_LENGTH: Final[int] = 8
MAX_KEY_LENGTH: Final[int] = 128
MAX_CLAIM_ATTEMPTS: Final[int] = 3
"""Bounded so two clients failing and retrying the same key cannot ping-pong."""


class IdempotencyKeyReusedError(MemhubError):
    """Same key, different request.

    Returning the stored response here would answer a question the caller never
    asked - which is worse than failing, because it fails silently.
    """

    code = "IDEMPOTENCY_KEY_REUSED"


@dataclass(frozen=True, slots=True)
class Claimed:
    """This caller owns the key and must do the work."""


@dataclass(frozen=True, slots=True)
class Replayed:
    """Another attempt already completed this request. Return its result."""

    response: dict[str, Any]


ClaimOutcome = Claimed | Replayed


def fingerprint(payload: dict[str, Any]) -> bytes:
    """Hash the semantically meaningful arguments of a request.

    Canonical JSON - sorted keys, no incidental whitespace - so that two
    encodings of the same request produce the same digest.

    Deliberately excludes incidental fields such as ``source``: a retry that
    differs only in free-text metadata is still the same request, and failing it
    would make idempotency more annoying than useful.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def validate_key(client_request_id: str) -> str:
    value = client_request_id.strip()
    if not MIN_KEY_LENGTH <= len(value) <= MAX_KEY_LENGTH:
        raise ValidationFailedError(
            f"client_request_id must be {MIN_KEY_LENGTH}-{MAX_KEY_LENGTH} characters, "
            f"got {len(value)}. A UUID is a good choice.",
            length=len(value),
        )
    return value


async def claim(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    client_request_id: str,
    operation: str,
    request_fingerprint: bytes,
) -> ClaimOutcome:
    """Claim the key, or wait for whoever already has it.

    Must run inside the same transaction as the write it protects. That is what
    makes the whole thing atomic: there is no state in which the key says
    COMPLETED but the memory does not exist, because both are the same commit.

    The protocol:

    1. ``INSERT ... ON CONFLICT DO NOTHING``. One row back means we own it.
    2. Zero rows means someone else does. ``SELECT ... FOR SHARE`` blocks until
       that transaction resolves.
    3. A row comes back - they committed. Verify the fingerprint matches and
       replay their stored response.
    4. No row comes back - they rolled back, which rolled their INSERT back too.
       The key is free; loop and claim it.
    """
    key = validate_key(client_request_id)

    for _ in range(MAX_CLAIM_ATTEMPTS):
        claimed = (
            await session.execute(
                load(CLAIM_IDEMPOTENCY),
                {
                    "project_id": project_id,
                    "client_request_id": key,
                    "operation": operation,
                    "request_fingerprint": request_fingerprint,
                },
            )
        ).first()

        if claimed is not None:
            return Claimed()

        existing = (
            await session.execute(
                load(WAIT_IDEMPOTENCY),
                {"project_id": project_id, "client_request_id": key},
            )
        ).first()

        if existing is None:
            # The owner rolled back and took its claim with it. Try again.
            continue

        state, response, stored_fingerprint, stored_operation = existing

        if bytes(stored_fingerprint) != request_fingerprint or stored_operation != operation:
            raise IdempotencyKeyReusedError(
                f"client_request_id {key!r} was already used for a different "
                "request in this project. Reusing a key with a changed payload "
                "would return a result for a request you did not make. Use a new "
                "key, or resend the original request unchanged.",
                client_request_id=key,
            )

        if state == "COMPLETED" and response is not None:
            return Replayed(response=dict(response))

        # IN_PROGRESS after FOR SHARE returned means the owner committed without
        # finishing - a bug in the caller, not a race. Fail rather than guess.
        raise MemhubError(
            f"Idempotency key {key!r} is in an inconsistent state: the owning "
            "transaction committed without recording a response."
        )

    raise MemhubError(
        f"Could not claim idempotency key {key!r} after {MAX_CLAIM_ATTEMPTS} attempts."
    )


async def complete(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    client_request_id: str,
    response: dict[str, Any],
) -> None:
    """Record the result, in the same transaction as the write itself."""
    row = await session.get(
        IdempotencyKey, {"project_id": project_id, "client_request_id": client_request_id}
    )
    if row is None:  # pragma: no cover - we claimed it moments ago
        raise MemhubError(f"Idempotency key {client_request_id!r} vanished mid-transaction.")

    row.state = "COMPLETED"
    row.response = response
    # The database clock, never the application's: two server processes must not
    # be able to disagree about when something completed.
    row.completed_at = (await session.execute(select(func.now()))).scalar_one()
    await session.flush()


async def purge_expired(session: AsyncSession, *, limit: int = 1000) -> int:
    """Delete expired keys, a bounded batch at a time.

    Bounded because an unbounded ``DELETE`` on a busy table takes a long lock and
    becomes its own outage. Returns the number removed so a caller can loop until
    it reaches zero.
    """
    doomed = (
        select(IdempotencyKey.project_id, IdempotencyKey.client_request_id)
        .where(IdempotencyKey.expires_at < func.now())
        .limit(limit)
        .subquery()
    )
    result = await session.execute(
        delete(IdempotencyKey).where(
            (IdempotencyKey.project_id == doomed.c.project_id)
            & (IdempotencyKey.client_request_id == doomed.c.client_request_id)
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)
