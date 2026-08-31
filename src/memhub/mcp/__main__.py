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
from contextlib import suppress

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memhub.config import get_settings
from memhub.embeddings.base import EmbeddingPort
from memhub.embeddings.factory import build_embedder
from memhub.embeddings.worker import EmbeddingWorker
from memhub.mcp.server import build_server
from memhub.observability.logging import configure_logging, get_logger
from memhub.persistence.engine import create_engine, create_session_factory

EMBEDDING_POLL_SECONDS = 2.0


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
    embedder = build_embedder(settings)
    server = build_server(session_factory, embedder=embedder)

    # The outbox worker runs in-process, as a background task alongside the
    # server. Each client spawns its own process, so each gets a worker and
    # they drain the same queue safely - that is what SKIP LOCKED is for.
    # A separate worker process would be more robust and is what a hosted
    # deployment would want; for a stdio subprocess it would mean asking the
    # user to run a second thing, which is worse than the failure it avoids.
    worker_task: asyncio.Task[None] | None = None
    if embedder is not None:
        worker_task = asyncio.create_task(_drain_forever(session_factory, embedder))

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
        if worker_task is not None:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task
        await engine.dispose()
        log.info("stdio server stopped")


async def _drain_forever(
    session_factory: async_sessionmaker[AsyncSession], embedder: EmbeddingPort
) -> None:
    """Poll the outbox for as long as the server runs.

    Polling rather than LISTEN/NOTIFY: the queue is small, the latency budget is
    "before the user asks their next question", and a notification channel would
    add a failure mode (a missed notification means a job sits forever) for a
    gain nobody would notice.

    Any exception is logged and swallowed. A crashing background task must not
    take the server down - the whole reason embedding is asynchronous is that it
    is allowed to fail.
    """
    log = get_logger("memhub.embeddings")
    worker = EmbeddingWorker(session_factory, embedder)
    while True:
        try:
            outcome = await worker.drain(max_batches=10)
            if outcome.embedded or outcome.failed:
                log.info(
                    "embedding batch complete",
                    extra={"embedded": outcome.embedded, "failed": outcome.failed},
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("embedding worker iteration failed")
        await asyncio.sleep(EMBEDDING_POLL_SECONDS)


def main() -> None:
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:  # pragma: no cover - operator action
        sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
