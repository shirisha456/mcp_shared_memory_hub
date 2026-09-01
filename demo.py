"""A hands-on walkthrough of the README story, over the real stdio transport.

Run it against a live database to watch the whole thing work end to end:

    python demo.py

Two separate server processes are started, one after the other. The first plays
Claude Desktop and the second plays Cursor. They share no memory and no cache -
the first has fully exited before the second starts - so anything the second one
knows, it learned from PostgreSQL.

This is a demonstration, not a test. The assertions live in tests/.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from typing import Any

from mcp import Client, StdioServerParameters

PROJECT = f"demo-{uuid.uuid4().hex[:8]}"
DATABASE_URL = os.environ.get(
    "MEMHUB_DATABASE_URL", "postgresql+asyncpg://memhub:memhub@localhost:5435/memhub"
)


def server() -> StdioServerParameters:
    """Launch the server the way a real MCP client would."""
    env = dict(os.environ)
    env["MEMHUB_DATABASE_URL"] = DATABASE_URL
    env["MEMHUB_LOG_LEVEL"] = "WARNING"
    return StdioServerParameters(command=sys.executable, args=["-m", "memhub.mcp"], env=env)


def say(step: str, detail: str = "") -> None:
    print(f"\n\033[1m{step}\033[0m" + (f"\n{detail}" if detail else ""))


async def call(client: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(tool, args)
    if result.is_error:
        raise SystemExit(f"{tool} failed: {result.content}")
    assert result.structured_content is not None
    return result.structured_content


def show(results: list[dict[str, Any]]) -> None:
    if not results:
        print("   (nothing returned)")
    for item in results:
        print(f"   - [{item['type']}] {item['content'][:88]}")
        print(f"     by {item['author_client']}, revision {item['revision_no']}")


async def main() -> None:
    print(f"database : {DATABASE_URL}")
    print(f"project  : {PROJECT}")

    # ---- Monday: Claude Desktop records a decision -------------------------
    async with Client(server()) as claude:
        project = await call(claude, "project_use", {"slug": PROJECT, "create": True})
        pid = project["project_id"]

        say("MONDAY - Claude Desktop records the queue decision.")
        redis = await call(
            claude,
            "memory_remember",
            {
                "project_id": pid,
                "type": "DECISION",
                "content": "The background job queue runs on Redis.",
                "tags": ["queue", "architecture"],
                "source": "architecture discussion",
                "client": "claude-desktop",
                "human_confirmed": True,
            },
        )
        redis_id = redis["memory"]["memory_id"]
        print(f"   stored {redis_id}")

        await call(
            claude,
            "memory_remember",
            {
                "project_id": pid,
                "type": "CONSTRAINT",
                "content": "Never log secrets or connection strings.",
                "client": "claude-desktop",
                "human_confirmed": True,
            },
        )
        print("   stored a second memory (a CONSTRAINT)")

    # The Claude Desktop process is now gone. So is everything it held in memory.

    # ---- Tuesday: Cursor, a brand new process, can see it ------------------
    async with Client(server()) as cursor:
        say(
            "TUESDAY - Cursor starts fresh and searches for 'queue'.",
            "Different process. Nothing shared but PostgreSQL.",
        )
        found = await call(cursor, "memory_search", {"project_id": pid, "query": "queue"})
        show(found["results"])

        # ---- Six months later: the decision is reversed --------------------
        say(
            "SIX MONTHS LATER - Cursor records the replacement with supersedes.",
            "Retiring the old fact and asserting the new one is a single transaction.",
        )
        replacement = await call(
            cursor,
            "memory_remember",
            {
                "project_id": pid,
                "type": "DECISION",
                "content": (
                    "The background job queue runs on PostgreSQL using SKIP LOCKED. "
                    "Redis was removed."
                ),
                "tags": ["queue", "architecture"],
                "supersedes": [redis_id],
                "client": "cursor",
                "human_confirmed": True,
            },
        )
        print(f"   stored {replacement['memory']['memory_id']}, retiring {redis_id}")
        print(f"   outcome: {replacement['outcome']}, superseded: {replacement['superseded']}")

        say(
            "THE POINT - search for 'redis' now.",
            "The retired memory mentions Redis by name and matches the query better\n"
            "than the replacement does. A ranking-based system would return it.",
        )
        stale = await call(cursor, "memory_search", {"project_id": pid, "query": "redis"})
        show(stale["results"])

        say("It is not gone, though. memory_history still has the whole chain.")
        history = await call(cursor, "memory_history", {"project_id": pid, "memory_id": redis_id})
        print(f"   status     : {history['status']}")
        print(f"   content    : {history['memory']['content']}")
        replaced_by = history.get("superseded_by")
        print(f"   replaced by: {replaced_by['content'] if replaced_by else '-'}")
        print(f"   revisions  : {len(history['revisions'])}")

        say(
            "CONTEXT BRIEF - the most useful summary that fits 500 tokens.",
            "Not search. Selection under a constraint.",
        )
        brief = await call(cursor, "memory_context", {"project_id": pid, "token_budget": 500})
        budget = brief["budget"]
        print(
            f"   spent {budget['estimated_used']} of {budget['requested']} tokens "
            f"({budget['utilisation']:.0%}), estimator: {budget['estimator']}"
        )
        print(
            f"   considered {budget['considered']}, selected {budget['selected']}, "
            f"dropped {budget['dropped']}"
        )
        print()
        print(brief["brief"])

    print("\nDone. Inspect it with:  memhub-admin status")


if __name__ == "__main__":
    asyncio.run(main())
