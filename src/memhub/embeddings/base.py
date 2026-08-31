"""The embedding boundary.

One narrow protocol, so the storage layer never learns which model produced a
vector. Swapping the adapter is then a configuration change rather than a
rewrite, and the eval harness can measure two adapters against the same corpus.

**The model name is part of the contract, not decoration.** Vectors from
different models occupy different spaces and their cosine similarities are not
comparable, so every stored vector records the model that produced it and every
query filters on it. Mixing them would not raise an error - it would silently
return nonsense rankings, which is far worse.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


class EmbeddingError(Exception):
    """The adapter could not produce a vector.

    Never fatal to a write. The outbox exists precisely so that a failing
    embedder degrades semantic search rather than stopping the system from
    recording anything.
    """


@runtime_checkable
class EmbeddingPort(Protocol):
    """What the rest of the system needs from an embedding model."""

    @property
    def model_name(self) -> str:
        """Stored with every vector and used to filter queries.

        Must change whenever the produced vectors change, including a version
        bump of the same model - otherwise old and new vectors would be compared
        as though they shared a space.
        """
        ...

    @property
    def dimension(self) -> int:
        """Must equal the width of the ``embedding`` column, or writes fail."""
        ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch, returning one unit-normalised vector per input.

        Batched because inference has fixed per-call overhead that dominates for
        single items, and the outbox worker always has a batch to hand.

        Unit-normalised so cosine distance is the only thing the index has to
        compute. An adapter returning unnormalised vectors would still "work" and
        would quietly rank by magnitude as much as by direction.

        Raises :class:`EmbeddingError` on failure. The caller records that and
        retries later; it never propagates to a write.
        """
        ...
