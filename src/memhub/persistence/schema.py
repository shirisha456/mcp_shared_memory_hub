"""Refusing to run against a schema this code does not expect.

A server started against a database that has not been migrated does not fail
cleanly. It fails on the first query that touches a missing column, as an
opaque ``ProgrammingError`` inside a tool call, and the client reports it as the
tool being broken. Nobody looks at migrations.

Worse is the other direction. A *newer* database, migrated by a colleague or by a
deploy that has already rolled forward, mostly works - until this process writes
a row that the new schema's constraints were designed to prevent, or reads a
column whose meaning changed. The failure surfaces later, somewhere else, as
corrupt data rather than an error.

So the check runs once at startup and refuses in both directions. Failing to
start is loud, immediate, and names the fix; the alternatives are quiet and
arrive later.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

REPO_ROOT = Path(__file__).resolve().parents[3]


class SchemaMismatchError(RuntimeError):
    """The database schema is not the one this code was written against."""


def expected_revision() -> str:
    """The migration head this build expects."""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:  # pragma: no cover - would mean no migrations at all
        raise SchemaMismatchError("no migrations found")
    return head


async def applied_revision(engine: AsyncEngine) -> str | None:
    """What the database says it is at, or ``None`` if never migrated."""
    async with engine.connect() as conn:
        exists = (
            await conn.execute(text("SELECT to_regclass('public.alembic_version')"))
        ).scalar_one()
        if exists is None:
            return None
        return (
            await conn.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one_or_none()


async def verify(engine: AsyncEngine) -> str:
    """Check the database matches this build, or raise with the remedy.

    Returns the revision on success so a caller can log what it verified against.
    """
    expected = expected_revision()
    applied = await applied_revision(engine)

    if applied is None:
        raise SchemaMismatchError(
            "the database has no schema. Run:\n\n    alembic upgrade head\n\n"
            f"This build expects revision {expected}."
        )

    if applied == expected:
        return expected

    known = _known_revisions()
    if applied in known and known.index(applied) < known.index(expected):
        raise SchemaMismatchError(
            f"the database is at {applied} but this build expects {expected}. Run:\n\n"
            "    alembic upgrade head\n\n"
            "Starting anyway would fail on the first query touching a column that "
            "does not exist yet, reported to the client as a broken tool."
        )

    raise SchemaMismatchError(
        f"the database is at {applied}, which this build ({expected}) does not "
        "know about - it has been migrated by newer code. Upgrade this "
        "installation rather than downgrading the database.\n\n"
        "Running against a newer schema is the more dangerous direction: it "
        "mostly works, until this process writes a row that newer constraints "
        "were added to prevent."
    )


def _known_revisions() -> list[str]:
    """Revisions oldest-first, so 'behind' and 'ahead' can be told apart."""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    return [revision.revision for revision in reversed(list(scripts.walk_revisions()))]
