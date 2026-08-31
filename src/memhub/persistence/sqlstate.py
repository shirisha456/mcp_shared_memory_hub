"""PostgreSQL error codes this system reacts to by name.

Kept here rather than in the MCP layer because a SQLSTATE is a fact about the
database, and both the protocol boundary and the retrieval service need to
recognise the same ones. The service layer must not import from the MCP layer,
so shared knowledge like this belongs below both.

Names, not literals, at the use sites: ``57014`` in a conditional is unreadable,
and the wrong five digits look exactly as plausible as the right ones.

Reference: https://www.postgresql.org/docs/16/errcodes-appendix.html
"""

from __future__ import annotations

QUERY_CANCELED = "57014"
"""A statement cancelled by ``statement_timeout``.

The query was too slow. The server is healthy, which is why this must not be
reported as an outage - that would send a caller into a retry loop re-running
the same slow query against a database that is working fine.
"""

ADMIN_SHUTDOWN = "57P01"
"""The backend was terminated: a restart, a failover, or ``pg_terminate_backend``."""

IN_FAILED_TRANSACTION = "25P02"
"""A statement issued after an earlier one in the same transaction failed.

Never handled, only avoided. Seeing this means code kept querying after an
error, and the fix is upstream.
"""

CONNECTION_LOST = frozenset({"08006", "08003", "08000", ADMIN_SHUTDOWN})
"""Connection failure, plus administrative shutdown.

Grouped because the caller's correct response is identical for all four: the
database is not reachable, nothing ran, retry when it is back.
"""
