"""Content normalisation and hashing.

The hash is computed on every write from Milestone 1, even though deduplication
itself arrives in Milestone 3. The reason is migration cost, not eagerness:
``content_hash`` is ``NOT NULL`` on an append-only table, so adding it later
would require a data migration to backfill every existing revision. Getting the
shape of the immutable content log right when we create it is cheaper than
rewriting it.

``hash_version`` is stored alongside every hash so the normaliser can change
without a big-bang re-hash: a new version is written going forward and old rows
stay valid under their own version.

The normaliser is deliberately conservative. It collapses formatting noise and
nothing else:

    "PostgreSQL is the queue"  ==  "postgresql   is the queue."
    "PostgreSQL is the queue"  !=  "We use PostgreSQL for queueing"

Anything smarter than this is semantic similarity, which is a Milestone 7
concern and must never be a silent merge (architecture doc, section 7.5).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Final

HASH_VERSION: Final[int] = 1

_WHITESPACE = re.compile(r"\s+")
_TRAILING_PUNCTUATION = re.compile(r"[.!?,;:\s]+$")


def normalize_content(content: str) -> str:
    """Reduce content to its comparison form.

    Steps, in order:

    1. NFKC Unicode normalisation, so visually identical text with different
       code-point spellings compares equal.
    2. Casefold, which handles non-ASCII case rules that ``lower()`` does not.
    3. Collapse all internal whitespace runs to a single space.
    4. Strip leading and trailing whitespace and trailing sentence punctuation.
    """
    normalized = unicodedata.normalize("NFKC", content)
    normalized = normalized.casefold()
    normalized = _WHITESPACE.sub(" ", normalized).strip()
    return _TRAILING_PUNCTUATION.sub("", normalized)


def content_hash(content: str) -> bytes:
    """SHA-256 over the normalised content.

    SHA-256 rather than a fast non-cryptographic hash: this value backs a
    uniqueness constraint from Milestone 3, and a collision there would silently
    merge two distinct project facts. 32 bytes per revision is not a cost worth
    optimising against that.
    """
    return hashlib.sha256(normalize_content(content).encode("utf-8")).digest()


def normalize_git_remote(remote: str) -> str:
    """Reduce a git remote URL to a stable comparison form.

    ``git@github.com:me/repo.git`` and ``https://github.com/me/repo`` describe
    the same repository and must resolve to the same project. Without this, the
    same developer using SSH in one client and HTTPS in another gets two
    project namespaces and a silently split corpus.
    """
    value = remote.strip()

    # scp-style SSH syntax: git@host:owner/repo
    scp = re.match(r"^(?:ssh://)?(?:[^@/]+@)?([^:/]+):(.+)$", value)
    if scp and "//" not in value:
        host, path = scp.group(1), scp.group(2)
    else:
        stripped = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", value)
        stripped = re.sub(r"^[^@/]+@", "", stripped)
        host, _, path = stripped.partition("/")

    host = host.casefold()
    path = path.strip("/").casefold()
    path = re.sub(r"\.git$", "", path)
    return f"{host}/{path}"


def normalize_workspace_path(path: str) -> str:
    """Reduce a filesystem path to a stable comparison form.

    Separators are unified and case is folded because the same workspace reaches
    us as ``C:\\src\\proj`` from one client and ``C:/src/proj`` from another.

    This is a *resolution alias*, never an identity: a path differs per machine
    and per clone. See ``memhub.services.projects``.
    """
    unified = path.strip().replace("\\", "/").rstrip("/")
    return unified.casefold()
