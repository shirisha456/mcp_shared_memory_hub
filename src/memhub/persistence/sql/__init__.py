"""Hand-written SQL for the concurrency-critical statements.

Almost all data access in this project goes through the ORM. These few
statements do not, and the exception is deliberate: their correctness depends on
exact PostgreSQL semantics - ``EvalPlanQual`` re-checks under READ COMMITTED,
``ON CONFLICT DO NOTHING`` waiting on an uncommitted insert, ``FOR SHARE``
blocking until a transaction resolves. Expressing them through a query builder
would hide the very thing a reviewer needs to read.

They live as ``.sql`` files rather than Python string constants for the same
reason: so they can be read, with their comments, as SQL.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from sqlalchemy import TextClause, text

_SQL_DIR = Path(__file__).parent


@cache
def load(name: str) -> TextClause:
    """Load a named statement, cached after first read.

    Fails loudly at first use if the file is missing from an installed package,
    which is the failure mode of shipping ``.sql`` alongside Python.
    """
    path = _SQL_DIR / f"{name}.sql"
    if not path.is_file():
        raise FileNotFoundError(
            f"SQL statement {name!r} not found at {path}. If this happens in an "
            "installed package rather than a source checkout, the build is not "
            "including .sql files."
        )
    return text(path.read_text(encoding="utf-8"))


CAS_REVISE = "cas_revise"
CAS_SUPERSEDE = "cas_supersede"
CAS_FORGET = "cas_forget"
CLAIM_DEDUP_KEY = "claim_dedup_key"
CLAIM_IDEMPOTENCY = "claim_idempotency"
WAIT_IDEMPOTENCY = "wait_idempotency"
