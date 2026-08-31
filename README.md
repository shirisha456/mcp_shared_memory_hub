# MCP Shared Memory Hub

A persistent, versioned memory service that lets multiple MCP-compatible AI clients share project
knowledge across sessions, with conflict-safe updates, provenance, hybrid retrieval, stale-memory
handling, and context-budgeted recall.

**Status: Milestone 0 — project foundation.** No memory storage yet. See
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

## Milestone 0 scope

| In | Out |
|---|---|
| Docker Compose (PostgreSQL 16 + pgvector image) | Any table — the first DDL is Milestone 1 |
| Typed settings, strict mypy, ruff | The MCP SDK dependency (Milestone 1, after verifying the current version) |
| JSON logging, stderr-only | Metrics and tracing (Milestone 2) |
| Async Alembic + round-trip and drift tests | Domain models |
| Template-database test harness | Embeddings, retrieval, ranking |

`0001_baseline` is intentionally empty: Milestone 0 proves the migration *pipeline*, not a schema.
The downgrade and drift tests therefore pass trivially today — they exist now so they are already
wired when Milestone 1 adds the first tables.

## Layout

```
src/memhub/
  config.py                    typed settings, validated at startup
  observability/logging.py     JSON logs to stderr (stdout is the JSON-RPC channel)
  persistence/base.py          declarative base + constraint naming convention
  persistence/engine.py        async engine, pool, server-side statement timeout
migrations/                    async Alembic
tests/                         unit + integration, real PostgreSQL
docs/architecture.md           the design, including what was deliberately not built
```

## License

MIT
