"""A deterministic embedder for tests.

**This carries no semantic signal, and that is not a defect to be fixed.** It
exists so CI can exercise the *plumbing* - the outbox, the vector column, the
HNSW index, fusion, coverage reporting, retry and backoff - without downloading a
model, without a GPU, and with byte-identical results on every machine.

It cannot be used to measure retrieval quality. Two sentences meaning the same
thing get unrelated vectors, so any nDCG computed against it would measure noise.
The quality numbers in ``docs/eval/results.md`` come from the real local adapter,
and the eval harness refuses to record a baseline produced by this one.

That distinction is the whole reason both exist. A fake that *looked* semantic
would be worse than useless: it would produce plausible quality numbers that mean
nothing.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Sequence

from memhub.persistence.models import EMBEDDING_DIM


class HashEmbedder:
    """Hash text into a stable unit vector.

    The mapping is a pure function of the text, so a test that embeds the same
    memory twice gets the same vector, and a test asserting a specific cosine
    distance stays true across machines and Python versions.
    """

    def __init__(self, *, dimension: int = EMBEDDING_DIM, name: str = "fake-hash-v1") -> None:
        self._dimension = dimension
        self._name = name

    @property
    def model_name(self) -> str:
        return self._name

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        # Expand SHA-256 by hashing counter-suffixed copies until there are
        # enough bytes for the requested width, then map to floats in [-1, 1).
        raw = b""
        counter = 0
        needed = self._dimension * 4
        while len(raw) < needed:
            raw += hashlib.sha256(f"{text}#{counter}".encode()).digest()
            counter += 1

        values = [
            struct.unpack_from(">i", raw, offset * 4)[0] / 2**31
            for offset in range(self._dimension)
        ]

        # Unit-normalise, matching the contract every real adapter follows.
        magnitude = math.sqrt(sum(value * value for value in values))
        if magnitude == 0:  # pragma: no cover - needs a 384-way hash collision
            return [0.0] * self._dimension
        return [value / magnitude for value in values]
