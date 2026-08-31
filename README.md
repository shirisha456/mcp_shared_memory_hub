# MCP Shared Memory Hub

A persistent, versioned memory service that lets multiple MCP-compatible AI clients share project
knowledge across sessions, with conflict-safe updates, provenance, hybrid retrieval, stale-memory
handling, and context-budgeted recall.

**Status: hybrid retrieval.** Six MCP tools over stdio, backed by PostgreSQL, with
compare-and-set updates, idempotent writes, deduplication, stale-memory suppression, and
keyword + vector search fused by rank. See
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
| 5 | Full-text retrieval | done |
| 6 | Evaluation harness (before vectors, deliberately) | done |
| 7 | pgvector, embedding outbox, hybrid RRF ranking | done |
| 8 | Context builder under a token budget | next |
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

### Retrieval quality is measured, not asserted

200-memory corpus, 34 queries with graded relevance judgments written **before** anything was
measured. Full results in [`docs/eval/results.md`](docs/eval/results.md); the numbers are gated
against a committed baseline, so a regression fails the build rather than going unnoticed.

| Strategy | nDCG@10 | Recall@10 | Precision@10 | Stale inclusion |
|---|---|---|---|---|
| full-text, all terms required | 0.478 | 0.468 | 0.484 | **0.000** |
| full-text, any-term fallback | 0.802 | 0.817 | 0.691 | **0.000** |

The harness immediately found a real defect. PostgreSQL joins bare query terms with AND, so
"connection pool size" demands all three lexemes and misses a memory saying "connection pooling is
bounded at 10" — questions as ordinary as "migration rules" returned *nothing*. A query that finds
nothing now retries with any-term matching, which is where the jump came from. Precision holds
because the widening only happens when the alternative is an empty result.

**Stale inclusion stayed at exactly 0.000 through both.** That is the useful part: loosening the
match did not loosen the correctness guarantee, because suppression is structural rather than a
ranking effect. Several queries are built to punish a similarity-only system — `q02` asks for
"redis" when the *retired* memory is about Redis and the current one mentions it only to say it was
removed.

Three queries still score zero, and all three are vocabulary gaps rather than bugs: `jwt` does not
match `JWTs` (Snowball does not stem acronym plurals), "deadlock prevention" misses a memory that
describes deadlock prevention without using the word, and "worked on right now" misses "Currently
implementing". That is the concrete target for semantic retrieval — measured first, so the claim
that it helps will be a number.

### Hybrid retrieval

Full-text and pgvector run over the same stage-0 filter and are fused by **Reciprocal Rank Fusion**
— position, not score. `ts_rank_cd` is unbounded and corpus-dependent; cosine distance is in [0, 2].
Adding them is meaningless, and normalising per query is worse: it scales a mediocre best match to
1.0 exactly like a perfect one. RRF also degrades cleanly — if the outbox is behind, those documents
simply do not appear in that ranking and contribute nothing.

| Strategy | nDCG@10 | Recall@10 | Precision@10 | Stale |
|---|---|---|---|---|
| full text, all terms required | 0.478 | 0.468 | 0.484 | **0.000** |
| full text, any-term fallback | 0.803 | 0.817 | 0.691 | **0.000** |
| hybrid, RRF, distance ≤ 0.35 | **0.853** | **0.828** | 0.671 | **0.000** |

The `jwt` query went from 0.000 to a perfect 1.000 — the stemmer never matched `JWTs`, and meaning
does. `deadlock prevention` is still 0.000: the memory describes deadlock prevention without using
the word, and 384 dimensions of a small model do not bridge that.

**The threshold is the part worth reading about.** Without one, hybrid scored nDCG 0.881 — and
precision collapsed to **0.113**, with every unanswerable query returning ten results. Approximate
nearest neighbour search returns the *k* closest vectors whether or not anything is close. 0.35 was
chosen by sweeping against the corpus; the full table and what it costs are in
[`docs/eval/threshold-sweep.md`](docs/eval/threshold-sweep.md). Reporting the 0.881 and stopping
would have been true and badly misleading.

### Embedding is asynchronous, by a transactional outbox

Generating a vector inline would hold the `memories` row lock across a slow, fallible call — so one
slow inference blocks every other writer, and a model being down becomes a *write* outage. Enqueuing
after the transaction is the classic dual-write bug: crash between commit and enqueue and the memory
exists forever with no vector and nothing knows.

So the job row is inserted **in the same transaction as the revision** — both exist or neither does.
A worker claims batches with `FOR UPDATE SKIP LOCKED`, which is what lets a worker in each client's
server process drain one queue without contending. There's a test asserting four concurrent workers
process exactly 40 jobs between them.

The result is eventual consistency with an **explicit, queryable** pending state. Every search
response carries `semantic_coverage`, so a caller can tell "the semantic half saw everything" from
"it saw 60%". Failures back off exponentially and end as `DEAD` with the reason recorded — never a
silent hole in the index.

This project uses PostgreSQL as a durable job queue, which is the architectural decision used as the
running example throughout its own documentation, and the reason Redis is not a dependency.

### Running it with semantic search

```bash
pip install -e ".[local-embeddings]"
```

Then set `MEMHUB_EMBEDDING_ADAPTER=local`. Defaults to `none`, so a fresh install works with no model
download and search is full-text only — a server that downloads a model on first use is a server
that fails to start without a network, for a feature meant to be an enhancement.

CI uses a deterministic hash embedder that carries **no semantic signal**. It exists to exercise the
outbox, the vector column, fusion and coverage reporting hermetically. It cannot measure quality, and
the harness does not let it try: the numbers above come from `BAAI/bge-small-en-v1.5` run locally.

### What is deliberately not built yet

### Retrieval

Full-text over a `tsvector` generated column with a partial GIN index, ranked by `ts_rank_cd` scaled
by three priors: importance, a **type-dependent recency half-life** (a TASK is worthless after a
month; a DECISION from a year ago may be the most important thing in the corpus), and a small type
weight. Priors are multiplicative, so a memory that does not match the query scores zero however
important it is.

The weights are **untuned and labelled as such**. Milestone 6 builds the evaluation harness; tuning
them now would mean fitting numbers to intuition and then building the ruler that agrees.

Two findings worth keeping:

- `is_current IS TRUE` makes the partial index **unusable** — PostgreSQL cannot prove it implies
  `WHERE is_current`, because `IS TRUE` is null-safe and therefore a different expression. The bare
  column works. Guarded by a test on the compiled SQL, since neither a latency test nor a plan
  assertion catches it reliably.
- The English stemmer maps `queue`/`queues`/`queueing` to one lexeme but `queued` to another, so
  that query misses. Pinned as a failing case rather than described in prose — it is the honest
  argument for semantic retrieval, and Milestone 6 will measure what it costs.

Measured at 10k memories (`docs/perf/`): **1.8–5.5 ms** inside PostgreSQL. Client-observed p50 is
7–17 ms; the gap is Docker Desktop port forwarding, not query cost, which is why the benchmark
asserts both separately rather than hiding the overhead in one number.

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
