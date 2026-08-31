"""Local embedding via fastembed.

Local rather than hosted, deliberately. A hosted embedding API would make the
demo depend on a network and a key, would send every memory to a third party -
which is a strange property for a system whose whole job is holding a
developer's private project knowledge - and would put an external outage on the
path to semantic search.

fastembed rather than sentence-transformers because it runs ONNX on CPU: a ~50MB
model instead of a multi-gigabyte torch install. Still an optional extra, since a
base install of this server has no reason to carry an inference runtime and CI
uses the deterministic fake.

    pip install -e ".[local-embeddings]"

The first call downloads the model. That is slow and needs a network, so it
happens lazily rather than at import: constructing the adapter must not be what
blocks a server from starting.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from memhub.embeddings.base import EmbeddingError

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIMENSION = 384


class LocalEmbedder:
    """Wraps a fastembed model behind the port.

    ``model_name`` includes the underlying model identifier verbatim, so vectors
    written under one model can never be compared against another: the stored
    name would differ and the query filter would exclude them.
    """

    def __init__(self, *, model: str = DEFAULT_MODEL, dimension: int = DEFAULT_DIMENSION) -> None:
        self._model = model
        self._dimension = dimension
        self._embedder: Any | None = None

    @property
    def model_name(self) -> str:
        return f"fastembed:{self._model}"

    @property
    def dimension(self) -> int:
        return self._dimension

    def _load(self) -> Any:
        """Load on first use, not at construction.

        Importing fastembed pulls in an ONNX runtime and the first call may
        download weights. Doing that during ``__init__`` would mean a server
        cannot start without a network, for a feature that is supposed to
        degrade gracefully when unavailable.
        """
        if self._embedder is not None:
            return self._embedder

        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise EmbeddingError(
                "fastembed is not installed. Install the optional extra with "
                'pip install -e ".[local-embeddings]", or configure the '
                "deterministic fake adapter for tests."
            ) from exc

        try:
            self._embedder = TextEmbedding(model_name=self._model)
        except Exception as exc:  # pragma: no cover - network or disk failure
            raise EmbeddingError(f"could not load {self._model}: {exc}") from exc
        return self._embedder

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        embedder = self._load()
        try:
            vectors = [vector.tolist() for vector in embedder.embed(list(texts))]
        except Exception as exc:  # pragma: no cover - inference failure
            raise EmbeddingError(f"embedding failed: {exc}") from exc

        for vector in vectors:
            if len(vector) != self._dimension:
                raise EmbeddingError(
                    f"{self.model_name} produced {len(vector)} dimensions, but the "
                    f"embedding column is {self._dimension} wide. Changing model "
                    "requires a migration and a full re-embed - vectors from "
                    "different models are not comparable, so there is no "
                    "incremental path."
                )
        return vectors
