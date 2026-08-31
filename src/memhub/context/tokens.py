"""Estimating how much of a budget a memory will cost.

**This cannot be exact, and pretending otherwise would be the mistake.**

The budget is spent in the *client's* model, and the server does not know which
model that is. Claude, GPT and Llama tokenise the same sentence into different
counts, and MCP carries no field that would tell us. Any number produced here is
an estimate.

Three ways that could be handled, and why this one:

``tiktoken``
    Precise-looking and wrong. It is OpenAI's tokeniser; using it to budget for
    Claude produces confident numbers that are systematically off, which is worse
    than an honest approximation because nobody thinks to check them.

A token-counting API
    Accurate for one vendor, and puts a network call on the read path of a
    feature whose entire purpose is to be fast. No.

**A calibrated heuristic with a stated error bound and a deliberate bias.**
Characters per token is remarkably stable across BPE tokenisers for English
prose - around 4. Using a *lower* divisor over-estimates the cost, so the builder
under-fills rather than overflows.

That asymmetry is the whole design. Exceeding the budget corrupts the caller's
context window; under-filling wastes a little of it. Those are not comparable
failures, so the estimator is tuned to fail in the cheap direction, and the
contract is stated plainly:

    **Never exceed the stated budget. May under-fill it by roughly 10%.**

The measured accuracy against a real tokeniser is in ``docs/eval/tokens.md``.
"""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable

CHARS_PER_TOKEN = 3.2
"""Below the ~4.2 that this corpus actually averages, on purpose.

Lower divisor, higher estimate, so the builder stops short of the budget rather
than overrunning it.

**Measured, and the first value was wrong.** 3.6 looked safely below the ~4.0
usually quoted for English prose - and under-estimated 2 of 33 real memories,
because technical writing full of identifiers (`FOR UPDATE SKIP LOCKED`,
`PostgreSQL`, `websearch_to_tsquery`) tokenises far more densely than prose: the
worst case measured 3.25 characters per token. A divisor above that is not
conservative, it is optimistic on exactly the content this system stores.

3.2 sits below the densest sample, so the estimate is at or above the true count
for every memory in the evaluation corpus. See ``docs/eval/tokens.md``.
"""

PER_ITEM_OVERHEAD = 12
"""Tokens each memory costs beyond its content.

A rendered item carries a type label, an importance marker, provenance and
newlines. Counting only the content would under-estimate a brief of twenty short
memories by a few hundred tokens - which is exactly the case where the budget is
tightest and the error matters most.
"""

SAFETY_MARGIN = 0.10
"""Fraction of the budget held back.

Belt and braces on top of the conservative divisor. The estimator is wrong by
some amount on every call; this is what makes "never exceed" a guarantee rather
than an expectation.
"""


@runtime_checkable
class TokenEstimator(Protocol):
    """Anything that can estimate the token cost of a string.

    A protocol rather than a function so a deployment that *does* know its
    client's tokeniser can supply the real one, and the builder immediately gets
    tighter budgets with no other change.
    """

    @property
    def name(self) -> str:
        """Reported in the budget summary, so a reader knows what produced it."""
        ...

    def estimate(self, text: str) -> int:
        """Tokens this text will cost, including per-item overhead."""
        ...


class HeuristicEstimator:
    """Character-count estimation with a conservative bias."""

    def __init__(
        self,
        *,
        chars_per_token: float = CHARS_PER_TOKEN,
        overhead: int = PER_ITEM_OVERHEAD,
    ) -> None:
        self._chars_per_token = chars_per_token
        self._overhead = overhead

    @property
    def name(self) -> str:
        return f"heuristic(chars/{self._chars_per_token})"

    def estimate(self, text: str) -> int:
        return math.ceil(len(text) / self._chars_per_token) + self._overhead


def usable_budget(budget: int, *, margin: float = SAFETY_MARGIN) -> int:
    """The budget the builder is allowed to fill, after holding back the margin.

    Floored at 1 so a caller asking for a very small budget gets *something*
    rather than an empty brief, which would look like a failure rather than a
    constraint.
    """
    return max(1, int(budget * (1.0 - margin)))
