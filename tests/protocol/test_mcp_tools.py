"""MCP protocol contract.

Driven through an in-process client/server pair - no subprocess, no Claude
Desktop. The SDK's ``Client`` accepts an ``MCPServer`` instance directly, so
these tests exercise the real dispatch path, real schema generation and real
error mapping against the real database.

The golden manifest tests are the ones that earn their place over time. Tool
descriptions are the prompt that steers the model, which makes them production
surface: an accidental edit changes system behaviour without changing a line of
logic, and nothing else in the suite would notice.

Note that ``Client`` is entered *inside* each test rather than in a fixture.
pytest-asyncio runs fixture setup and teardown in different tasks, and the
client holds an anyio task group whose cancel scope must be exited in the task
that entered it - a fixture-based client fails teardown with "Attempted to exit
cancel scope in a different task".
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.types import TextResourceContents
from sqlalchemy.ext.asyncio import AsyncEngine

from memhub.mcp.server import build_server
from memhub.persistence.engine import create_session_factory

pytestmark = pytest.mark.integration

EXPECTED_TOOLS = {"project_use", "memory_remember", "memory_revise", "memory_search"}


@pytest.fixture
def server(engine: AsyncEngine) -> MCPServer:
    """A server bound to the module's test database.

    Uses ``engine`` rather than the rolled-back ``db_session`` fixture: the tools
    open and commit their own transactions, which is the behaviour under test.
    Cleanup comes from the module-scoped database being dropped afterwards.
    """
    return build_server(create_session_factory(engine), name="memhub-test")


def ok(result: Any) -> dict[str, Any]:
    """Structured content from a successful tool call."""
    assert not result.is_error, f"tool returned an error: {result.content}"
    assert result.structured_content is not None
    return dict(result.structured_content)


def error_text(result: Any) -> str:
    assert result.is_error, "expected an error result"
    return str(result.content[0].text)


async def make_project(client: Client, slug: str) -> str:
    created = ok(await client.call_tool("project_use", {"slug": slug, "create": True}))
    return str(created["project_id"])


class TestManifest:
    async def test_exposes_exactly_the_expected_tools(self, server: MCPServer) -> None:
        """A guard against accidental surface growth.

        The tool surface is meant to stay small and deliberate. A new tool
        appearing without this set changing means someone added one without
        deciding it belonged.
        """
        async with Client(server) as client:
            listed = await client.list_tools()
        assert {tool.name for tool in listed.tools} == EXPECTED_TOOLS

    async def test_every_tool_declares_an_output_schema(self, server: MCPServer) -> None:
        """Declared output schemas are what let a client validate our responses."""
        async with Client(server) as client:
            listed = await client.list_tools()
        for tool in listed.tools:
            assert tool.output_schema, f"{tool.name} has no output schema"

    async def test_tool_inputs_are_introspected_not_empty(self, server: MCPServer) -> None:
        """Regression guard for a subtle decorator bug.

        The SDK derives the input schema from the handler signature. A wrapper
        that does not preserve ``__wrapped__`` produces a tool advertising no
        parameters at all - which fails at call time, not at registration, and
        only against a real client.
        """
        async with Client(server) as client:
            listed = await client.list_tools()
        remember = next(t for t in listed.tools if t.name == "memory_remember")
        properties = remember.input_schema["properties"]
        assert {"project_id", "type", "content", "tags", "importance"} <= set(properties)

    async def test_descriptions_carry_the_operating_contract(self, server: MCPServer) -> None:
        """Tool descriptions are production surface, not documentation.

        These sentences are the entire mechanism preventing (a) the model storing
        credentials and (b) the model storing a rejected alternative as a
        standalone fact that later retrieval would surface as current. Edit them
        away and system behaviour changes silently.
        """
        async with Client(server) as client:
            listed = await client.list_tools()

        remember = next(t for t in listed.tools if t.name == "memory_remember")
        assert remember.description is not None
        assert "NEVER record credentials" in remember.description
        assert "rejected alternative INSIDE the decision" in remember.description

        project_use = next(t for t in listed.tools if t.name == "project_use")
        assert project_use.description is not None
        assert "never created implicitly" in project_use.description

        # A conflict is a normal outcome, and the description has to say so.
        # A model told only "your write failed" retries blindly; one told
        # "someone changed it, here is their version" merges.
        revise = next(t for t in listed.tools if t.name == "memory_revise")
        assert revise.description is not None
        assert "This is not an error" in revise.description
        assert "Do not simply resend" in revise.description

    async def test_server_instructions_do_not_overclaim(self, server: MCPServer) -> None:
        """The accuracy constraint, asserted rather than trusted.

        An MCP server receives tool-call arguments, never transcripts. The
        instructions must say so: a model that believes this server can read
        conversations will behave as though memories appear on their own.
        """
        async with Client(server) as client:
            instructions = client.instructions
        assert instructions is not None
        assert "cannot see conversations" in instructions


class TestProjectUse:
    async def test_creates_and_resolves(self, server: MCPServer) -> None:
        async with Client(server) as client:
            created = ok(await client.call_tool("project_use", {"slug": "demo", "create": True}))
            resolved = ok(await client.call_tool("project_use", {"slug": "demo"}))

        assert created["created"] is True
        assert resolved["created"] is False
        assert resolved["project_id"] == created["project_id"]

    async def test_missing_project_is_a_tool_error_not_a_protocol_error(
        self, server: MCPServer
    ) -> None:
        """The distinction from architecture section 3.4(a).

        A protocol error means the request was malformed or the server broke. A
        domain refusal is neither - the model must be able to see it and correct
        itself, which requires it to arrive as a result rather than an exception.
        """
        async with Client(server) as client:
            result = await client.call_tool("project_use", {"slug": "never-created"})
        assert "PROJECT_NOT_FOUND" in error_text(result)

    async def test_error_carries_a_stable_code(self, server: MCPServer) -> None:
        """Clients branch on the code, not on prose that may be reworded."""
        async with Client(server) as client:
            result = await client.call_tool("project_use", {"slug": "UPPER CASE"})
        assert "[VALIDATION_FAILED]" in error_text(result)


class TestRememberAndSearch:
    async def test_write_then_read_back(self, server: MCPServer) -> None:
        """The Milestone 1 exit criterion, in miniature."""
        async with Client(server) as client:
            pid = await make_project(client, "flagship")
            written = ok(
                await client.call_tool(
                    "memory_remember",
                    {
                        "project_id": pid,
                        "type": "DECISION",
                        "content": (
                            "PostgreSQL is the task queue. Redis is intentionally excluded from V1."
                        ),
                        "tags": ["queue"],
                        "client": "claude-desktop",
                    },
                )
            )
            found = ok(
                await client.call_tool("memory_search", {"project_id": pid, "query": "queue"})
            )

        assert written["outcome"] == "created"
        assert written["memory"]["revision_no"] == 1
        assert found["returned"] == 1
        assert found["results"][0]["content"].startswith("PostgreSQL is the task queue")
        assert found["results"][0]["author_client"] == "claude-desktop"

    async def test_outcome_discriminator_is_present(self, server: MCPServer) -> None:
        """Callers must branch on 'outcome' rather than assume a new memory.

        Milestone 2 adds 'idempotent_replay' and Milestone 3 'deduplicated'.
        Shipping the field now means adding those will not break a client
        written against this version.
        """
        async with Client(server) as client:
            pid = await make_project(client, "discriminator")
            written = ok(
                await client.call_tool(
                    "memory_remember",
                    {"project_id": pid, "type": "FACT", "content": "a fact"},
                )
            )
        assert written["outcome"] == "created"

    async def test_unknown_type_is_rejected_with_the_allowed_values(
        self, server: MCPServer
    ) -> None:
        """The cut types must fail loudly, not be silently accepted as tags."""
        async with Client(server) as client:
            pid = await make_project(client, "badtype")
            result = await client.call_tool(
                "memory_remember",
                {"project_id": pid, "type": "OBSERVATION", "content": "x"},
            )
        text = error_text(result)
        assert "OBSERVATION" in text
        assert "DECISION, CONSTRAINT, FACT, TASK" in text

    async def test_malformed_uuid_is_rejected(self, server: MCPServer) -> None:
        async with Client(server) as client:
            result = await client.call_tool(
                "memory_remember",
                {"project_id": "not-a-uuid", "type": "FACT", "content": "x"},
            )
        assert "must be a UUID" in error_text(result)

    async def test_oversized_content_is_rejected_with_the_actual_size(
        self, server: MCPServer
    ) -> None:
        async with Client(server) as client:
            pid = await make_project(client, "oversized")
            result = await client.call_tool(
                "memory_remember",
                {"project_id": pid, "type": "FACT", "content": "x" * 9000},
            )
        assert "9000 characters" in error_text(result)

    async def test_task_expiry_is_applied_through_the_protocol(self, server: MCPServer) -> None:
        """TASK's mandatory TTL must reach the client, not just the database."""
        async with Client(server) as client:
            pid = await make_project(client, "tasks")
            task = ok(
                await client.call_tool(
                    "memory_remember",
                    {
                        "project_id": pid,
                        "type": "TASK",
                        "content": "Currently implementing worker heartbeat logic.",
                    },
                )
            )
            fact = ok(
                await client.call_tool(
                    "memory_remember",
                    {"project_id": pid, "type": "FACT", "content": "Python 3.12 minimum."},
                )
            )
        assert task["memory"]["expires_at"] is not None
        assert fact["memory"]["expires_at"] is None

    async def test_search_states_what_it_excludes(self, server: MCPServer) -> None:
        """The response is self-describing about stale-memory suppression."""
        async with Client(server) as client:
            pid = await make_project(client, "excludes")
            found = ok(await client.call_tool("memory_search", {"project_id": pid}))
        assert "superseded" in found["filtered_out"]

    async def test_projects_are_isolated_across_the_protocol(self, server: MCPServer) -> None:
        async with Client(server) as client:
            a = await make_project(client, "iso-a")
            b = await make_project(client, "iso-b")
            await client.call_tool(
                "memory_remember",
                {"project_id": a, "type": "FACT", "content": "belongs to a"},
            )
            found = ok(await client.call_tool("memory_search", {"project_id": b}))
        assert found["returned"] == 0


class TestSharedStateAcrossClients:
    async def test_a_second_client_sees_the_first_clients_memory(self, server: MCPServer) -> None:
        """The point of the whole project, reduced to one test.

        Two independent client sessions, one shared database. The second session
        starts with no knowledge of the first and retrieves what it wrote. This
        is the Milestone 1 exit criterion: a memory survives the end of the
        session that created it.
        """
        async with Client(server) as claude:
            pid = await make_project(claude, "shared-state")
            await claude.call_tool(
                "memory_remember",
                {
                    "project_id": pid,
                    "type": "DECISION",
                    "content": "PostgreSQL is the task queue.",
                    "client": "claude-desktop",
                },
            )

        # First session is fully closed here. Nothing is carried over in memory.
        async with Client(server) as cursor:
            resolved = ok(await cursor.call_tool("project_use", {"slug": "shared-state"}))
            found = ok(
                await cursor.call_tool(
                    "memory_search", {"project_id": resolved["project_id"], "query": "queue"}
                )
            )

        assert resolved["project_id"] == pid
        assert found["returned"] == 1
        assert found["results"][0]["content"] == "PostgreSQL is the task queue."
        assert found["results"][0]["author_client"] == "claude-desktop"


class TestResources:
    async def test_memory_resource_is_identity_addressed(self, server: MCPServer) -> None:
        """Read-only and side-effect free, which is why it is a resource."""
        async with Client(server) as client:
            pid = await make_project(client, "resources")
            written = ok(
                await client.call_tool(
                    "memory_remember",
                    {"project_id": pid, "type": "CONSTRAINT", "content": "No Redis in V1."},
                )
            )
            memory_id = written["memory"]["memory_id"]
            read = await client.read_resource(f"memory://memories/{memory_id}")

        contents = read.contents[0]
        assert isinstance(contents, TextResourceContents)
        body = json.loads(contents.text)
        assert body["content"] == "No Redis in V1."
        assert body["memory_id"] == memory_id


class TestReviseOverTheProtocol:
    async def test_conflict_is_a_result_not_an_error(self, server: MCPServer) -> None:
        """The architecture 3.4(a) amendment, exercised end to end.

        A conflict reaches the model as an ordinary structured result with
        outcome='conflict'. Not is_error, because the request was well formed and
        the domain simply said no - and the model needs machine-readable data to
        branch on, not a sentence to parse.
        """
        async with Client(server) as client:
            pid = await make_project(client, "revise-conflict")
            written = ok(
                await client.call_tool(
                    "memory_remember",
                    {"project_id": pid, "type": "DECISION", "content": "Redis is the queue."},
                )
            )
            memory_id = written["memory"]["memory_id"]

            first = ok(
                await client.call_tool(
                    "memory_revise",
                    {
                        "project_id": pid,
                        "memory_id": memory_id,
                        "expected_revision": 1,
                        "content": "PostgreSQL is the queue.",
                        "client": "cursor",
                    },
                )
            )
            # A second client still holding revision 1.
            stale = await client.call_tool(
                "memory_revise",
                {
                    "project_id": pid,
                    "memory_id": memory_id,
                    "expected_revision": 1,
                    "content": "SQLite is the queue.",
                    "client": "claude-desktop",
                },
            )

        assert first["outcome"] == "revised"
        assert first["memory"]["revision_no"] == 2

        assert stale.is_error is False, "a conflict is an outcome, not a tool error"
        assert stale.structured_content is not None
        payload = dict(stale.structured_content)
        assert payload["outcome"] == "conflict"
        assert payload["expected_revision"] == 1
        assert payload["current_revision"] == 2
        # Everything needed to merge and retry, in one round trip.
        assert payload["memory"]["content"] == "PostgreSQL is the queue."
        assert payload["memory"]["author_client"] == "cursor"
        assert "expected_revision=2" in payload["guidance"]

    async def test_search_returns_only_the_current_revision(self, server: MCPServer) -> None:
        """Superseded revisions stay in the log but leave retrieval."""
        async with Client(server) as client:
            pid = await make_project(client, "revise-search")
            written = ok(
                await client.call_tool(
                    "memory_remember",
                    {"project_id": pid, "type": "DECISION", "content": "Redis is the queue."},
                )
            )
            await client.call_tool(
                "memory_revise",
                {
                    "project_id": pid,
                    "memory_id": written["memory"]["memory_id"],
                    "expected_revision": 1,
                    "content": "PostgreSQL is the queue.",
                },
            )
            found = ok(await client.call_tool("memory_search", {"project_id": pid}))

        assert found["returned"] == 1
        assert found["results"][0]["content"] == "PostgreSQL is the queue."
        assert found["results"][0]["revision_no"] == 2

    async def test_retry_with_an_idempotency_key_replays(self, server: MCPServer) -> None:
        """A dropped connection must not cost the caller a duplicate memory."""
        async with Client(server) as client:
            pid = await make_project(client, "revise-idempotent")
            key = "protocol-test-key-0001"
            payload = {
                "project_id": pid,
                "type": "FACT",
                "content": "Python 3.12 minimum.",
                "client_request_id": key,
            }
            first = ok(await client.call_tool("memory_remember", payload))
            retry = ok(await client.call_tool("memory_remember", payload))
            found = ok(await client.call_tool("memory_search", {"project_id": pid}))

        assert first["outcome"] == "created"
        assert retry["outcome"] == "idempotent_replay"
        assert retry["memory"]["memory_id"] == first["memory"]["memory_id"]
        assert found["returned"] == 1, "the retry must not have created a second memory"

    async def test_reusing_a_key_for_a_different_request_is_refused(
        self, server: MCPServer
    ) -> None:
        async with Client(server) as client:
            pid = await make_project(client, "revise-key-reuse")
            key = "protocol-test-key-0002"
            ok(
                await client.call_tool(
                    "memory_remember",
                    {
                        "project_id": pid,
                        "type": "FACT",
                        "content": "the original",
                        "client_request_id": key,
                    },
                )
            )
            result = await client.call_tool(
                "memory_remember",
                {
                    "project_id": pid,
                    "type": "FACT",
                    "content": "something else entirely",
                    "client_request_id": key,
                },
            )

        assert "IDEMPOTENCY_KEY_REUSED" in error_text(result)
