"""Declarative base and the constraint naming convention.

The naming convention is not cosmetic. Milestone 0 ships a drift test that runs
``alembic check`` and requires an empty diff between ORM metadata and the
migrated schema. Without a deterministic naming convention, PostgreSQL invents
names for unnamed constraints, Alembic autogenerate cannot match them against
the metadata, and the drift test produces phantom differences forever.

It also makes the invariants in the architecture document (section 13)
greppable: ``uq_memory_revisions_memory_id`` says what it enforces.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for every ORM model.

    No tables are mapped yet - the first ones arrive in Milestone 1. The
    metadata object exists now so Alembic's ``target_metadata`` is wired and
    the drift test is already in place when they do.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
