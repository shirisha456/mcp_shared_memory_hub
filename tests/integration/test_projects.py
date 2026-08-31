"""Project resolution.

The behaviour under test is mostly about what the system *refuses* to do.
Silently resolving to the wrong project, or silently creating a second one, is
the failure mode that splits a corpus in half - and it is invisible afterwards,
because both halves look healthy and each client only sees its own.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from memhub.domain.errors import (
    AmbiguousProjectError,
    ProjectNotFoundError,
    ValidationFailedError,
)
from memhub.services.projects import use_project

pytestmark = pytest.mark.integration


async def test_create_then_resolve_by_slug(db_session: AsyncSession) -> None:
    created = await use_project(db_session, slug="memhub", create=True)
    assert created.created is True

    resolved = await use_project(db_session, slug="memhub")
    assert resolved.created is False
    assert resolved.id == created.id


async def test_resolution_never_creates(db_session: AsyncSession) -> None:
    """The core anti-fork rule."""
    with pytest.raises(ProjectNotFoundError, match="never created implicitly"):
        await use_project(db_session, slug="does-not-exist")


async def test_unknown_workspace_path_does_not_create(db_session: AsyncSession) -> None:
    with pytest.raises(ProjectNotFoundError):
        await use_project(db_session, workspace_path="C:/some/random/dir")


async def test_create_requires_an_explicit_slug(db_session: AsyncSession) -> None:
    """A slug derived from a path would differ per machine, which is the
    mis-resolution the design exists to avoid."""
    with pytest.raises(ValidationFailedError, match="requires an explicit slug"):
        await use_project(db_session, workspace_path="C:/src/thing", create=True)


async def test_ssh_and_https_remotes_resolve_to_one_project(db_session: AsyncSession) -> None:
    """The most likely real cause of a split corpus: same repo, two URL forms."""
    created = await use_project(
        db_session,
        slug="memhub",
        git_remote="git@github.com:me/memhub.git",
        create=True,
    )
    resolved = await use_project(db_session, git_remote="https://github.com/me/memhub")
    assert resolved.id == created.id


async def test_alias_is_recorded_on_later_resolution(db_session: AsyncSession) -> None:
    """A project learns aliases as clients present them."""
    created = await use_project(db_session, slug="memhub", create=True)
    await use_project(db_session, slug="memhub", workspace_path="C:/src/memhub")

    by_path = await use_project(db_session, workspace_path="C:\\src\\memhub")
    assert by_path.id == created.id


async def test_conflicting_hints_raise_rather_than_guess(db_session: AsyncSession) -> None:
    """Two hints, two projects. Never pick one."""
    first = await use_project(
        db_session, slug="proj-a", git_remote="git@github.com:me/a.git", create=True
    )
    second = await use_project(db_session, slug="proj-b", create=True)
    assert first.id != second.id

    with pytest.raises(AmbiguousProjectError) as exc:
        await use_project(db_session, slug="proj-b", git_remote="git@github.com:me/a.git")

    message = str(exc.value)
    assert "2 different projects" in message
    assert str(first.id) in message and str(second.id) in message


async def test_an_alias_cannot_belong_to_two_projects(db_session: AsyncSession) -> None:
    """A new slug plus an alias owned by another project is a disagreement.

    Resolving to the alias owner would hand the caller a namespace they did not
    ask for, and every memory they then wrote would land in the wrong project.
    """
    owner = await use_project(
        db_session, slug="proj-a", git_remote="git@github.com:me/x.git", create=True
    )

    with pytest.raises(AmbiguousProjectError) as exc:
        await use_project(
            db_session, slug="proj-b", git_remote="git@github.com:me/x.git", create=True
        )
    assert str(owner.id) in str(exc.value)


async def test_create_on_an_existing_slug_is_idempotent(db_session: AsyncSession) -> None:
    """project_use is naturally idempotent, which is why it needs no
    idempotency key: the unique slug already makes a retry a no-op."""
    first = await use_project(db_session, slug="memhub", create=True)
    second = await use_project(db_session, slug="memhub", create=True)

    assert second.id == first.id
    assert first.created is True
    assert second.created is False


async def test_unknown_project_id_is_an_error_even_with_other_hints(
    db_session: AsyncSession,
) -> None:
    """An explicit identifier that does not exist must not fall back to a hint."""
    await use_project(db_session, slug="memhub", create=True)
    with pytest.raises(ProjectNotFoundError):
        await use_project(db_session, project_id=uuid.uuid4(), slug="memhub")


async def test_requires_at_least_one_hint(db_session: AsyncSession) -> None:
    with pytest.raises(ValidationFailedError, match="at least one"):
        await use_project(db_session)


async def test_display_name_defaults_to_slug(db_session: AsyncSession) -> None:
    ref = await use_project(db_session, slug="memhub", create=True)
    assert ref.display_name == "memhub"

    named = await use_project(db_session, slug="other", display_name="Other Thing", create=True)
    assert named.display_name == "Other Thing"
