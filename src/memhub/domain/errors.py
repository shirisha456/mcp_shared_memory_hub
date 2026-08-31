"""Domain errors.

Two categories, and the split drives how the MCP layer maps them (see
``memhub.mcp.mapping``):

*Caller errors* - the request was wrong. Unknown project, ambiguous reference,
content too long. These become ``is_error=True`` tool results so the model sees
them and can correct itself.

*Domain outcomes* - the request was well formed and the domain has something to
say about it. A deduplicated write, or (from Milestone 2) a revision conflict.
These are **not** errors: they are returned as ordinary structured results with
an ``outcome`` discriminator, because the model needs machine-readable data to
act on, not a sentence to parse.

Nothing in this module imports from the MCP, persistence, or service layers.
"""

from __future__ import annotations

from typing import Any


class MemhubError(Exception):
    """Base class for every error this system raises deliberately."""

    code: str = "INTERNAL"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def __str__(self) -> str:
        return self.message


class ValidationFailedError(MemhubError):
    """The caller sent a value the domain refuses.

    Raised in addition to - not instead of - the database ``CHECK`` constraints.
    The constraint is the guarantee; this is the good error message.
    """

    code = "VALIDATION_FAILED"


class ProjectNotFoundError(MemhubError):
    """No project matched the supplied reference.

    Never auto-create on a miss: a client opened in the wrong directory would
    silently fork the memory corpus in half. Creation is explicit.
    """

    code = "PROJECT_NOT_FOUND"


class AmbiguousProjectError(MemhubError):
    """The supplied hints matched more than one project.

    Resolution never guesses. The candidates are reported so the caller can pass
    an unambiguous identifier.
    """

    code = "AMBIGUOUS_PROJECT"


class ProjectAlreadyExistsError(MemhubError):
    """Slug collision on explicit creation."""

    code = "PROJECT_EXISTS"


class MemoryNotFoundError(MemhubError):
    """No such memory in this project.

    Deliberately indistinguishable from 'exists but belongs to another project'.
    Project isolation is a boundary, so a lookup outside the caller's project
    must not confirm that an id exists elsewhere.
    """

    code = "MEMORY_NOT_FOUND"


class BackendUnavailableError(MemhubError):
    """The database could not be reached.

    Nothing was written - the failure happened before any statement ran - so this
    is safe to retry unconditionally. That distinction is the whole reason this
    is a separate class from :class:`UnknownOutcomeError`, which is *not* safe to
    retry blindly.

    A model that receives an opaque internal error concludes the tool is broken
    and stops using it. One that receives this, with its retry hint, waits and
    tries again, which is the correct behaviour for a restarting database.
    """

    code = "BACKEND_UNAVAILABLE"


class BackendBusyError(MemhubError):
    """Every connection is in use and the pool timed out.

    Fail fast rather than queue. An unbounded wait converts a load spike into an
    outage: requests pile up holding memory, and by the time the backlog drains
    the callers have all given up anyway. Refusing quickly at least tells the
    caller what happened while it is still true.

    Also safe to retry - no statement ran.
    """

    code = "BACKEND_BUSY"


class UnknownOutcomeError(MemhubError):
    """The connection dropped while a statement was in flight.

    The one genuinely ambiguous failure in this system. The transaction either
    committed just before the connection died or it did not, and there is no way
    to tell from this side: the acknowledgement is exactly what was lost.

    So this does not claim the write failed. It says the outcome is unknown and
    names the two ways to resolve it - replay the idempotency key, which will
    return the stored response if the write did land (section 6.4), or re-read.
    Retrying without a key risks writing twice.
    """

    code = "UNKNOWN_OUTCOME"


class DeadlineExceededError(MemhubError):
    """A query ran past ``statement_timeout`` and PostgreSQL cancelled it.

    The timeout exists so that one pathological query cannot hold a connection
    indefinitely and starve everything else. When it fires the caller is told
    plainly, rather than being left to guess why a tool went quiet.

    ``memory_context`` does not surface this: it degrades to lexical-only
    retrieval and reports ``degraded`` in its response, because a smaller brief
    is more useful to a model than no brief.
    """

    code = "DEADLINE_EXCEEDED"
