"""stdio entry point.

Each MCP host - Claude Desktop, Cursor - spawns its *own* copy of this process.
The two share no memory and no cache; PostgreSQL is the only shared state
between them. That is why the concurrency control in Milestone 2 is real rather
than decorative, and it is why this process holds no authoritative state of its
own: a restart is a non-event.

The single most important line here is ``configure_logging``. stdout carries the
JSON-RPC frames, so every log record must go to stderr. One stray byte on stdout
and the host fails with an opaque parse error that says nothing about the cause.
"""

from __future__ import annotations

import asyncio
import sys

from memhub.config import get_settings
from memhub.mcp.server import build_server
from memhub.observability.logging import configure_logging, get_logger
from memhub.persistence.engine import create_engine, create_session_factory


async def _serve() -> None:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        log_format=settings.log_format,
        service_name=settings.service_name,
    )
    log = get_logger("memhub.mcp")

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    server = build_server(session_factory)

    log.info(
        "starting stdio server",
        extra={
            "database": settings.sqlalchemy_url.render_as_string(hide_password=True),
            "pool_size": settings.db_pool_size,
        },
    )
    try:
        await server.run_stdio_async()
    finally:
        await engine.dispose()
        log.info("stdio server stopped")


def main() -> None:
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:  # pragma: no cover - operator action
        sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
