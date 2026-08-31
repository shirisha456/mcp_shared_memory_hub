"""The MCP server.

Handlers are thin by rule: validate, call a service, map the result. No SQL, no
policy, no transaction management inline. Everything they call is reachable from
a plain function call, which is why the service-layer tests need no MCP client
at all.

**Tool descriptions are production surface, not documentation.** They are the
prompt that steers the model, so they carry the operating contract - do not
store credentials; state a rejected alternative inside the decision rather than
as a standalone fact. Those sentences are load-bearing for correctness, and the
golden-manifest protocol test fails if they change by accident.

Seven tools, which is the whole surface.

Supersession is deliberately *not* a seventh tool: retiring a fact and
asserting its replacement are one atomic act, so ``memory_remember`` takes a
``supersedes`` argument instead. Splitting them would leave a window in which
the project appears to have no opinion about something it has a firm opinion
about.
"""

# NOTE: no ``from __future__ import annotations`` in this module, deliberately.
# The MCP SDK builds each tool's JSON schema by introspecting runtime
# annotations. With PEP 563 they would be strings needing evaluation against the
# right module globals, which breaks as soon as a handler is wrapped by a
# decorator defined elsewhere.

import json
import uuid
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memhub.domain.enums import AuthorKind, MemoryType
from memhub.domain.errors import ValidationFailedError
from memhub.embeddings.base import EmbeddingPort
from memhub.mcp.mapping import domain_errors
from memhub.mcp.schemas import (
    ContextOut,
    ForgetOut,
    HistoryOut,
    MemoryOut,
    ProjectOut,
    RememberOut,
    ReviseOut,
    SearchOut,
)
from memhub.persistence.engine import session_scope
from memhub.persistence.models import Memory
from memhub.persistence.repositories.projects import ProjectRepository
from memhub.services import context as context_service
from memhub.services import memories as memory_service
from memhub.services import projects as project_service
from memhub.services import retrieval as retrieval_service

SERVER_INSTRUCTIONS = """\
Shared, versioned project memory for MCP clients.

This server stores knowledge you explicitly record. It cannot see conversations \
- it only ever receives the arguments of the tool calls you make to it.

Record a memory when the user establishes something durable: an architectural \
decision and what it rules out, a constraint the project must respect, a \
load-bearing fact, or the piece of work currently in progress. Do not record \
conversational chatter, and never record credentials.

Before answering questions about how a project works, search memory first: \
another client may have recorded the answer in an earlier session.\
"""


def _parse_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ValidationFailedError(f"{field} must be a UUID, got {value!r}.") from exc


def _parse_type(value: str) -> MemoryType:
    try:
        return MemoryType(value.upper())
    except ValueError as exc:
        allowed = ", ".join(t.value for t in MemoryType)
        raise ValidationFailedError(
            f"Unknown memory type {value!r}. Use one of: {allowed}."
        ) from exc


def build_server(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    name: str = "memhub",
    version: str = "0.1.0",
    embedder: EmbeddingPort | None = None,
) -> MCPServer:
    """Construct the server bound to a session factory.

    Taking the factory as an argument rather than reaching for a global is what
    makes the protocol tests possible: they build a server against the test
    database and drive it in-process, with no subprocess and no fixtures beyond
    the ones the service tests already use.
    """
    server = MCPServer(
        name=name,
        version=version,
        title="MCP Shared Memory Hub",
        instructions=SERVER_INSTRUCTIONS,
    )

    @server.tool(
        name="project_use",
        title="Resolve or create a project",
        description=(
            "Resolve the project namespace that memories belong to, and return its "
            "canonical project_id for use with the other tools.\n\n"
            "Pass whatever you know: a slug, the repository's git remote, or the "
            "workspace path. Hints are resolved independently and must agree; if "
            "they point at different projects you get an error listing them rather "
            "than a guess.\n\n"
            "Projects are never created implicitly. If nothing matches you get an "
            "error, and creating one requires create=true with an explicit slug. "
            "This is deliberate: a client opened in the wrong directory would "
            "otherwise silently start a second, empty memory namespace and split "
            "the project's knowledge in half."
        ),
    )
    @domain_errors
    async def project_use(
        slug: Annotated[
            str | None,
            Field(description="Stable human-facing key, e.g. 'ai-agent-control-plane'."),
        ] = None,
        project_id: Annotated[
            str | None, Field(description="Canonical UUID, if you already have it.")
        ] = None,
        git_remote: Annotated[
            str | None,
            Field(description="Repository remote URL. SSH and HTTPS forms resolve alike."),
        ] = None,
        workspace_path: Annotated[
            str | None,
            Field(
                description="Absolute path of the workspace. A hint only - paths differ per machine."
            ),
        ] = None,
        display_name: Annotated[
            str | None, Field(description="Human-readable name, used only on creation.")
        ] = None,
        create: Annotated[
            bool, Field(description="Create the project if nothing matches. Requires slug.")
        ] = False,
    ) -> ProjectOut:
        async with session_scope(session_factory) as session:
            ref = await project_service.use_project(
                session,
                project_id=_parse_uuid(project_id, "project_id") if project_id else None,
                slug=slug,
                git_remote=git_remote,
                workspace_path=workspace_path,
                display_name=display_name,
                create=create,
            )
            return ProjectOut.of(ref)

    @server.tool(
        name="memory_remember",
        title="Record a project memory",
        description=(
            "Record one durable piece of project knowledge so other clients and "
            "later sessions can retrieve it.\n\n"
            "Choose the type by behaviour, not by topic:\n"
            "- DECISION: a choice that was made. State the rejected alternative "
            "INSIDE the decision text - never store 'Redis was considered' as a "
            "separate memory, because a standalone fact can later be retrieved as "
            "though it were current.\n"
            "- CONSTRAINT: a rule the project must not violate.\n"
            "- FACT: a durable statement about the project.\n"
            "- TASK: short-lived working state ('currently implementing X'). "
            "Expires automatically within 30 days. Not an issue tracker.\n\n"
            "Record one self-contained statement per call - something that will "
            "still make sense to a different client in a month with no other "
            "context. Do not record conversational chatter.\n\n"
            "When a decision REPLACES an earlier one, pass the old memory's id in "
            "supersedes. The old fact stops appearing in search immediately, in the "
            "same transaction, so no client can ever see both as simultaneously "
            "true. It stays readable through memory_history. Do not instead record "
            "a new memory saying 'X is no longer true' - that leaves the original "
            "in circulation and adds a second thing to contradict it.\n\n"
            "If another client already recorded the same fact, you get that memory "
            "back with outcome='deduplicated' and your assertion is recorded as "
            "corroboration. That is a success, not a failure.\n\n"
            "NEVER record credentials, API keys, tokens, private keys or .env "
            "contents. This is not a secret store."
        ),
    )
    @domain_errors
    async def memory_remember(
        project_id: Annotated[str, Field(description="From project_use.")],
        type: Annotated[  # noqa: A002 - the MCP argument name is part of the API
            str, Field(description="DECISION, CONSTRAINT, FACT or TASK.")
        ],
        content: Annotated[
            str,
            Field(description="One self-contained statement, at most 8192 characters."),
        ],
        tags: Annotated[
            list[str] | None,
            Field(description="Up to 16 lowercase tags, e.g. ['queue','postgres']."),
        ] = None,
        importance: Annotated[
            int | None,
            Field(description="0-100. Defaults by type; raise it for load-bearing knowledge."),
        ] = None,
        supersedes: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Memory ids this assertion replaces. They stop appearing in "
                    "search immediately, in the same transaction as this write."
                )
            ),
        ] = None,
        source: Annotated[
            str | None,
            Field(description="Where this came from, e.g. 'architecture discussion'."),
        ] = None,
        client: Annotated[
            str,
            Field(description="Your client name, e.g. 'claude-desktop' or 'cursor'."),
        ] = "unknown",
        human_confirmed: Annotated[
            bool,
            Field(description="True if the user explicitly asked for this to be remembered."),
        ] = False,
        client_request_id: Annotated[
            str | None,
            Field(
                description=(
                    "Unique per logical request, 8-128 characters; a UUID is ideal. "
                    "Retrying with the same value returns the original result "
                    "instead of storing a second copy."
                )
            ),
        ] = None,
    ) -> RememberOut:
        async with session_scope(session_factory) as session:
            result = await memory_service.remember(
                session,
                _parse_uuid(project_id, "project_id"),
                memory_type=_parse_type(type),
                content=content,
                tags=tags,
                importance=importance,
                source=source,
                author_client=client,
                author_kind=(AuthorKind.HUMAN_CONFIRMED if human_confirmed else AuthorKind.AGENT),
                supersedes=(
                    [_parse_uuid(m, "supersedes") for m in supersedes] if supersedes else None
                ),
                client_request_id=client_request_id,
                embedding_model=embedder.model_name if embedder else None,
            )
            return RememberOut.of(result)

    @server.tool(
        name="memory_revise",
        title="Update an existing memory",
        description=(
            "Change the content of a memory you have already read.\n\n"
            "You must pass expected_revision - the revision_no you saw when you "
            "read it. If another client has changed the memory since then, your "
            "write is refused and you get back their version instead, with the "
            "revision number to retry with. This is not an error: it means "
            "someone else got there first and you would otherwise have silently "
            "erased their change.\n\n"
            "On a conflict, read the returned content, merge your change into it, "
            "and call again with the new expected_revision. Do not simply resend "
            "your original text.\n\n"
            "Use this to refine a fact that is still the same fact. If the fact "
            "has been replaced by a different one, that is supersession, not "
            "revision - support for it arrives in a later version.\n\n"
            "Pass client_request_id (any unique string, a UUID is ideal) so that "
            "retrying after a dropped connection replays the original result "
            "rather than reporting a confusing conflict against yourself."
        ),
    )
    @domain_errors
    async def memory_revise(
        project_id: Annotated[str, Field(description="From project_use.")],
        memory_id: Annotated[str, Field(description="The memory to change.")],
        expected_revision: Annotated[
            int,
            Field(
                description="The revision_no you read. The write fails if it has moved on.", ge=1
            ),
        ],
        content: Annotated[str, Field(description="The full new content, not a diff.")],
        tags: Annotated[
            list[str] | None, Field(description="Replaces the existing tags entirely.")
        ] = None,
        change_reason: Annotated[
            str | None, Field(description="Why this changed, e.g. 'clarified after review'.")
        ] = None,
        client: Annotated[str, Field(description="Your client name.")] = "unknown",
        client_request_id: Annotated[
            str | None,
            Field(description="Unique per logical request, 8-128 chars. Makes retries safe."),
        ] = None,
    ) -> ReviseOut:
        async with session_scope(session_factory) as session:
            result = await memory_service.revise(
                session,
                _parse_uuid(project_id, "project_id"),
                _parse_uuid(memory_id, "memory_id"),
                expected_revision=expected_revision,
                content=content,
                tags=tags,
                change_reason=change_reason,
                author_client=client,
                client_request_id=client_request_id,
                embedding_model=embedder.model_name if embedder else None,
            )
            return ReviseOut.of(result)

    @server.tool(
        name="memory_search",
        title="Search project memory",
        description=(
            "Retrieve memories recorded for a project, by any client, in any "
            "earlier session.\n\n"
            "Superseded, deleted and expired memories are never returned. What you "
            "get back is what is currently true, not everything that was ever "
            "said - so you can rely on a result without checking whether it has "
            "since been replaced.\n\n"
            "Filter by type and tags to narrow the set. The optional query is a "
            "substring match today; relevance ranking arrives in a later version."
        ),
    )
    @domain_errors
    async def memory_search(
        project_id: Annotated[str, Field(description="From project_use.")],
        query: Annotated[
            str | None, Field(description="Case-insensitive substring to match in content.")
        ] = None,
        types: Annotated[
            list[str] | None, Field(description="Restrict to these memory types.")
        ] = None,
        tags: Annotated[
            list[str] | None, Field(description="Only memories carrying ALL of these tags.")
        ] = None,
        limit: Annotated[int, Field(description="Maximum results, 1-100.", ge=1, le=100)] = 10,
    ) -> SearchOut:
        async with session_scope(session_factory) as session:
            parsed_project = _parse_uuid(project_id, "project_id")
            parsed_types = [_parse_type(t) for t in types] if types else None

            # Hybrid only when there is both a query to embed and an embedder to
            # embed it with. Browsing without a query has nothing to be similar
            # to, and an unconfigured embedder means full-text is the whole
            # search rather than half of a broken one.
            if embedder is not None and query:
                result = await retrieval_service.hybrid_search(
                    session,
                    parsed_project,
                    query=query,
                    embedder=embedder,
                    types=parsed_types,
                    tags=tags,
                    limit=limit,
                )
            else:
                result = await memory_service.search(
                    session,
                    parsed_project,
                    query=query,
                    types=parsed_types,
                    tags=tags,
                    limit=limit,
                )
            return SearchOut.of(result)

    @server.tool(
        name="memory_context",
        title="Get a project brief within a token budget",
        description=(
            "Get the most useful project knowledge that fits in a given number of "
            "tokens. Use this at the start of a session, before you know what you "
            "will be asked, or when you need background rather than one specific "
            "fact.\n\n"
            "This is not search. Search answers 'what matches this query'; this "
            "answers 'given this much room, what is worth knowing'. It balances "
            "memory types so constraints are never crowded out by chatter, drops "
            "near-duplicates rather than repeating the same decision three times, "
            "and returns a readable brief alongside the structured records.\n\n"
            "Pass a query to focus the brief on a topic; leave it out for a "
            "general overview ordered by importance.\n\n"
            "The token count is an estimate: this server does not know which model "
            "you are, so it is biased to over-estimate. The budget is never "
            "exceeded and may be under-filled by around 10%. The response reports "
            "what was spent and what was dropped, so you can ask for more if the "
            "brief looks thin."
        ),
    )
    @domain_errors
    async def memory_context(
        project_id: Annotated[str, Field(description="From project_use.")],
        query: Annotated[
            str | None,
            Field(description="Focus the brief on a topic. Omit for a general overview."),
        ] = None,
        token_budget: Annotated[
            int,
            Field(
                description="How many tokens the brief may cost. 2000 is a sensible default.",
                ge=100,
                le=32000,
            ),
        ] = 2000,
    ) -> ContextOut:
        async with session_scope(session_factory) as session:
            built = await context_service.build_context(
                session,
                _parse_uuid(project_id, "project_id"),
                query=query,
                token_budget=token_budget,
                embedder=embedder,
            )
            return ContextOut.of(built)

    @server.tool(
        name="memory_forget",
        title="Retire a memory",
        description=(
            "Stop a memory appearing in search, because it is no longer relevant "
            "or should not have been recorded.\n\n"
            "This tombstones, it does not destroy. Every revision stays readable "
            "through memory_history, so the record of what the project once "
            "believed survives. Calling it twice is harmless.\n\n"
            "Use this when a fact has simply stopped mattering. If it was replaced "
            "by a *different* fact, record the new one with supersedes instead - "
            "that keeps the connection between old and new, which forgetting loses.\n\n"
            "If a credential was recorded by mistake, forgetting is not enough: it "
            "hides the content but does not erase it. Tell the user to run the "
            "operator purge command. Permanent erasure is deliberately not "
            "available through this interface."
        ),
    )
    @domain_errors
    async def memory_forget(
        project_id: Annotated[str, Field(description="From project_use.")],
        memory_id: Annotated[str, Field(description="The memory to retire.")],
        reason: Annotated[
            str | None, Field(description="Why, e.g. 'the feature was removed'.")
        ] = None,
        client: Annotated[str, Field(description="Your client name.")] = "unknown",
    ) -> ForgetOut:
        async with session_scope(session_factory) as session:
            result = await memory_service.forget(
                session,
                _parse_uuid(project_id, "project_id"),
                _parse_uuid(memory_id, "memory_id"),
                reason=reason,
                author_client=client,
            )
            return ForgetOut.of(result)

    @server.tool(
        name="memory_history",
        title="Inspect a memory's full record",
        description=(
            "Show everything known about one memory, including memories that "
            "search deliberately hides.\n\n"
            "Returns every revision in order, which memory replaced this one (or "
            "which ones it replaced), which clients independently asserted it, and "
            "a recent audit trail.\n\n"
            "Use it to answer 'why does the project do it this way now?' or 'what "
            "did we believe before?'. Retired memories remain fully readable here - "
            "they leave retrieval, never the record - so this is how you explain a "
            "change rather than merely observe it."
        ),
    )
    @domain_errors
    async def memory_history(
        project_id: Annotated[str, Field(description="From project_use.")],
        memory_id: Annotated[str, Field(description="The memory to inspect. May be retired.")],
    ) -> HistoryOut:
        async with session_scope(session_factory) as session:
            record = await memory_service.history(
                session,
                _parse_uuid(project_id, "project_id"),
                _parse_uuid(memory_id, "memory_id"),
            )
            return HistoryOut.of(record)

    @server.resource(
        "memory://projects",
        name="projects",
        title="Known projects",
        description=(
            "Every project namespace on this server. Read-only, identity-addressed "
            "and cacheable, which is why it is a resource rather than a tool."
        ),
        mime_type="application/json",
    )
    async def list_projects() -> str:
        async with session_scope(session_factory) as session:
            projects = await ProjectRepository(session).list_all()
            return json.dumps(
                [
                    {
                        "project_id": str(p.id),
                        "slug": p.slug,
                        "display_name": p.display_name,
                    }
                    for p in projects
                ],
                indent=2,
            )

    @server.resource(
        "memory://memories/{memory_id}/history",
        name="memory-history",
        title="A memory's full record",
        description=(
            "Revisions, supersession lineage, attestations and audit trail for one "
            "memory - including retired ones that search deliberately hides."
        ),
        mime_type="application/json",
    )
    async def read_memory_history(memory_id: str) -> str:
        parsed = _parse_uuid(memory_id, "memory_id")
        async with session_scope(session_factory) as session:
            owner = await _owning_project(session, parsed)
            record = await memory_service.history(session, owner, parsed)
            return HistoryOut.of(record).model_dump_json(indent=2)

    @server.resource(
        "memory://memories/{memory_id}",
        name="memory",
        title="A single memory",
        description=(
            "Read one memory at its current revision. Identity-addressed and "
            "side-effect free, which is why this is a resource rather than a tool."
        ),
        mime_type="application/json",
    )
    async def read_memory(memory_id: str) -> str:
        parsed = _parse_uuid(memory_id, "memory_id")
        async with session_scope(session_factory) as session:
            owner = await _owning_project(session, parsed)
            view = await memory_service.get_memory(session, owner, parsed)
            if view is None:
                raise ResourceNotFoundError(f"No active memory with id {memory_id}.")
            return MemoryOut.of(view).model_dump_json(indent=2)

    return server


async def _owning_project(session: AsyncSession, memory_id: uuid.UUID) -> uuid.UUID:
    """Resolve which project a memory belongs to.

    Resource URIs carry no project, unlike every tool. That is safe here for two
    reasons: a memory id is an unguessable UUID, and the project it resolves to
    is then used as the scope for the actual read - so a caller still cannot
    reach across a project boundary, they can only read a memory whose id they
    already hold.

    Raising ``ResourceNotFoundError`` rather than ``ToolError``: a missing
    resource is a ``-32602`` protocol error, which is the correct shape for a
    URI that does not resolve. ``ToolError`` belongs to tool calls.
    """
    owner = (
        await session.execute(select(Memory.project_id).where(Memory.id == memory_id))
    ).scalar_one_or_none()
    if owner is None:
        raise ResourceNotFoundError(f"No memory with id {memory_id}.")
    return uuid.UUID(str(owner))
