"""Normalisation and hashing.

These are pure functions, but they are load-bearing: from Milestone 3 the
content hash backs a uniqueness constraint, so a normaliser that is too
aggressive silently merges two distinct project facts, and one that is too timid
lets duplicates through. The boundary is worth pinning down with tests before
anything depends on it.
"""

from __future__ import annotations

import pytest

from memhub.domain.normalize import (
    HASH_VERSION,
    content_hash,
    normalize_content,
    normalize_git_remote,
    normalize_workspace_path,
)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("PostgreSQL is the queue", "postgresql is the queue"),
        ("PostgreSQL is the queue", "PostgreSQL is the queue."),
        ("PostgreSQL is the queue", "  PostgreSQL   is  the queue  "),
        ("PostgreSQL is the queue", "PostgreSQL is the queue!"),
        ("PostgreSQL is the queue", "PostgreSQL\tis\nthe queue"),
    ],
)
def test_formatting_noise_collapses(left: str, right: str) -> None:
    assert content_hash(left) == content_hash(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("PostgreSQL is the queue", "We use PostgreSQL for queueing"),
        ("Redis is the queue", "Redis is not the queue"),
        ("Use Postgres for tasks", "Use Postgres for events"),
    ],
)
def test_different_statements_stay_different(left: str, right: str) -> None:
    """The normaliser must never merge on meaning.

    Anything smarter than formatting collapse is semantic similarity, and a
    silent semantic merge destroys a real distinction. Similarity surfaces
    candidates for the model to judge (Milestone 7); it never merges.
    """
    assert content_hash(left) != content_hash(right)


def test_unicode_normalisation() -> None:
    # Composed vs decomposed forms of the same visible text.
    assert normalize_content("caf\u00e9") == normalize_content("cafe\u0301")


def test_hash_is_pinned() -> None:
    """A frozen digest.

    From Milestone 3 this hash is a primary key. If the normaliser changes,
    previously stored hashes stop matching newly computed ones and deduplication
    silently stops working - so a change must be a deliberate HASH_VERSION bump,
    not an accident. This test is what makes that accidental change impossible.
    """
    assert (
        content_hash("PostgreSQL is the queue.").hex()
        == "63d4fbfa57347cf18b6d5e7e17156896e7ba929f6159b29715a12bbb4b01a61b"
    )
    assert HASH_VERSION == 1


@pytest.mark.parametrize(
    "remote",
    [
        "git@github.com:me/repo.git",
        "https://github.com/me/repo",
        "https://github.com/me/repo.git",
        "ssh://git@github.com/me/repo.git",
        "https://GitHub.com/Me/Repo/",
    ],
)
def test_git_remote_forms_converge(remote: str) -> None:
    """SSH in one client and HTTPS in another must not fork the project.

    This is the most likely real-world cause of a split corpus: the same
    developer, the same repository, two clients configured differently.
    """
    assert normalize_git_remote(remote) == "github.com/me/repo"


def test_different_repos_stay_distinct() -> None:
    assert normalize_git_remote("git@github.com:me/a.git") != normalize_git_remote(
        "git@github.com:me/b.git"
    )
    assert normalize_git_remote("git@github.com:me/repo.git") != normalize_git_remote(
        "git@gitlab.com:me/repo.git"
    )


@pytest.mark.parametrize(
    "path",
    ["C:\\src\\proj", "C:/src/proj", "C:/src/proj/", "c:/SRC/Proj"],
)
def test_workspace_path_forms_converge(path: str) -> None:
    assert normalize_workspace_path(path) == "c:/src/proj"
