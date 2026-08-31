"""Project persistence.

Resolution is deliberately dumb: each method answers one question about one
hint. Deciding what to do when hints disagree is policy, and policy lives in
``memhub.services.projects``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from memhub.persistence.models import Project, ProjectAlias


class ProjectRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, project_id: uuid.UUID) -> Project | None:
        return await self._session.get(Project, project_id)

    async def get_by_slug(self, slug: str) -> Project | None:
        stmt = select(Project).where(Project.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def resolve_alias(self, kind: str, value_norm: str) -> uuid.UUID | None:
        """Map an alias to a project.

        Returns at most one id by construction: ``uq_project_aliases_kind_value_norm``
        is global rather than per-project, so an alias value cannot belong to two
        projects.
        """
        stmt = select(ProjectAlias.project_id).where(
            ProjectAlias.kind == kind, ProjectAlias.value_norm == value_norm
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def create(self, *, slug: str, display_name: str) -> Project:
        project = Project(slug=slug, display_name=display_name)
        self._session.add(project)
        # Flush rather than commit: the caller owns the transaction, and the
        # aliases inserted next must land atomically with the project.
        await self._session.flush()
        await self._session.refresh(project)
        return project

    async def add_alias(self, *, project_id: uuid.UUID, kind: str, value_norm: str) -> None:
        self._session.add(ProjectAlias(project_id=project_id, kind=kind, value_norm=value_norm))
        await self._session.flush()

    async def list_all(self, *, limit: int = 100) -> list[Project]:
        stmt = select(Project).order_by(Project.slug).limit(limit)
        return list((await self._session.execute(stmt)).scalars())
