# MCP Shared Memory Hub

A persistent, versioned memory service that lets multiple MCP-compatible AI clients share project
knowledge across sessions, with conflict-safe updates, provenance, hybrid retrieval, stale-memory
handling, and context-budgeted recall.

**Status: Milestone 1 — memory persistence + MCP slice.** Three MCP tools over stdio,
backed by PostgreSQL. See
[`docs/architecture.md`](docs/architecture.md) for the full design and
[the roadmap](docs/architecture.md#15-revised-roadmap) for what lands when.

---

## What this is, precisely

Claude Desktop and Cursor each spawn their **own** copy of this server as a subprocess. The two
processes share no memory and no cache — PostgreSQL is the only shared state between them. That
constraint is what makes the concurrency control in this project real rather than decorative.

An MCP server receives **the arguments of the tool calls made to it**. It does not receive
transcripts. This is therefore a *shared memory system* — clients explicitly record what is worth
keeping — and not a chat-history synchronisation system. Nothing in this repository will ever claim
otherwise.

## What it is not

Not a chatbot, not a RAG platform, not a vector-database wrapper, not a notes CRUD app, not an
issue tracker, not an MCP gateway, not an agent control plane.

## Engineering focus

Schema design and database-enforced invariants · optimistic concurrency control via single-statement
compare-and-set · idempotent writes · immutable revisions with supersession · staged retrieval with
a measured evaluation harness · an explicit failure model · real-PostgreSQL testing.

---

## Quick start

Start PostgreSQL (the image bundles pgvector, which Milestone 7 will need):

```bash
docker compose up -d --wait
```

Install the package with development dependencies:

```bash
pip install -e ".[dev]"
```

Apply migrations:

```bash
alembic upgrade head
```

Run the checks:

```bash
ruff check . && ruff format --check . && mypy && pytest -v
```

Integration tests skip with an actionable message if PostgreSQL is unreachable. In CI,
`MEMHUB_REQUIRE_DB=1` turns that skip into a failure so a broken service container cannot be
mistaken for a green build.

## Configuration

Every setting is declared in [`src/memhub/config.py`](src/memhub/config.py); nothing reads
`os.environ` directly. Override via environment variables prefixed `MEMHUB_`, or a `.env` file —
see [`.env.example`](.env.example).

## Connecting a client

Add to your MCP client configuration (Claude Desktop, Cursor):

```json
{
  "mcpServers": {
    "memhub": {
      "command": "memhub-server",
      "env": { "MEMHUB_DATABASE_URL": "postgresql+asyncpg://memhub:memhub@localhost:5435/memhub" }
    }
  }
}
```

Each client spawns its **own** server process. They share nothing but PostgreSQL.

## Tool surface

| Tool | Purpose |
|---|---|
| `project_use` | Resolve or explicitly create a project namespace. Never creates implicitly. |
| `memory_remember` | Record one durable piece of project knowledge. |
| `memory_search` | Retrieve active memories. Superseded, deleted and expired are never returned. |

Plus one read-only resource, `memory://memories/{memory_id}`.

`memory_revise`, `memory_forget`, `memory_history` and `memory_context` arrive with the milestones
that give them something to do.

## Milestone status

| # | Milestone | State |
|---|---|---|
| 0 | Skeleton: Docker, Alembic, logging, test harness, CI | done |
| 1 | Projects, memories, immutable revisions, 3 MCP tools over stdio | done |
| 2 | Compare-and-set revise, idempotency, audit log, metrics | next |
| 3 | Deduplication, attestations, supersession, forget, history | |
| 4 | Claude Desktop / Cursor integration, golden manifest | |
| 5 | Full-text retrieval | |
| 6 | Evaluation harness (before vectors, deliberately) | |
| 7 | pgvector, embedding outbox, hybrid RRF ranking | |
| 8 | Context builder under a token budget | |
| 9 | Failure injection, benchmarks, `EXPLAIN ANALYZE` | |

### What Milestone 1 deliberately does not do

Revision, supersession, deduplication and idempotency are **schema-ready but not implemented**.
`memories` carries `superseded_by_id` and `current_revision_no`; `memory_revisions` is append-only
with a partial unique index enforcing one current revision. Those constraints exist now because
splitting a table definition across three migrations is worse engineering than writing it once —
but nothing exercises them yet.

`content_hash` is computed on every write despite deduplication being Milestone 3, because the
column is `NOT NULL` on an append-only table and backfilling it later would need a data migration.

Search is substring matching with a deterministic total order. No relevance ranking, because
Milestone 6 builds the evaluation harness that can prove ranking is an improvement — and building
the measurement after the optimisation means fitting the metric to the conclusion.

## Layout

```
src/memhub/
  domain/          pure types, policy, validation, normalisation — no I/O
  services/        transactions, invariants, policy — no MCP awareness
  persistence/     ORM models, repositories (every method requires a project scope)
  retrieval/       filters.py — the stage-0 filter, written once
  mcp/             thin handlers, schemas, error mapping, stdio entry point
  observability/   JSON logs to stderr (stdout is the JSON-RPC channel)
migrations/        async Alembic
tests/             unit, integration, protocol — real PostgreSQL throughout
docs/architecture.md   the design, including what was deliberately not built
```

## Invariants enforced by the database

Nine of the fourteen invariants in the architecture document are schema-level, so they survive a bug
in the service layer. `tests/integration/test_invariants.py` proves each one by bypassing the
service layer and attempting the forbidden write directly:

- at most one current revision per memory (partial unique index)
- revision numbers unique per memory (composite primary key)
- supersession cannot cross a project boundary (composite foreign key)
- a memory cannot supersede itself
- `SUPERSEDED` requires both a timestamp and a target
- every `TASK` has an expiry

## License

MIT
