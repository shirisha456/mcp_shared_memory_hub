"""Selecting memories to fit a token budget.

Search answers "what matches this query". This answers a harder question: *given
this much room, what is the most useful thing to say?* Those come apart. The ten
most relevant memories might be five restatements of one decision plus five
details of a task that finished last month, and a brief made of those is worse
than a shorter one that covered four different things.

So this is a constrained selection problem, and it is built as one:

1. **Candidates** - over-fetch from retrieval, which has already applied the
   stage-0 filter. Nothing retired can reach this stage.
2. **Hard filters** - drop anything that cannot fit even alone.
3. **Quotas** - divide the budget by memory type, so a flood of one kind cannot
   crowd out the others. Unspent share is redistributed.
4. **Diversity** - MMR, so near-duplicates do not consume the budget three times
   over.
5. **Fill** - greedy by score per token. Knapsack; greedy on the ratio is within
   a known bound and is what the size of this problem warrants.
6. **Order** - a total order, so identical inputs give byte-identical output.

That last step is not cosmetic. A brief whose contents shuffle between identical
calls cannot be snapshot-tested and cannot be safely cached, and both of those
matter more than any marginal ranking gain.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from memhub.context.tokens import TokenEstimator, usable_budget
from memhub.domain.enums import MemoryType
from memhub.domain.models import MemoryView

TYPE_QUOTA: dict[MemoryType, float] = {
    # Violating a constraint breaks the project, so it gets a guaranteed share
    # even though constraints are usually the smallest group.
    MemoryType.CONSTRAINT: 0.25,
    # The largest share: a decision plus its rejected alternative is the most
    # useful thing this system holds.
    MemoryType.DECISION: 0.40,
    MemoryType.FACT: 0.20,
    # Smallest, and expires fastest. Useful for "where did I leave off", not
    # worth much of a brief.
    MemoryType.TASK: 0.15,
}

MMR_LAMBDA = 0.7
"""Balance between relevance and novelty.

At 1.0 this is pure greedy relevance and near-duplicates fill the brief. At 0.0
it selects for difference alone and returns unrelated trivia. 0.7 leans towards
relevance while still refusing a memory that says what one already selected says.
"""

DUPLICATE_SIMILARITY = 0.6
"""Above this, treat two memories as saying the same thing.

Similarity is Jaccard overlap of content words - deliberately not embeddings.
Diversity here is about *redundant phrasing*, which word overlap captures well,
and making the context builder depend on the embedding pipeline would mean a
brief could not be produced while the outbox was behind.
"""

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    """a an and are as at be by for from has have in is it its of on or that the
    to was were will with this these those not no than then""".split()
)


@dataclass(frozen=True, slots=True)
class Candidate:
    memory: MemoryView
    score: float
    tokens: int

    @property
    def value_density(self) -> float:
        """Score per token - what greedy knapsack fill orders by.

        A long memory has to earn its space. Ranking by score alone would let one
        verbose decision consume a third of the budget for the same value as
        three concise ones.
        """
        return self.score / self.tokens if self.tokens else 0.0


@dataclass(frozen=True, slots=True)
class Selection:
    selected: tuple[Candidate, ...]
    tokens_used: int
    budget: int
    considered: int
    dropped: dict[str, int] = field(default_factory=dict)

    @property
    def utilisation(self) -> float:
        return self.tokens_used / self.budget if self.budget else 0.0


def content_words(text: str) -> frozenset[str]:
    """Content words, for the similarity comparison."""
    return frozenset(w for w in _WORD.findall(text.casefold()) if w not in _STOPWORDS)


def similarity(left: str, right: str) -> float:
    """Jaccard overlap of content words, in [0, 1]."""
    a, b = content_words(left), content_words(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def select(
    candidates: Sequence[Candidate],
    *,
    budget: int,
    estimator: TokenEstimator,
    quotas: dict[MemoryType, float] | None = None,
) -> Selection:
    """Choose the most useful subset that fits.

    ``estimator`` is taken for reporting rather than recomputation - candidates
    arrive with their token cost already measured, and re-estimating here would
    let the two disagree.
    """
    quotas = quotas or TYPE_QUOTA
    spendable = usable_budget(budget)
    dropped: dict[str, int] = {}

    def drop(reason: str, count: int = 1) -> None:
        if count:
            dropped[reason] = dropped.get(reason, 0) + count

    # A memory that cannot fit alone will never fit. Removing it here keeps the
    # fill loop from reconsidering it at every step.
    fitting = [c for c in candidates if c.tokens <= spendable]
    drop("too_large_alone", len(candidates) - len(fitting))

    # Quota pass. Each type gets its share, and what it does not use is released.
    #
    # `redundant` is carried across passes rather than recomputed. A candidate
    # rejected as a near-duplicate during its quota must not be reconsidered
    # during redistribution: it would be rejected again for the same reason, and
    # the drop counts would report more rejections than there were candidates.
    selected: list[Candidate] = []
    redundant: set[uuid.UUID] = set()
    used = 0

    for memory_type, share in sorted(quotas.items(), key=lambda kv: -kv[1]):
        pool = [c for c in fitting if c.memory.type is memory_type]
        allowance = int(spendable * share)
        taken, spent, rejected = _fill(pool, allowance, already=selected)
        selected.extend(taken)
        redundant |= rejected
        used += spent

    # Redistribution. A project with no open tasks should spend that 15% on
    # decisions rather than leave it unused, so a second pass offers the whole
    # remaining budget to everything neither chosen nor already ruled out.
    chosen = {c.memory.memory_id for c in selected}
    remaining = spendable - used
    if remaining > 0:
        leftovers = [
            c
            for c in fitting
            if c.memory.memory_id not in chosen and c.memory.memory_id not in redundant
        ]
        taken, spent, rejected = _fill(leftovers, remaining, already=selected)
        selected.extend(taken)
        redundant |= rejected
        used += spent

    drop("too_similar", len(redundant))
    drop("no_budget_left", len(fitting) - len(selected) - len(redundant))

    return Selection(
        selected=tuple(_stable_order(selected)),
        tokens_used=used,
        budget=budget,
        considered=len(candidates),
        dropped={reason: count for reason, count in dropped.items() if count > 0},
    )


def _fill(
    pool: Sequence[Candidate],
    allowance: int,
    *,
    already: Sequence[Candidate] = (),
) -> tuple[list[Candidate], int, set[uuid.UUID]]:
    """Greedy fill of one allowance, with MMR diversity.

    Returns what was taken, what it cost, *which* candidates were rejected as
    near-duplicates, and nothing else. Returning the ids rather than a count is
    what lets the caller avoid reconsidering them in a later pass - and stops the
    drop tally counting the same rejection twice.
    """
    taken: list[Candidate] = []
    chosen = {c.memory.memory_id for c in already}
    chosen_text = [c.memory.content for c in already]
    rejected: set[uuid.UUID] = set()
    spent = 0

    remaining = [c for c in pool if c.memory.memory_id not in chosen]
    remaining.sort(key=lambda c: (-c.value_density, str(c.memory.memory_id)))
    while remaining:
        best: Candidate | None = None
        best_value = float("-inf")

        for candidate in remaining:
            if spent + candidate.tokens > allowance:
                continue
            novelty = 1.0 - max(
                (similarity(candidate.memory.content, text) for text in chosen_text),
                default=0.0,
            )
            value = MMR_LAMBDA * candidate.value_density + (1 - MMR_LAMBDA) * novelty * (
                candidate.value_density or 1e-9
            )
            if value > best_value:
                best, best_value = candidate, value

        if best is None:
            break

        remaining.remove(best)
        redundant = any(
            similarity(best.memory.content, text) >= DUPLICATE_SIMILARITY for text in chosen_text
        )
        if redundant:
            rejected.add(best.memory.memory_id)
            continue

        taken.append(best)
        chosen_text.append(best.memory.content)
        spent += best.tokens

    return taken, spent, rejected


def _stable_order(selected: Sequence[Candidate]) -> list[Candidate]:
    """Order the brief for a reader, deterministically.

    Constraints first, because they bound everything else; then decisions, facts,
    and current work. Within a type, by score. The trailing id is the tiebreak
    that makes identical inputs produce identical output - without it, two
    memories with equal scores come back in whatever order the fill happened to
    produce.
    """
    order = {
        MemoryType.CONSTRAINT: 0,
        MemoryType.DECISION: 1,
        MemoryType.FACT: 2,
        MemoryType.TASK: 3,
    }
    return sorted(
        selected,
        key=lambda c: (order.get(c.memory.type, 9), -c.score, str(c.memory.memory_id)),
    )
