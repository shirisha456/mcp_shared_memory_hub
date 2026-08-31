"""Ranking priors.

Relevance answers "does this text match the query". It does not answer "is this
worth telling the agent about", and those come apart constantly: a throwaway note
mentioning PostgreSQL can out-match the architectural constraint that governs the
whole project.

Three priors, applied multiplicatively on top of lexical relevance:

``importance``
    Set per memory, defaulted by type. A CONSTRAINT outranks a FACT because
    violating one breaks the project and forgetting the other is an
    inconvenience.

``recency``
    Exponential decay with a **type-dependent half-life**. This is the prior that
    earns its place: a TASK saying "currently implementing X" is worthless after
    a month, while a DECISION from a year ago may be the single most important
    thing in the corpus. One global decay rate would have to be wrong for one of
    them.

``type weight``
    A small nudge so that, all else equal, a decision beats an observation.

**These weights are untuned, and that is deliberate.** Milestone 6 builds the
evaluation harness; only then is there a measurement to tune against. Tuning them
now would mean fitting numbers to intuition and then building the ruler that
agrees with them. They live here as named constants so that tuning later is a
single-file change, and the current values are recorded as the baseline the
harness will measure.

Multiplicative rather than additive, because a prior should *scale* relevance
rather than substitute for it: a memory that does not match the query at all
scores zero, and no amount of importance should rescue it.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from sqlalchemy import Case, ColumnElement, Float, case, func, literal

from memhub.domain.enums import MemoryType
from memhub.persistence.models import Memory

# --- weights -----------------------------------------------------------------
# Each is the maximum proportional boost the prior can apply. w_importance = 0.5
# means a maximally important memory scores 1.5x a minimally important one, all
# else equal. Untuned; see the module docstring.
W_IMPORTANCE: Final[float] = 0.5
W_RECENCY: Final[float] = 0.3

RECENCY_HALF_LIFE: Final[dict[MemoryType, dt.timedelta]] = {
    # Effectively never decays. A constraint from two years ago still binds.
    MemoryType.CONSTRAINT: dt.timedelta(days=3650),
    MemoryType.DECISION: dt.timedelta(days=365),
    MemoryType.FACT: dt.timedelta(days=180),
    # Aggressive. "Currently implementing X" is stale within days, and a TASK
    # that has drifted out of relevance should fall behind before its TTL
    # removes it entirely.
    MemoryType.TASK: dt.timedelta(days=7),
}

TYPE_WEIGHT: Final[dict[MemoryType, float]] = {
    MemoryType.CONSTRAINT: 1.15,
    MemoryType.DECISION: 1.10,
    MemoryType.FACT: 1.00,
    MemoryType.TASK: 0.95,
}


def importance_prior() -> ColumnElement[float]:
    """1.0 at importance 0, 1 + W_IMPORTANCE at importance 100."""
    return literal(1.0) + literal(W_IMPORTANCE) * (Memory.importance / literal(100.0))


def recency_prior(*, now: ColumnElement[dt.datetime] | None = None) -> ColumnElement[float]:
    """Exponential decay, half-life chosen by memory type.

    ``0.5 ^ (age_days / half_life_days)``: 1.0 at creation, 0.5 after one
    half-life, approaching zero thereafter.

    Age is measured against the **database** clock. Using the application clock
    would let two server processes disagree about how old a memory is, and the
    ordering of results would then depend on which process answered.
    """
    reference = func.now() if now is None else now
    age_days = func.extract("epoch", reference - Memory.created_at) / literal(86400.0)

    half_life = case(
        *[
            (Memory.type == memory_type.value, literal(float(delta.days)))
            for memory_type, delta in RECENCY_HALF_LIFE.items()
        ],
        else_=literal(365.0),
    )

    decay = func.power(literal(0.5), age_days / half_life).cast(Float)
    return literal(1.0) + literal(W_RECENCY) * decay


def type_prior() -> Case[float]:
    return case(
        *[
            (Memory.type == memory_type.value, literal(weight))
            for memory_type, weight in TYPE_WEIGHT.items()
        ],
        else_=literal(1.0),
    )


def final_score(relevance: ColumnElement[float]) -> ColumnElement[float]:
    """Lexical relevance, scaled by the priors.

    Nothing here can promote a memory that does not match the query: relevance is
    a factor, so a zero stays zero however important or recent the memory is.
    """
    return relevance * importance_prior() * recency_prior() * type_prior()
