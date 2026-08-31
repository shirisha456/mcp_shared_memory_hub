"""Choosing an embedder from configuration.

One place that maps a setting to an adapter, so nothing else has to know which
adapters exist. Returning ``None`` for ``none`` is deliberate rather than a
null-object: the absence of an embedder is a real state the search path already
handles - it falls back to full text - and inventing a do-nothing embedder would
hide that behind a fake success.
"""

from __future__ import annotations

import logging

from memhub.config import Settings
from memhub.embeddings.base import EmbeddingPort
from memhub.embeddings.fake import HashEmbedder

logger = logging.getLogger(__name__)


def build_embedder(settings: Settings) -> EmbeddingPort | None:
    """Construct the configured embedder, or ``None`` for full-text-only search.

    Construction never loads a model or touches the network - the local adapter
    defers that to first use - so this cannot be what stops a server starting.
    """
    match settings.embedding_adapter:
        case "none":
            return None

        case "fake":
            # Loud, because this produces plausible-looking results with no
            # semantic content. Silently ranking by hash would be very hard to
            # notice from the outside.
            logger.warning(
                "using the deterministic fake embedder: semantic search will "
                "return meaningless neighbours. Intended for tests only.",
                extra={"adapter": "fake"},
            )
            return HashEmbedder()

        case "local":
            from memhub.embeddings.local import LocalEmbedder

            embedder = LocalEmbedder()
            logger.info(
                "semantic search enabled",
                extra={"adapter": "local", "model": embedder.model_name},
            )
            return embedder
