"""Combining two rankings.

**Reciprocal Rank Fusion, not a weighted sum of scores.**

``ts_rank_cd`` is unbounded, corpus- and length-dependent. Cosine distance is in
[0, 2]. Adding them is meaningless, and the usual fix - min-max normalising each
per query - is also wrong here: it is *query-dependent*, so a query whose best
match is mediocre gets its top result scaled to 1.0 exactly like a query with a
perfect match. The two become indistinguishable at precisely the moment the
difference matters.

RRF sidesteps the problem by discarding the scores entirely and using only
position:

    score(d) = sum over retrievers of  1 / (k + rank(d))

Properties that make it the right tool here:

* **Scale-free.** Nothing needs normalising because no magnitudes are compared.
* **Robust to one retriever failing.** If the vector index is empty because the
  outbox is behind, those documents simply do not appear in that list and
  contribute nothing - no special case, no zero-filling, no skew.
* **Rewards agreement.** A document both retrievers rank highly beats one that
  either ranks first alone, which is exactly the signal wanted from combining
  a lexical and a semantic view.

``k = 60`` is the value from the original TREC work and the de facto default. It
damps the difference between the top few positions: without it, rank 1 would be
worth twice rank 2, which overweights a single retriever's confident mistake.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

RRF_K = 60


@dataclass(frozen=True, slots=True)
class FusedResult:
    memory_id: uuid.UUID
    score: float
    lexical_rank: int | None
    semantic_rank: int | None

    @property
    def found_by_both(self) -> bool:
        return self.lexical_rank is not None and self.semantic_rank is not None


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[uuid.UUID]],
    *,
    k: int = RRF_K,
    weights: Mapping[str, float] | None = None,
) -> list[FusedResult]:
    """Fuse named rankings into one ordered list.

    ``weights`` scales a retriever's contribution - useful when one is known to
    be better on a given corpus. Defaults to 1.0 for each, because weighting
    before measuring is guesswork, and the eval harness exists to replace guesses
    with numbers.

    Ties break on memory id, so identical inputs always produce identical output.
    Without that, two documents with equal fused scores would come back in
    dictionary order, which is stable within a process and not across them.
    """
    weights = weights or {}
    scores: dict[uuid.UUID, float] = {}
    positions: dict[str, dict[uuid.UUID, int]] = {}

    for name, ranking in rankings.items():
        weight = weights.get(name, 1.0)
        positions[name] = {}
        for index, memory_id in enumerate(ranking):
            rank = index + 1
            positions[name][memory_id] = rank
            scores[memory_id] = scores.get(memory_id, 0.0) + weight / (k + rank)

    fused = [
        FusedResult(
            memory_id=memory_id,
            score=score,
            lexical_rank=positions.get("lexical", {}).get(memory_id),
            semantic_rank=positions.get("semantic", {}).get(memory_id),
        )
        for memory_id, score in scores.items()
    ]
    fused.sort(key=lambda result: (-result.score, str(result.memory_id)))
    return fused
