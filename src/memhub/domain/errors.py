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
