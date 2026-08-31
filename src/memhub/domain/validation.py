"""Input validation.

Every rule here is *also* a database ``CHECK`` constraint. That duplication is
deliberate: the constraint is the guarantee (it holds even if a future code path
forgets to call these functions), and this module is the good error message.
A caller should learn "content is 9102 characters, the limit is 8192", not
``IntegrityError: ck_memory_revisions_content_length``.
"""

from __future__ import annotations

import datetime as dt
import re

from memhub.domain.enums import MemoryType
from memhub.domain.errors import ValidationFailedError
from memhub.domain.policy import (
    MAX_CONTENT_LENGTH,
    MAX_SEARCH_LIMIT,
    MAX_TAG_LENGTH,
    MAX_TAGS,
    TYPE_POLICY,
)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}[a-z0-9]$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,31}$")


def validate_slug(slug: str) -> str:
    value = slug.strip().casefold()
    if not _SLUG_RE.match(value):
        raise ValidationFailedError(
            f"Invalid project slug {slug!r}. Use 2-64 lowercase characters: "
            "letters, digits, dot, underscore or hyphen, starting and ending "
            "with a letter or digit.",
            slug=slug,
        )
    return value


def validate_content(content: str) -> str:
    stripped = content.strip()
    if not stripped:
        raise ValidationFailedError("Memory content must not be empty.")
    if len(stripped) > MAX_CONTENT_LENGTH:
        raise ValidationFailedError(
            f"Memory content is {len(stripped)} characters; the limit is "
            f"{MAX_CONTENT_LENGTH}. Split it into separate memories, each "
            "stating one fact, rather than storing a document.",
            length=len(stripped),
            limit=MAX_CONTENT_LENGTH,
        )
    return stripped


def validate_tags(tags: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Normalise, deduplicate and bound a tag list.

    Order is preserved after deduplication so that output is deterministic for a
    given input - which matters because search results are snapshot-tested.
    """
    if not tags:
        return ()

    seen: dict[str, None] = {}
    for raw in tags:
        value = raw.strip().casefold()
        if not value:
            continue
        if not _TAG_RE.match(value):
            raise ValidationFailedError(
                f"Invalid tag {raw!r}. Use up to {MAX_TAG_LENGTH} lowercase "
                "characters: letters, digits, dot, underscore, hyphen or slash.",
                tag=raw,
            )
        seen[value] = None

    if len(seen) > MAX_TAGS:
        raise ValidationFailedError(
            f"{len(seen)} tags supplied; the limit is {MAX_TAGS}.",
            count=len(seen),
            limit=MAX_TAGS,
        )
    return tuple(seen)


def validate_importance(importance: int | None, memory_type: MemoryType) -> int:
    """Default from the type policy, then bound."""
    if importance is None:
        return TYPE_POLICY[memory_type].base_importance
    if not 0 <= importance <= 100:
        raise ValidationFailedError(
            f"importance must be between 0 and 100, got {importance}.",
            importance=importance,
        )
    return importance


def resolve_expiry(
    expires_at: dt.datetime | None,
    memory_type: MemoryType,
    *,
    now: dt.datetime,
) -> dt.datetime | None:
    """Apply the type's TTL policy.

    TASK is the only type with a mandatory expiry, and the only one with a
    ceiling. A caller asking for a 2-year "currently implementing X" is not
    recording working state; they are trying to use this as an issue tracker,
    and the cap is what stops that quietly succeeding.
    """
    policy = TYPE_POLICY[memory_type]

    if expires_at is None:
        if policy.default_ttl is None:
            return None
        return now + policy.default_ttl

    if expires_at.tzinfo is None:
        raise ValidationFailedError(
            "expires_at must be timezone-aware. Send an ISO-8601 timestamp with "
            "an offset, e.g. 2026-01-31T12:00:00Z.",
        )
    if expires_at <= now:
        raise ValidationFailedError(
            f"expires_at {expires_at.isoformat()} is in the past.",
            expires_at=expires_at.isoformat(),
        )
    if policy.max_ttl is not None:
        ceiling = now + policy.max_ttl
        if expires_at > ceiling:
            raise ValidationFailedError(
                f"{memory_type.value} memories expire within "
                f"{policy.max_ttl.days} days; {expires_at.isoformat()} is later "
                f"than {ceiling.isoformat()}. This type holds short-lived "
                "working state, not long-term plans.",
                max_days=policy.max_ttl.days,
            )
    return expires_at


def validate_limit(limit: int | None) -> int:
    from memhub.domain.policy import DEFAULT_SEARCH_LIMIT

    if limit is None:
        return DEFAULT_SEARCH_LIMIT
    if limit < 1:
        raise ValidationFailedError(f"limit must be at least 1, got {limit}.", limit=limit)
    if limit > MAX_SEARCH_LIMIT:
        raise ValidationFailedError(
            f"limit must be at most {MAX_SEARCH_LIMIT}, got {limit}.",
            limit=limit,
            max_limit=MAX_SEARCH_LIMIT,
        )
    return limit
