"""Operator commands.

Deliberately **not** reachable over MCP. Two of these destroy data irreversibly,
and an irreversible unrecoverable delete does not belong in a language model's
tool surface no matter how carefully the description is worded. A model that
misreads a request and calls ``memory_forget`` costs a tombstone that can be
undone; the same mistake against ``purge`` costs the content.

That split is the reason ``memory_forget`` is a soft delete at all. Soft delete
is the right default and the wrong tool for the one case that genuinely needs
destruction - a credential recorded by mistake - so that case gets a separate,
human-invoked, audited path.

    memhub-admin purge --project ai-agent-control-plane --memory <uuid> --yes
    memhub-admin gc
    memhub-admin status
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, Result, text
from sqlalchemy.ext.asyncio import AsyncSession

from memhub.config import get_settings
from memhub.observability.logging import configure_logging, get_logger
from memhub.persistence.engine import create_engine, create_session_factory, session_scope
from memhub.persistence.repositories.projects import ProjectRepository
from memhub.services import idempotency

log = get_logger("memhub.admin")

# Every table holding a copy of, or a derivative of, a memory's content. Purge
# must clear all of them or the erasure is partial - and a partial erasure of a
# leaked credential is not an erasure.
_PURGE_ORDER = (
    "DELETE FROM memory_embeddings WHERE memory_id = :m",
    "DELETE FROM embedding_jobs WHERE memory_id = :m",
    "DELETE FROM memory_dedup_keys WHERE memory_id = :m",
    "DELETE FROM memory_attestations WHERE memory_id = :m",
    "DELETE FROM memory_revisions WHERE memory_id = :m",
)


def _affected(result: Result[Any]) -> int:
    """How many rows a DELETE or UPDATE touched.

    ``Session.execute`` is typed as returning ``Result``, which has no
    ``rowcount`` because a SELECT has no meaningful one. A DML statement always
    returns a ``CursorResult``, which does. The cast records that, in one place,
    instead of silencing the type checker at each call site.
    """
    return int(cast("CursorResult[Any]", result).rowcount or 0)


async def purge(
    session: AsyncSession, *, project_id: uuid.UUID, memory_id: uuid.UUID, reason: str
) -> dict[str, int]:
    """Destroy a memory and everything derived from it.

    The one operation in this system that actually erases content. Everything
    else - forget, supersede, expire - hides a memory while keeping the record,
    because history is the point. This exists for the case where the content
    itself is the problem.

    The audit row survives, with the content replaced by a redaction marker. That
    is the whole reason ``audit_events`` has no foreign key to ``memories``: the
    record that a purge happened has to outlive its subject, and a CASCADE would
    erase the evidence along with the thing it describes.
    """
    exists = (
        await session.execute(
            text("SELECT 1 FROM memories WHERE id = :m AND project_id = :p"),
            {"m": memory_id, "p": project_id},
        )
    ).scalar_one_or_none()
    if exists is None:
        raise SystemExit(f"no memory {memory_id} in that project")

    removed: dict[str, int] = {}
    for statement in _PURGE_ORDER:
        result = await session.execute(text(statement), {"m": memory_id})
        table = statement.split()[2]
        removed[table] = _affected(result)

    # Any other memory pointing here as its successor must be released first, or
    # the foreign key refuses the delete and the purge fails halfway.
    await session.execute(
        text(
            "UPDATE memories SET superseded_by_id = NULL, superseded_at = NULL, "
            "status = 'DELETED', deleted_at = COALESCE(deleted_at, now()) "
            "WHERE superseded_by_id = :m"
        ),
        {"m": memory_id},
    )
    result = await session.execute(text("DELETE FROM memories WHERE id = :m"), {"m": memory_id})
    removed["memories"] = _affected(result)

    # Scrub any earlier audit detail, then record the purge itself.
    await session.execute(
        text("UPDATE audit_events SET detail = '{\"redacted\": true}'::jsonb WHERE memory_id = :m"),
        {"m": memory_id},
    )
    await session.execute(
        text(
            "INSERT INTO audit_events (project_id, memory_id, action, outcome, "
            "actor_client, detail) VALUES (:p, :m, 'purge', 'ok', 'operator', "
            # Explicit cast: asyncpg cannot infer a parameter's type inside
            # jsonb_build_object and fails with IndeterminateDatatypeError.
            "jsonb_build_object('reason', CAST(:r AS text)))"
        ),
        {"p": project_id, "m": memory_id, "r": reason},
    )
    return removed


async def collect_garbage(session: AsyncSession, *, batch: int = 1000) -> dict[str, int]:
    """Remove what has outlived its usefulness.

    Bounded per call. An unbounded ``DELETE`` on a busy table takes a long lock
    and becomes its own outage, which is a poor trade for tidiness.

    Note what is *not* collected: no memory is ever removed here. Retention that
    silently deletes a project's knowledge would be indistinguishable from data
    loss, so the only thing this touches is machinery - spent idempotency keys
    and permanently failed embedding jobs.
    """
    keys = await idempotency.purge_expired(session, limit=batch)

    dead = await session.execute(
        text(
            "DELETE FROM embedding_jobs WHERE id IN ("
            "  SELECT id FROM embedding_jobs "
            "   WHERE state = 'DEAD' AND created_at < now() - interval '30 days' "
            "   LIMIT :n)"
        ),
        {"n": batch},
    )
    return {"idempotency_keys": keys, "dead_embedding_jobs": _affected(dead)}


async def status(session: AsyncSession) -> dict[str, object]:
    """A short operational summary, for answering "is anything stuck?"."""
    row = (
        await session.execute(
            text(
                """
                SELECT
                  (SELECT count(*) FROM projects)                                AS projects,
                  (SELECT count(*) FROM memories WHERE status = 'ACTIVE')        AS active,
                  (SELECT count(*) FROM memories WHERE status = 'SUPERSEDED')    AS superseded,
                  (SELECT count(*) FROM memories WHERE status = 'DELETED')       AS deleted,
                  (SELECT count(*) FROM embedding_jobs WHERE state = 'PENDING')  AS embed_pending,
                  (SELECT count(*) FROM embedding_jobs WHERE state = 'DEAD')     AS embed_dead,
                  (SELECT count(*) FROM idempotency_keys WHERE expires_at < now()) AS stale_keys
                """
            )
        )
    ).one()
    return dict(row._mapping)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memhub-admin",
        description=(
            "Operator commands for the memory server. These are not exposed over "
            "MCP: purge destroys content irreversibly, which is not something a "
            "language model should be able to do on its own reading of a request."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "purge",
        help="permanently erase one memory and everything derived from it",
        description=(
            "Irreversible. Use this when the content itself is the problem - a "
            "credential recorded by mistake - not to tidy up. To retire a memory "
            "while keeping its history, the client tool memory_forget is the "
            "right operation."
        ),
    )
    p.add_argument("--project", required=True, help="project slug")
    p.add_argument("--memory", required=True, help="memory id (UUID)")
    p.add_argument("--reason", default="operator purge", help="recorded in the audit log")
    p.add_argument(
        "--yes",
        action="store_true",
        help="confirm. Without it the command reports what would be destroyed and stops.",
    )

    sub.add_parser("gc", help="remove expired idempotency keys and long-dead embedding jobs")
    sub.add_parser("status", help="counts of memories, pending embeddings and stale keys")
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(level=settings.log_level, log_format=settings.log_format)
    engine = create_engine(settings)
    sessions = create_session_factory(engine)

    try:
        async with session_scope(sessions) as session:
            if args.command == "status":
                summary = await status(session)
                log.info("status", extra=summary)
                _emit(summary)
                return 0

            if args.command == "gc":
                removed = await collect_garbage(session)
                log.info("garbage collected", extra=removed)
                _emit(removed)
                return 0

            project = await ProjectRepository(session).get_by_slug(args.project)
            if project is None:
                _emit({"error": f"no project with slug {args.project!r}"})
                return 1

            memory_id = uuid.UUID(args.memory)
            if not args.yes:
                # Refusing to act without confirmation is the point. The content
                # about to be destroyed is shown so the operator can check they
                # named the right memory before it stops being recoverable.
                content = (
                    await session.execute(
                        text(
                            "SELECT content FROM memory_revisions "
                            "WHERE memory_id = :m AND is_current"
                        ),
                        {"m": memory_id},
                    )
                ).scalar_one_or_none()
                _emit(
                    {
                        "would_purge": str(memory_id),
                        "project": project.slug,
                        "current_content": content,
                        "note": "irreversible. re-run with --yes to proceed.",
                    }
                )
                return 0

            removed = await purge(
                session, project_id=project.id, memory_id=memory_id, reason=args.reason
            )
            log.warning("memory purged", extra={"memory_id": str(memory_id), **removed})
            _emit({"purged": str(memory_id), "rows_removed": removed})
            return 0
    finally:
        await engine.dispose()


def _emit(payload: object) -> None:
    """Write a result for a human.

    stdout is safe here in a way it never is in the server: this is a CLI, not an
    MCP subprocess, so nothing is parsing this stream as JSON-RPC.
    """
    import json

    sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_run(_parser().parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
