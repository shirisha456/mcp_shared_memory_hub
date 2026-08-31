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
from collections.abc import Awaitable, Callable

from mcp.server.mcpserver.exceptions import ToolError

from memhub.domain.errors import MemhubError

logger = logging.getLogger(__name__)


def as_tool_error(exc: MemhubError) -> ToolError:
    """Render a domain error as the message the model will read.

    The error code is included because it is stable across wording changes - a
    client can branch on ``PROJECT_NOT_FOUND`` without string-matching prose.
    """
    return ToolError(f"[{exc.code}] {exc.message}")


def domain_errors[**P, R](
    func: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Translate domain errors raised by a tool handler.

    Anything that is not a :class:`MemhubError` is left to propagate. That is
    deliberate: an unexpected exception is a bug, and the SDK's default handling
    logs a traceback and returns a generic message rather than leaking internals
    to the model.

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

    return wrapper
