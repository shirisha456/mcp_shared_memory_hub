"""baseline

Revision ID: 0001
Revises:
Create Date: Milestone 0

Deliberately empty.

Milestone 0's job is to prove the *migration pipeline*: that Alembic is wired to
the async engine, that ``upgrade``/``downgrade`` round-trip, that the
``alembic_version`` table is created in a fresh database, and that the drift
check compares ORM metadata against the migrated schema.

The first real DDL - ``projects``, ``memories``, ``memory_revisions`` - lands in
Milestone 1. Creating tables here would pull domain modelling into the milestone
that is supposed to contain none, and would leave the schema half-built if
Milestone 1's design shifted.

Consequence, stated plainly: the downgrade and drift tests pass trivially today.
They exist now so they are already wired when there is something to check.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
