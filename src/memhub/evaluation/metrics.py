"""Retrieval quality metrics.

Pure functions over ranked id lists and graded judgments. No database, no
imports from the rest of the package - so they can be unit-tested against
worked examples where the right answer is known by hand, which is the only way
to be sure a metric implementation is correct.

**Why nDCG rather than MRR.** MRR credits only the position of the *first*
relevant result. Our queries usually have several relevant memories at different
degrees of relevance - a decision that directly answers the question, plus a
constraint that qualifies it - and MRR would score "perfect answer first,
everything else missing" identically to "perfect answer first, everything else
present". nDCG uses the whole ranking and respects grades. MRR is still computed
and reported, because it is cheap and occasionally illuminating, but it is not
what a change is judged on.

**Why stale inclusion is reported separately.** It is not a quality metric to be
traded off against the others; it is a correctness metric with a target of
exactly zero. A retrieval change that improved nDCG while surfacing one retired
memory would be a regression, not an improvement, and averaging it into a
quality score would hide that.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

Judgments = Mapping[str, int]
"""memory id -> graded relevance. 0 irrelevant, 1 useful, 2 directly answers."""


def dcg(gains: Sequence[float]) -> float:
    """Discounted cumulative gain, using the exponential gain formulation.

    ``gain = 2^rel - 1`` rather than ``rel`` itself, so the difference between
    "directly answers" (2) and "useful" (1) is 3 versus 1 rather than 2 versus 1.
    That gap matters here: a memory that answers the question and one that merely
    relates to it are not two-thirds as different as the raw grades suggest.
    """
    return sum((2**gain - 1) / math.log2(position + 2) for position, gain in enumerate(gains))


def ndcg_at_k(ranked: Sequence[str], judgments: Judgments, k: int = 10) -> float:
    """Normalised DCG at k, in [0, 1].

    Returns 0.0 when no relevant memory exists for the query - not 1.0. A query
    with nothing to find should not count as a perfect result; such queries are
    excluded from the mean by the harness rather than silently inflating it.
    """
    ideal_gains = sorted(judgments.values(), reverse=True)[:k]
    ideal = dcg([float(gain) for gain in ideal_gains])
    if ideal == 0:
        return 0.0

    actual = dcg([float(judgments.get(memory_id, 0)) for memory_id in ranked[:k]])
    return actual / ideal


def recall_at_k(ranked: Sequence[str], judgments: Judgments, k: int = 10) -> float:
    """Fraction of relevant memories that appear in the top k.

    Any positive grade counts. Recall answers "did we find them at all", which is
    a different question from nDCG's "did we order them well", and a retriever can
    be good at one and bad at the other.
    """
    relevant = {memory_id for memory_id, grade in judgments.items() if grade > 0}
    if not relevant:
        return 0.0
    return len(relevant & set(ranked[:k])) / len(relevant)


def precision_at_k(ranked: Sequence[str], judgments: Judgments, k: int = 10) -> float:
    """Fraction of the top k that is relevant.

    Reported because it is what a context budget actually spends: at k=5 with a
    2000-token budget, an irrelevant result is not merely noise, it displaces
    something useful.
    """
    if not ranked[:k]:
        return 0.0
    return sum(1 for memory_id in ranked[:k] if judgments.get(memory_id, 0) > 0) / len(ranked[:k])


def reciprocal_rank(ranked: Sequence[str], judgments: Judgments) -> float:
    """1 / position of the first relevant result. Secondary; see the module docstring."""
    for position, memory_id in enumerate(ranked, start=1):
        if judgments.get(memory_id, 0) > 0:
            return 1.0 / position
    return 0.0


def stale_inclusion(ranked: Sequence[str], forbidden: Sequence[str], k: int = 10) -> bool:
    """Whether any memory that must never be returned appears in the top k.

    The metric this project exists for. A superseded fact surfacing here is not a
    ranking failure to be weighed against a quality gain - it is the system
    telling a client something that is no longer true.
    """
    return bool(set(ranked[:k]) & set(forbidden))
