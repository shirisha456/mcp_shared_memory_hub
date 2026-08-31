"""Validation and type policy.

The TASK tests are the important ones. TASK is the type most likely to drag this
project toward being a bad issue tracker, and the TTL ceiling is the mechanism
that stops it - a caller asking for a two-year "currently implementing X" is not
recording working state.
"""

from __future__ import annotations

import datetime as dt

import pytest

from memhub.domain.enums import MemoryType
from memhub.domain.errors import ValidationFailedError
from memhub.domain.policy import MAX_CONTENT_LENGTH, TYPE_POLICY
from memhub.domain.validation import (
    resolve_expiry,
    validate_content,
    validate_importance,
    validate_limit,
    validate_slug,
    validate_tags,
)

NOW = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)


class TestContent:
    def test_strips_surrounding_whitespace(self) -> None:
        assert validate_content("  hello  ") == "hello"

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationFailedError, match="must not be empty"):
            validate_content("   ")

    def test_accepts_maximum_length(self) -> None:
        assert len(validate_content("x" * MAX_CONTENT_LENGTH)) == MAX_CONTENT_LENGTH

    def test_rejects_one_over_and_says_by_how_much(self) -> None:
        with pytest.raises(ValidationFailedError, match=f"{MAX_CONTENT_LENGTH + 1} characters"):
            validate_content("x" * (MAX_CONTENT_LENGTH + 1))


class TestTags:
    def test_normalises_case_and_whitespace(self) -> None:
        assert validate_tags([" Queue ", "POSTGRES"]) == ("queue", "postgres")

    def test_deduplicates_preserving_order(self) -> None:
        """Order must be deterministic: search results are snapshot-tested."""
        assert validate_tags(["b", "a", "B", "a"]) == ("b", "a")

    def test_rejects_invalid_characters(self) -> None:
        with pytest.raises(ValidationFailedError, match="Invalid tag"):
            validate_tags(["has space"])

    def test_rejects_too_many(self) -> None:
        with pytest.raises(ValidationFailedError, match="the limit is 16"):
            validate_tags([f"tag{i}" for i in range(17)])

    def test_duplicates_do_not_count_towards_the_limit(self) -> None:
        assert len(validate_tags(["a"] * 20)) == 1

    def test_none_and_empty_are_empty(self) -> None:
        assert validate_tags(None) == ()
        assert validate_tags([]) == ()


class TestImportance:
    def test_defaults_come_from_type_policy(self) -> None:
        for memory_type, policy in TYPE_POLICY.items():
            assert validate_importance(None, memory_type) == policy.base_importance

    def test_constraints_outrank_tasks_by_default(self) -> None:
        """The type policy must actually encode a priority ordering, or the
        types are not earning their existence."""
        assert validate_importance(None, MemoryType.CONSTRAINT) > validate_importance(
            None, MemoryType.DECISION
        )
        assert validate_importance(None, MemoryType.DECISION) > validate_importance(
            None, MemoryType.FACT
        )
        assert validate_importance(None, MemoryType.FACT) > validate_importance(
            None, MemoryType.TASK
        )

    @pytest.mark.parametrize("value", [-1, 101])
    def test_rejects_out_of_range(self, value: int) -> None:
        with pytest.raises(ValidationFailedError, match="between 0 and 100"):
            validate_importance(value, MemoryType.FACT)


class TestExpiry:
    @pytest.mark.parametrize(
        "memory_type", [MemoryType.DECISION, MemoryType.CONSTRAINT, MemoryType.FACT]
    )
    def test_durable_types_do_not_expire_by_default(self, memory_type: MemoryType) -> None:
        assert resolve_expiry(None, memory_type, now=NOW) is None

    def test_task_gets_a_default_ttl(self) -> None:
        """TASK is the only type with a mandatory expiry.

        Without it, 'currently implementing X' outlives the work it describes and
        the corpus fills with finished work presented as in progress.
        """
        expiry = resolve_expiry(None, MemoryType.TASK, now=NOW)
        assert expiry == NOW + dt.timedelta(days=7)

    def test_task_ttl_is_capped(self) -> None:
        with pytest.raises(ValidationFailedError, match="short-lived working state"):
            resolve_expiry(NOW + dt.timedelta(days=365), MemoryType.TASK, now=NOW)

    def test_task_accepts_expiry_inside_the_cap(self) -> None:
        requested = NOW + dt.timedelta(days=14)
        assert resolve_expiry(requested, MemoryType.TASK, now=NOW) == requested

    def test_durable_types_accept_a_long_explicit_expiry(self) -> None:
        """Only TASK has a ceiling; an explicitly time-boxed FACT is legitimate."""
        requested = NOW + dt.timedelta(days=365)
        assert resolve_expiry(requested, MemoryType.FACT, now=NOW) == requested

    def test_rejects_past_expiry(self) -> None:
        with pytest.raises(ValidationFailedError, match="in the past"):
            resolve_expiry(NOW - dt.timedelta(days=1), MemoryType.FACT, now=NOW)

    def test_rejects_naive_datetime(self) -> None:
        """A naive timestamp is ambiguous across the two server processes."""
        with pytest.raises(ValidationFailedError, match="timezone-aware"):
            resolve_expiry(dt.datetime(2030, 1, 1), MemoryType.FACT, now=NOW)


class TestSlug:
    @pytest.mark.parametrize("slug", ["ai-agent-control-plane", "memhub", "a1", "x.y_z-1"])
    def test_accepts_valid(self, slug: str) -> None:
        assert validate_slug(slug) == slug

    def test_folds_case(self) -> None:
        assert validate_slug("MemHub") == "memhub"

    @pytest.mark.parametrize("slug", ["a", "-leading", "trailing-", "has space", "UPPER!"])
    def test_rejects_invalid(self, slug: str) -> None:
        with pytest.raises(ValidationFailedError, match="Invalid project slug"):
            validate_slug(slug)


class TestLimit:
    def test_defaults(self) -> None:
        assert validate_limit(None) == 10

    @pytest.mark.parametrize("value", [0, -1, 101])
    def test_rejects_out_of_range(self, value: int) -> None:
        with pytest.raises(ValidationFailedError):
            validate_limit(value)
