"""Domain errors to MCP results.

The distinction this module encodes, from architecture section 3.4(a):

*Protocol errors* (JSON-RPC) mean the request was malformed or the server broke.
The SDK raises these for schema violations before our code runs.

*Tool errors* (``is_error=True``) mean the request was well formed but the
domain refused it. The model sees the message and can correct itself. Unknown
project, invalid content, ambiguous reference.

*Domain outcomes* are neither. A deduplicated write, or a Milestone 2 revision
conflict, is a legitimate result of a legitimate request. Those are returned as
ordinary structured results carrying an ``outcome`` discriminator.

*Infrastructure failures* are a fourth case, added in Milestone 9. They are tool
errors too, but they are not the caller's fault and the caller can often recover
from them, so they carry codes that distinguish safe-to-retry from
not-safe-to-retry rather than a single opaque failure.

**Amendment, Milestone 1.** Section 3.4(a) originally specified that a revision
conflict would be ``is_error=True`` *with a structured payload*. The v2 SDK's
``ToolError`` carries a message string only - ``is_error`` and
``structured_content`` are mutually exclusive in practice. So conflicts will be
returned as normal results with ``outcome="conflict"`` plus the current revision
and content. This is arguably better: it forces the discriminator to be part of
the success schema, which means every caller has to acknowledge that a write can
resolve in more than one way. Recorded here because it changes a documented
decision.
"""

from __future__ import annotations

import functools
import logging
import socket
from collections.abc import Awaitable, Callable

from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from memhub.domain.errors import (
    BackendBusyError,
    BackendUnavailableError,
    DeadlineExceededError,
    MemhubError,
    UnknownOutcomeError,
)
from memhub.persistence.sqlstate import CONNECTION_LOST, QUERY_CANCELED

logger = logging.getLogger(__name__)


def as_tool_error(exc: MemhubError) -> ToolError:
    """Render a domain error as the message the model will read.

    The error code is included because it is stable across wording changes - a
    client can branch on ``PROJECT_NOT_FOUND`` without string-matching prose.
    """
    return ToolError(f"[{exc.code}] {exc.message}")


def classify_infrastructure_error(exc: Exception) -> MemhubError | None:
    """Turn a driver-level failure into something a caller can act on.

    Without this the model sees a bare ``OperationalError``, concludes the tool
    is broken, and stops calling it. The three outcomes below are distinguished
    because the correct client response differs for each, and only one of them
    is safe to retry blindly.

    Returns ``None`` for anything unrecognised, which then propagates as a bug -
    guessing at an unfamiliar failure would produce confident wrong advice.
    """
    if isinstance(exc, PoolTimeoutError):
        return BackendBusyError(
            "every database connection is in use and the request timed out "
            "waiting for one. Nothing was written; retry shortly.",
            retryable=True,
        )

    if isinstance(exc, ConnectionError | socket.gaierror):
        # asyncpg raises the socket error unwrapped when the connection is
        # refused outright - there is no DBAPI cursor yet for SQLAlchemy to
        # attach it to. Nothing ran, so this is the safe-to-retry case.
        return BackendUnavailableError(
            "the database is not reachable. Nothing was written; this is safe "
            "to retry once the backend is available.",
            retryable=True,
        )

    if not isinstance(exc, DBAPIError):
        return None

    pgcode = getattr(getattr(exc, "orig", None), "sqlstate", None)

    if pgcode == QUERY_CANCELED:
        return DeadlineExceededError(
            "the query exceeded the server statement timeout and was cancelled. "
            "Narrow the search or request fewer results.",
            retryable=False,
        )

    # The load-bearing distinction. ``connection_invalidated`` is set by
    # SQLAlchemy when a connection that had already been established died; a
    # connection that never opened at all leaves it False. That is exactly the
    # line between "nothing ran" and "something may have run", and it is the
    # difference between safe to retry and not.
    if exc.connection_invalidated:
        return UnknownOutcomeError(
            "the connection to the database was lost while the request was in "
            "flight. The write may or may not have committed. Replay the same "
            "request with its idempotency key to find out, or re-read before "
            "retrying - retrying without a key risks writing twice.",
            retryable=False,
        )

    if pgcode in CONNECTION_LOST or isinstance(exc.orig, ConnectionError | socket.gaierror):
        return BackendUnavailableError(
            "the database is not reachable. Nothing was written; this is safe "
            "to retry once the backend is available.",
            retryable=True,
        )

    return None


def domain_errors[**P, R](
    func: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Translate domain and infrastructure errors raised by a tool handler.

    Two things are translated. :class:`MemhubError` is the domain refusing a
    request. Driver-level failures - the database gone, the pool exhausted, a
    statement cancelled - are classified by
    :func:`classify_infrastructure_error` into codes that say what the caller
    should do next.

    Anything else is left to propagate. That is deliberate: an unrecognised
    exception is a bug, and the SDK's default handling logs a traceback and
    returns a generic message rather than leaking internals to the model. An
    unfamiliar failure dressed up as a known one would be worse than an opaque
    error, because it would come with confident and possibly wrong advice about
    whether retrying is safe.

    ``functools.wraps`` is load-bearing here, not tidiness. The SDK derives each
    tool's input and output schema by introspecting the handler's signature, so a
    wrapper advertising ``(*args, **kwargs)`` would produce a tool with no
    parameters. ``wraps`` sets ``__wrapped__``, which ``inspect.signature``
    follows back to the real function.

    For the same reason ``memhub.mcp.server`` deliberately does **not** use
    ``from __future__ import annotations``: string annotations would be resolved
    against *this* module's globals, where ``ProjectOut`` and friends do not
    exist.
    """

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await func(*args, **kwargs)
        except MemhubError as exc:
            logger.info(
                "tool rejected request",
                extra={"error_code": exc.code, "detail": exc.details},
            )
            raise as_tool_error(exc) from exc
        except Exception as exc:
            classified = classify_infrastructure_error(exc)
            if classified is None:
                raise
            # warning, not info: a caller error is normal traffic, but the
            # database being unreachable is an operational event someone
            # should see in the logs.
            logger.warning(
                "tool failed on infrastructure",
                extra={"error_code": classified.code},
                exc_info=exc,
            )
            raise as_tool_error(classified) from exc

    return wrapper
