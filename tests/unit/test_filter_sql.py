"""The stage-0 filter compiles to the SQL the indexes need.

This exists because of a bug that cost real time to find, and that no other test
in the suite could have caught.

The full-text index is partial - ``... WHERE is_current``. PostgreSQL will only
use it if it can prove the query's predicate implies the index's. It proves that
for a bare boolean column, but **not** for ``is_current IS TRUE``, because
``IS TRUE`` is null-safe and therefore a different expression.

Written the wrong way, the index becomes unusable at any corpus size. Nothing
fails: results are still correct, tests stay green, and the latency cost only
appears once the corpus is large enough to matter - by which point nobody
connects it to a two-word change made months earlier.

A latency test cannot catch this on a small corpus, and a plan assertion cannot
either, because at 10k rows PostgreSQL legitimately prefers other access paths
whether or not the index is usable. Checking the compiled SQL is the one place
the regression is unambiguous, so that is where it is checked.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.dialects import postgresql

from memhub.retrieval.filters import current_revisions

PROJECT_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


def compiled_sql(*, include_retired: bool = False) -> str:
    statement = current_revisions(PROJECT_ID, include_retired=include_retired)
    # postgresql.dialect() is untyped in SQLAlchemy's stubs.
    dialect: Any = postgresql.dialect()  # type: ignore[no-untyped-call]
    return str(statement.compile(dialect=dialect))


class TestPartialIndexCompatibility:
    def test_is_current_is_a_bare_boolean(self) -> None:
        sql = compiled_sql()
        assert "memory_revisions.is_current" in sql
        assert "is_current IS true" not in sql.lower().replace("IS TRUE", "IS true"), (
            "the currency predicate compiled to `IS TRUE`, which PostgreSQL "
            "cannot prove implies the partial index predicate `WHERE is_current`. "
            "The full-text and tag indexes become unusable. Use the bare column."
        )

    def test_no_is_true_anywhere_in_the_filter(self) -> None:
        """Belt and braces: the same trap applies to the partial tag index."""
        assert " IS TRUE" not in compiled_sql().upper()


class TestFilterContents:
    def test_project_scope_is_always_present(self) -> None:
        """Isolation is not optional, in either mode."""
        assert "memories.project_id = " in compiled_sql()
        assert "memories.project_id = " in compiled_sql(include_retired=True)

    def test_normal_retrieval_excludes_retired_and_expired(self) -> None:
        sql = compiled_sql()
        assert "memories.status = " in sql
        assert "memories.expires_at IS NULL OR memories.expires_at > now()" in sql
        assert "memory_revisions.is_current" in sql

    def test_expiry_uses_the_database_clock(self) -> None:
        """``now()``, not a bound parameter.

        A timestamp bound by the application would let two server processes
        disagree about what has expired, and which memories came back would
        depend on which process answered.
        """
        assert "now()" in compiled_sql()

    def test_include_retired_drops_only_the_lifecycle_conditions(self) -> None:
        # Inspect the WHERE clause, not the whole statement: every column name,
        # expires_at included, appears in the SELECT list regardless of filtering.
        where = compiled_sql(include_retired=True).split("WHERE", 1)[1]
        assert "memories.status = " not in where
        assert "expires_at" not in where
        assert "is_current" not in where
        # ...but never the project scope. Even debug paths stay scoped.
        assert "memories.project_id = " in where
