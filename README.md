# MCP Shared Memory Hub

A persistent, versioned memory service that lets multiple MCP-compatible AI clients share project
knowledge across sessions, with conflict-safe updates, provenance, hybrid retrieval, stale-memory
handling, and context-budgeted recall.

**Status: truth maintenance.** Six MCP tools over stdio, backed by PostgreSQL, with
compare-and-set updates, idempotent writes, deduplication and stale-memory suppression. See
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

Full setup for both clients, with verified config formats and troubleshooting, is in
[docs/clients.md](docs/clients.md). The short version — note the **absolute path**, since neither
client runs the server from your project directory and Cursor's config has no `cwd` field:

```json
{
  "mcpServers": {
    "memhub": {
      "command": "C:\\Users\\you\\mcp_shared_memory_hub\\.venv\\Scripts\\memhub-server.exe",
      "env": {
        "MEMHUB_DATABASE_URL": "postgresql+asyncpg://memhub:memhub@localhost:5435/memhub"
      }
    }
  }
}
```

Claude Desktop reads `%APPDATA%\Claude\claude_desktop_config.json`; Cursor reads
`~/.cursor/mcp.json` or `.cursor/mcp.json`, and additionally wants `"type": "stdio"`.

Point both at the **same** `MEMHUB_DATABASE_URL`. Each client spawns its own server process, and
those processes share nothing else — no memory, no cache, no files. Different URLs and everything
still appears to work while each client quietly keeps a private corpus.

## Tool surface

| Tool | Purpose |
|---|---|
| `project_use` | Resolve or explicitly create a project namespace. Never creates implicitly. |
| `memory_remember` | Record one durable piece of project knowledge. |
| `memory_revise` | Update a memory, guarded by `expected_revision`. A conflict returns the winning version, not an error. |
| `memory_forget` | Tombstone a memory. Reversible; content is never destroyed. |
| `memory_search` | Retrieve active memories. Superseded, deleted and expired are never returned. |
| `memory_history` | Full record for one memory, including retired ones: revisions, lineage, attestations, audit. |

Plus three read-only resources — identity-addressed and side-effect free, which is what makes them
resources rather than tools:

```
memory://projects                        every project namespace
memory://memories/{memory_id}            one memory at its current revision
memory://memories/{memory_id}/history    full record, including retired memories
```

The tool manifest — names, titles, descriptions, schemas — is snapshotted to
[`tests/protocol/manifest.json`](tests/protocol/manifest.json) and asserted on every run. Tool
descriptions are the prompt that steers the model, so a wording change alters behaviour with no
logic change; the snapshot makes that show up in review as a diff.

`memory_context` arrives with the context-budget milestone. Supersession is deliberately not a
seventh tool: retiring a fact and asserting its replacement are one atomic act, so
`memory_remember` takes a `supersedes` argument.

## Milestone status

| # | Milestone | State |
|---|---|---|
| 0 | Skeleton: Docker, Alembic, logging, test harness, CI | done |
| 1 | Projects, memories, immutable revisions, 3 MCP tools over stdio | done |
| 2 | Compare-and-set revise, idempotency, audit log, metrics | done |
| 3 | Deduplication, attestations, supersession, forget, history | done |
| 4 | Claude Desktop / Cursor integration, golden manifest | done |
| 5 | Full-text retrieval | next |
| 6 | Evaluation harness (before vectors, deliberately) | |
| 7 | pgvector, embedding outbox, hybrid RRF ranking | |
| 8 | Context builder under a token budget | |
| 9 | Failure injection, benchmarks, `EXPLAIN ANALYZE` | |

### Concurrency

Each MCP client runs its own server process. They share no memory, so PostgreSQL is the only thing
that can adjudicate a conflicting write. `memory_revise` performs a single-statement compare-and-set
at `READ COMMITTED`:

```sql
UPDATE memories SET current_revision_no = current_revision_no + 1
 WHERE id = :id AND project_id = :pid AND current_revision_no = :expected AND status = 'ACTIVE'
```

Zero rows means another writer got there first. The correctness argument is `EvalPlanQual`: when the
losing transaction unblocks, PostgreSQL walks to the newest committed row version and re-evaluates
this `WHERE` against it, so the read and the write are the same statement and there is no window to
lose. `READ COMMITTED` is chosen *for* these semantics — `SERIALIZABLE` would raise `40001` and turn
49 clean refusals into 49 retries.

`tests/concurrency/` proves it: 50 writers aligned on a barrier, exactly 1 success and 49 conflicts,
then the invariant suite. The fixture asserts the pool can supply 50 distinct backends first,
because a 50-way test against a 10-connection pool measures five sequential waves and passes for the
wrong reason. Removing the version predicate from the SQL makes those tests fail — verified.

### Idempotency is not deduplication

Idempotency is *one* client retrying *the same request*, keyed on a caller-supplied
`client_request_id`; the retry replays the original response. Deduplication is *two* clients
asserting *the same fact*, keyed on a content hash; that arrives next.

The claim is `INSERT ... ON CONFLICT DO NOTHING` — "check then insert" races. If another transaction
holds the key uncommitted, `SELECT ... FOR SHARE` blocks until it resolves: a row means it committed
and its response is replayed, no row means it rolled back and the key is free. A key reused with a
*different* payload is refused rather than silently answering a question the caller never asked.

Note that `memory_revise` does not *need* a key for correctness — the compare-and-set already makes
a duplicate write impossible. The key is there so a retry after a dropped connection replays cleanly
instead of reporting a conflict against yourself.

### Stale-memory suppression

The problem the project exists for. A project once used Redis as its queue and now uses PostgreSQL.
Both statements were true when written; only one is true now. A retrieval-only system returns both
and lets similarity decide — and similarity has no opinion about which is current, so the stale
phrasing often wins because it matches the query *better*.

Recording the replacement with `supersedes` retires the old fact in the same transaction. It leaves
retrieval immediately and stays fully readable through `memory_history`, with a link to what
replaced it and who wrote it.

The assertion that matters in `tests/integration/test_stale_memory.py` is not "the right answer
ranks first" — it is **"the wrong answer is absent at every limit"**, checked across limits 1 to 100
and several queries including `redis` itself. Ranking can bury a stale fact; only structure can
exclude it. Removing the status condition from the stage-0 filter makes five of those tests fail —
verified by mutation.

### Deduplication is not idempotency

| | Idempotency | Deduplication |
|---|---|---|
| Trigger | one client retries the same request | two clients assert the same fact |
| Key | caller-supplied `client_request_id` | normalised content hash |
| Answer | replay the original response | return the existing memory, record corroboration |

A deduplicated write is evidence, not a nuisance: when Cursor states what Claude Desktop already
stored, that second independent assertion is recorded as an attestation, and
`COUNT(DISTINCT client_name)` becomes a ranking prior later. Counted per client, so one client
retrying in a loop cannot manufacture corroboration.

The dedup key lives in its own table rather than as a partial unique index, because the rule spans
two tables — `is_current` on the revision, `status` on the memory — and no index can. Retirement
releases the key, so a sentence can be legitimately re-asserted if a decision is reversed.

### What is deliberately not built yet

Search is substring matching with a deterministic total order. No relevance ranking, because
Milestone 6 builds the evaluation harness that can prove ranking is an improvement — and building
the measurement after the optimisation means fitting the metric to the conclusion.

Metrics are an in-process registry that enforces the label-cardinality rule (`memory_id` and
`project_id` are refused as labels). There is no OTLP exporter: the server is a short-lived
subprocess, so pull-based scraping cannot find it and push needs a collector. That is Milestone 9.

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
