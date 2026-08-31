"""Project resolution and creation.

The one rule that matters: **resolution never guesses and never auto-creates.**

A client opened in the wrong directory, or a repository with two remotes, must
produce an error the caller can act on - not a silently forked memory corpus.
Splitting a project's knowledge in half is close to unrecoverable, because
nothing surfaces it: both halves look healthy, and each client only ever sees
its own.

Hints fall into two classes, and they are treated differently:

*Explicit identifiers* - ``project_id`` and ``slug``. If one is supplied and
does not resolve, that is an error. The caller named something specific and it
does not exist; quietly falling back to a repository hint would resolve to a
project they did not ask for.

*Resolution hints* - ``git_remote`` and ``workspace_path``. Best-effort. A miss
is not an error, because a known project simply may not have that alias
recorded yet.
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from memhub.domain.errors import (
    AmbiguousProjectError,
    ProjectAlreadyExistsError,
    ProjectNotFoundError,
    ValidationFailedError,
)
from memhub.domain.models import ProjectRef
from memhub.domain.normalize import normalize_git_remote, normalize_workspace_path
from memhub.domain.validation import validate_slug
from memhub.persistence.models import Project
from memhub.persistence.repositories.projects import ProjectRepository

ALIAS_GIT_REMOTE = "git_remote"
ALIAS_WORKSPACE_PATH = "workspace_path"


async def use_project(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    slug: str | None = None,
    git_remote: str | None = None,
    workspace_path: str | None = None,
    display_name: str | None = None,
    create: bool = False,
) -> ProjectRef:
    """Resolve a project from any combination of hints, optionally creating it.

    Every supplied hint is resolved independently and the results must agree.
    Resolving all of them rather than short-circuiting on the first match is
    what turns a silent mis-resolution into a loud one: if a caller sends a slug
    and a git remote pointing at different projects, their configuration is
    wrong and they need to hear about it.
    """
    repo = ProjectRepository(session)

    if not any((project_id, slug, git_remote, workspace_path)):
        raise ValidationFailedError(
            "Supply at least one of project_id, slug, git_remote or workspace_path."
        )

    normalized_slug = validate_slug(slug) if slug else None
    remote_norm = normalize_git_remote(git_remote) if git_remote else None
    path_norm = normalize_workspace_path(workspace_path) if workspace_path else None

    candidates: dict[uuid.UUID, list[str]] = {}

    def note(found: uuid.UUID | None, source: str) -> None:
        if found is not None:
            candidates.setdefault(found, []).append(source)

    # --- explicit identifiers: a miss is an error unless we are creating ---
    if project_id is not None:
        by_id = await repo.get_by_id(project_id)
        if by_id is None:
            raise ProjectNotFoundError(
                f"No project with id {project_id}.", project_id=str(project_id)
            )
        note(by_id.id, "project_id")

    by_slug: Project | None = None
    if normalized_slug is not None:
        by_slug = await repo.get_by_slug(normalized_slug)
        if by_slug is None and not create:
            raise ProjectNotFoundError(
                f"No project with slug {normalized_slug!r}. Projects are never "
                "created implicitly - a client opened in the wrong directory "
                "would otherwise silently start a second, empty memory "
                "namespace. Retry with create=true once you are sure this is a "
                "new project.",
                slug=normalized_slug,
            )
        note(by_slug.id if by_slug else None, "slug")

    # --- resolution hints: a miss is fine ---
    if remote_norm is not None:
        note(await repo.resolve_alias(ALIAS_GIT_REMOTE, remote_norm), "git_remote")
    if path_norm is not None:
        note(await repo.resolve_alias(ALIAS_WORKSPACE_PATH, path_norm), "workspace_path")

    if len(candidates) > 1:
        detail = ", ".join(
            f"{pid} (matched by {'+'.join(sources)})" for pid, sources in candidates.items()
        )
        raise AmbiguousProjectError(
            f"The supplied hints match {len(candidates)} different projects: {detail}. "
            "Pass an unambiguous project_id or slug.",
            candidates=[str(pid) for pid in candidates],
        )

    # A named slug that does not exist, while a hint points at some *other*
    # project, is a disagreement - not a match. Returning the hint's project here
    # would hand the caller a namespace they did not ask for and silently write
    # another project's memories into it. In practice this is the
    # "two projects claim the same git remote" case.
    if normalized_slug is not None and by_slug is None and candidates:
        owner = next(iter(candidates))
        raise AmbiguousProjectError(
            f"No project has slug {normalized_slug!r}, but the supplied "
            f"git_remote or workspace_path already belongs to project {owner}. "
            "An alias can only belong to one project. Either use that project, "
            "or create this one without the conflicting alias.",
            slug=normalized_slug,
            candidates=[str(owner)],
        )

    if candidates:
        resolved_id = next(iter(candidates))
        project = await repo.get_by_id(resolved_id)
        if project is None:  # pragma: no cover - resolved microseconds ago
            raise ProjectNotFoundError(f"No project with id {resolved_id}.")
        await _ensure_aliases(repo, project.id, remote_norm, path_norm)
        return ProjectRef(
            id=project.id, slug=project.slug, display_name=project.display_name, created=False
        )

    if not create:
        raise ProjectNotFoundError(
            "No project matched the supplied hints. Projects are never created "
            "implicitly. Retry with create=true and an explicit slug once you "
            "are sure this is a new project."
        )

    if normalized_slug is None:
        raise ValidationFailedError(
            "Creating a project requires an explicit slug. A slug derived from a "
            "path or remote would differ between machines and clients, which is "
            "exactly the mis-resolution this design avoids."
        )

    try:
        created = await repo.create(
            slug=normalized_slug, display_name=display_name or normalized_slug
        )
        await _ensure_aliases(repo, created.id, remote_norm, path_norm)
    except IntegrityError as exc:
        raise ProjectAlreadyExistsError(
            f"A project or alias conflicting with slug {normalized_slug!r} already "
            "exists. An alias can only belong to one project.",
            slug=normalized_slug,
        ) from exc

    return ProjectRef(
        id=created.id, slug=created.slug, display_name=created.display_name, created=True
    )


async def _ensure_aliases(
    repo: ProjectRepository,
    project_id: uuid.UUID,
    remote_norm: str | None,
    path_norm: str | None,
) -> None:
    """Record newly seen aliases against a resolved project.

    Only adds what is missing. Because ``(kind, value_norm)`` is globally
    unique, an alias already owned by a different project raises rather than
    silently re-pointing - and that is the behaviour we want, since it means two
    projects are claiming the same repository.
    """
    for kind, value in ((ALIAS_GIT_REMOTE, remote_norm), (ALIAS_WORKSPACE_PATH, path_norm)):
        if value is None:
            continue
        if await repo.resolve_alias(kind, value) is None:
            await repo.add_alias(project_id=project_id, kind=kind, value_norm=value)
