"""Full-text matching and scoring.

Two choices here are worth defending.

``websearch_to_tsquery`` rather than ``to_tsquery``
    A model composes the query string, and it will write things like
    ``"task queue" -redis`` or ``postgres or sqlite``. ``to_tsquery`` raises a
    syntax error on anything that is not a strict boolean expression, which
    would turn an ordinary question into a tool failure. ``websearch_to_tsquery``
    accepts what a search box accepts and never raises.

``ts_rank_cd`` rather than ``ts_rank``
    Cover density rewards query terms appearing *near each other*. Memories are
    one or two sentences, so proximity is close to the whole signal: a memory
    that says "PostgreSQL is the task queue" should beat one that mentions
    PostgreSQL in one clause and queues in another.

**The scale caveat, which matters later.** ``ts_rank_cd`` is unbounded and
depends on document length and term frequency in *this* document - not on the
corpus. Its ordering is meaningful within a single query and meaningless across
queries, so this module never thresholds on the value, never compares it between
queries, and never adds it to anything on a different scale. When semantic
search arrives, fusion will happen in **rank space** (Reciprocal Rank Fusion),
precisely because these numbers cannot be summed with cosine similarities.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, Float, func, literal_column

from memhub.persistence.models import MemoryRevision

TEXT_SEARCH_CONFIG = "english"
"""Fixed rather than per-project.

Changing it would silently invalidate every stored ``content_tsv``, because the
generated column bakes the configuration in. Making it configurable would mean a
setting that quietly corrupts the index when changed - so it is a constant, and
supporting other languages would be a migration and a re-index, deliberately.
"""


def to_query(query: str) -> ColumnElement[str]:
    """Parse a user or model supplied string into a tsquery.

    The configuration must be a ``regconfig``, not text. PostgreSQL has no
    ``websearch_to_tsquery(varchar, varchar)`` overload, so a plain string
    parameter fails at execution time with "function does not exist" - a
    confusing way to learn that the first argument is a catalog reference rather
    than a name.

    It is emitted as a SQL literal rather than a bound parameter for two
    reasons: a bound ``REGCONFIG`` cannot be rendered by ``literal_binds``, which
    the ``EXPLAIN ANALYZE`` path in the performance tests needs; and inlining it
    lets the planner see a constant. Safe to inline because
    ``TEXT_SEARCH_CONFIG`` is a module constant - the *query* stays a bound
    parameter, which is what actually carries untrusted input.
    """
    return func.websearch_to_tsquery(literal_column(f"'{TEXT_SEARCH_CONFIG}'::regconfig"), query)


def matches(query: str) -> ColumnElement[bool]:
    """The predicate that uses the GIN index.

    ``@@`` against the stored generated column, so the index on
    ``content_tsv WHERE is_current`` can be used directly. Computing the vector
    at query time instead would force a sequential scan over every row.
    """
    return MemoryRevision.content_tsv.op("@@")(to_query(query))


def relevance(query: str) -> ColumnElement[float]:
    """Cover-density rank for this query.

    Normalisation flag 32 divides by ``rank + 1``, mapping the unbounded raw
    score into ``[0, 1)``. That does not make values comparable across queries -
    nothing does - but it keeps them in a predictable range so the multiplicative
    priors in :mod:`memhub.retrieval.ranking` behave consistently instead of
    being swamped by an occasional very large rank.
    """
    return func.ts_rank_cd(
        MemoryRevision.content_tsv,
        to_query(query),
        32,
    ).cast(Float)


# Characters and words that mean the caller is using explicit search syntax.
# A quoted phrase, a negation, or an existing OR is a deliberate expression of
# intent, and rewriting it would answer a different question than the one asked.
_EXPLICIT_SYNTAX = ('"', "-", " or ", " OR ")


def any_term_query(query: str) -> str | None:
    """Rewrite a plain query so any single term matches, or ``None`` to decline.

    ``websearch_to_tsquery`` joins bare terms with AND, so "connection pool size"
    requires all three lexemes and misses a memory that says "connection pooling
    is bounded at 10" - it has no "size". Natural-language questions are mostly
    like this: "what queue does this project use" demands *queue* and *project*
    and *use* together, and returns nothing.

    That is the single largest weakness in the measured full-text baseline, so
    the search path retries with this form when the strict query finds nothing.
    Relevance still does the work: ``ts_rank_cd`` ranks a memory matching three
    terms above one matching one, so widening the net does not flatten the
    ordering.

    Returns ``None`` when the query already uses explicit syntax. A caller who
    wrote ``queueing -redis`` meant it, and turning that into an OR would
    silently invert their intent.
    """
    if any(marker in query for marker in _EXPLICIT_SYNTAX):
        return None

    terms = query.split()
    if len(terms) < 2:
        return None
    return " or ".join(terms)
