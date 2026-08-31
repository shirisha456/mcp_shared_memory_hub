"""End-to-end over the real stdio transport.

Everything else in the protocol suite drives an in-process server. This module
spawns the actual ``memhub-server`` console script as a subprocess and talks to
it over stdin/stdout, which is the only way to prove three things the in-process
tests cannot:

1. The console script and its entry point actually work.
2. **stdout carries nothing but JSON-RPC.** If any log line, warning or stray
   ``print`` reached stdout, the client's framing would break and every call
   here would fail. This is the cheapest possible guard against the single most
   common way an MCP server breaks, and it is why the assertion is worth the
   cost of a subprocess.
3. Two *separate sessions* - the second started after the first has fully
   exited - share state through PostgreSQL and nothing else. That is the
   Milestone 1 exit criterion.

Marked ``stdio`` so it can be deselected when iterating: it is markedly slower
than the in-process tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from memhub.config import Settings

pytestmark = [pytest.mark.integration, pytest.mark.stdio]

REPO_ROOT = Path(__file__).resolve().parents[2]


def server_params(database_url: str) -> StdioServerParameters:
    """Launch the package the way a real client would.

    ``sys.executable -m memhub.mcp`` rather than the ``memhub-server`` shim so
    the test runs against the interpreter executing it, which keeps it working
    on a machine where the console script is not on PATH.
    """
    env = dict(os.environ)
    env["MEMHUB_DATABASE_URL"] = database_url
    env["MEMHUB_LOG_LEVEL"] = "INFO"
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "memhub.mcp"],
        env=env,
        cwd=str(REPO_ROOT),
    )


@pytest.fixture
def database_url(settings: Settings, test_database: str) -> str:
    return settings.sqlalchemy_url.set(database=test_database).render_as_string(hide_password=False)


async def test_two_sessions_share_state_over_stdio(database_url: str) -> None:
    """The flagship interaction, over the real transport.

    Session one plays Claude Desktop and records a decision. It then exits
    completely - the process is gone, and with it any in-memory state. Session
    two plays Cursor, starts with no knowledge of the first, and retrieves the
    decision along with its provenance.

    Nothing is shared between the two processes except PostgreSQL. That is the
    entire thesis of the project, executed rather than asserted.
    """
    params = server_params(database_url)

    async with Client(params) as claude:
        created = await claude.call_tool(
            "project_use", {"slug": "ai-agent-control-plane", "create": True}
        )
        assert not created.is_error, created.content
        assert created.structured_content is not None
        project_id = created.structured_content["project_id"]

        written = await claude.call_tool(
            "memory_remember",
            {
                "project_id": project_id,
                "type": "DECISION",
                "content": (
                    "PostgreSQL is the source of truth for task state and queueing. "
                    "Redis is intentionally excluded from V1."
                ),
                "tags": ["queue", "architecture"],
                "source": "architecture discussion",
                "client": "claude-desktop",
                "human_confirmed": True,
            },
        )
        assert not written.is_error, written.content

    # The first server process has now exited.

    async with Client(params) as cursor:
        resolved = await cursor.call_tool("project_use", {"slug": "ai-agent-control-plane"})
        assert not resolved.is_error, resolved.content
        assert resolved.structured_content is not None
        assert resolved.structured_content["project_id"] == project_id
        assert resolved.structured_content["created"] is False

        found = await cursor.call_tool(
            "memory_search",
            {"project_id": project_id, "query": "queue", "types": ["DECISION"]},
        )
        assert not found.is_error, found.content
        assert found.structured_content is not None

        results = found.structured_content["results"]
        assert len(results) == 1
        memory = results[0]
        assert "PostgreSQL is the source of truth" in memory["content"]
        assert "Redis is intentionally excluded" in memory["content"]
        # Provenance survived the process boundary.
        assert memory["author_client"] == "claude-desktop"
        assert memory["author_kind"] == "human_confirmed"
        assert memory["source"] == "architecture discussion"
        assert memory["revision_no"] == 1


async def test_stdout_carries_only_protocol_traffic(database_url: str) -> None:
    """A working session is itself the proof.

    The client parses stdout as a JSON-RPC stream. Any log record, warning or
    banner written there would corrupt the framing and this handshake would
    fail, so a successful tool listing means stdout stayed clean end to end.
    """
    async with Client(server_params(database_url)) as client:
        listed = await client.list_tools()

    assert {tool.name for tool in listed.tools} == {
        "project_use",
        "memory_remember",
        "memory_revise",
        "memory_forget",
        "memory_search",
        "memory_history",
    }
