# MCP Shared Memory Hub — Architecture

**Status:** v0.2 — **LOCKED**. Implementation in progress (Milestone 0).
Changes to this document from here require an implementation finding that reveals a genuinely missing decision. See §19.
**Audience:** the author, and interviewers reading the repo.

---

## 0. Reader's guide: what I changed about your spec

You asked me to be critical. Before the design, here is everything I am pushing back on, so you can reject it before we build on it.

| # | Your spec | My recommendation | Why |
|---|-----------|-------------------|-----|
| 1 | 8 memory types (`DECISION FACT CONSTRAINT TASK OBSERVATION BUG SOLUTION TEMPORARY_CONTEXT`) | **4 types**: `DECISION`, `CONSTRAINT`, `FACT`, `TASK` | A type must change *system behaviour* (default TTL, ranking band, context quota). If it does not, it is a tag. `OBSERVATION` is a provenance/trust distinction, `BUG`/`SOLUTION` are `FACT`s with tags, `TEMPORARY_CONTEXT` is any type with an `expires_at`. |
| 2 | `EXPIRED` as a stored status | **Derived predicate**, not stored state | A stored `EXPIRED` needs a sweeper to be true; between expiry and sweep the column *lies*. `expires_at IS NULL OR expires_at > now()` is always correct with zero moving parts. |
| 3 | "revision vs supersedes — pick one" | **Both. They are different concepts.** | *Revision* = same logical fact, refined (v1 to v2). *Supersession* = a **different** memory retires this one (memory #7 "Redis is the queue" is retired by memory #19 "Postgres is the queue"). Forcing #19 to be a revision of #7 destroys authorship, timestamps, and the many-to-one shape (one new memory can retire several old ones). |
| 4 | `memory_supersede` as its own tool | **Folded into `memory_remember(supersedes=[...])`** | Superseding *is* "assert a new fact that retires old ones". One atomic call, one transaction, smaller tool surface, no window where the old fact is retired but the new one does not exist yet. |
| 5 | `memory_forget` with hard delete | **Tombstone only over MCP. Hard purge is an operator CLI command.** | An irreversible destructive operation should not sit in a language model's tool surface. Purge exists (you need it when a secret gets stored) but it is a human-invoked, audited path. |
| 6 | `score = lexical + semantic + importance + recency` | **Reciprocal Rank Fusion**, then a small number of interpretable priors | You already spotted that summing unrelated scales is wrong. Min-max normalisation is *also* wrong here — it is corpus- and query-dependent, so scores are not comparable across queries. RRF works in rank space and is scale-free by construction. |
| 7 | "MRR or another justified ranking metric" | **nDCG@10 primary**, Recall@10 secondary, MRR dropped | MRR only credits the *first* relevant hit. Our queries have several relevant memories at different degrees of relevance. Graded relevance + multiple relevant docs = nDCG. |
| 8 | Observability at Phase 9 | **Structured logging + core counters at Phase 2** | You cannot debug a 50-way concurrency test or explain a p99 without them. Dashboards can wait; instrumentation cannot. |
| 9 | MCP tools/clients at Phase 4 | **Thin end-to-end MCP slice in Phase 1** | The MCP layer and client config hold the most unknown-unknowns. De-risk the part you do not control before building three phases of backend on assumptions about it. |
| 10 | Retrieval evaluation at Phase 10 | **Eval harness before pgvector** | If you build hybrid retrieval and *then* build the measurement, you will unconsciously fit the metric to the conclusion. Build the ruler first, then prove hybrid beats FTS. |
| 11 | Supersession/retention at Phase 8 | **Phase 3, with revisions** | Stale-memory suppression is the thesis of the project. It cannot be a late add-on. |
| 12 | "50 concurrent clients" | **50 concurrent *callers* against the service layer** | With stdio there are realistically 2 MCP client processes. The 50-way test is a synthetic load test of the write path. It is a completely valid correctness test — just do not let the README imply 50 MCP clients. |
| 13 | Native PostgreSQL `ENUM` types (implied) | **`text` + `CHECK`** | `ALTER TYPE ... ADD VALUE` is awkward under Alembic and value *removal* is impossible. A `CHECK` constraint is trivially alterable in a migration. Negligible storage cost at this scale. |

Everything else in your spec I am adopting, several parts of it verbatim.

---

## 1. Problem definition

### 1.1 What we are solving

A developer works on one codebase across several MCP-capable clients (Claude Desktop, Cursor, others) and across many sessions. Durable project knowledge — the decisions, the constraints, the rejected alternatives, the load-bearing facts — lives only in whichever transcript happened to produce it. It is not addressable, not shared, and not *maintained*.

The system under design is a **multi-writer, versioned knowledge store** with two hard properties:

1. **Write correctness under concurrency.** Several independent client processes write to the same logical facts with no coordination between them and no shared memory. Lost updates must be impossible, not unlikely.
2. **Read consistency over a self-contradictory corpus.** Knowledge about a live codebase contradicts itself over time. "Redis is the queue" and "Postgres is the queue" are both *true statements that were made*; only one is *currently valid*. The read path must return the current one and must not present the retired one as an equal peer.

Framed for an interview in one sentence:

> The hard problem is not recall. It is **truth maintenance in a multi-writer store whose read path has a hard budget constraint.** That is a database and distributed-systems problem; the AI clients are just its users.

### 1.2 What we are explicitly NOT solving

- **Not chat-history synchronisation.** MCP gives a server the arguments of the tool calls made to it. Nothing more. The server never sees a transcript. This is stated in the README, in the tool descriptions, and in this document, and it must never drift.
- **Not automatic ingestion.** No "save every message". See §11.
- **Not a general RAG platform.** The corpus is small (10^2 to 10^5 items), curated, and structured.
- **Not a knowledge graph.** We have a bounded set of typed links, not arbitrary entity/relation extraction.
- **Not a task tracker.** `TASK` exists so a second client knows what is in flight, with a mandatory TTL. If it starts growing assignees, states, and boards, we cut the type. (Watch item.)
- **Not multi-tenant.** One developer, one machine, V1. Remote hosting is an explicit later phase that brings its own auth design.
- **Not a secret store.** Actively hostile to becoming one — see §10.

### 1.3 Why this instead of a notes table, or a vector store

**Why not a notes database** (Postgres table + `LIKE`, or Obsidian, or a wiki): no notion of a fact being *retired by* another fact, no compare-and-set on a logical unit, no provenance, no read path that answers "give me the most useful 2000 tokens", and no machine-callable protocol so the agent cannot participate. Every property that makes this project interesting is absent.

**Why not a vector store** (Pinecone, Qdrant, Chroma):

- Nearest-neighbour search over a corpus containing both "Redis is the queue" and "Postgres is the queue" returns **both**, ranked by similarity to the query — and similarity has no opinion about which is true. This is precisely the failure mode we exist to prevent.
- Vector stores have no transactions. Metadata (status, revision, supersession) and the vector would live in two systems, and the *only* way to guarantee "the vector index never returns a retired memory" is a distributed transaction across two stores, or acceptance of a permanent inconsistency window. Keeping vectors in the same database as the state machine means the filter and the ANN search commit together and are read under one snapshot.
- No compare-and-set, no unique constraints, no referential integrity.

**Why not SQLite + `sqlite-vec` + FTS5** — the strongest honest alternative, since this is a local tool. It would be *simpler*: zero infrastructure, single file, no Docker, no connection pool. If the only goal were "a personal tool that works", SQLite would probably win.

I am choosing PostgreSQL anyway, for reasons I will state plainly rather than dress up as scale:

1. **Multi-process concurrent writers are SQLite's weakest axis.** Two stdio server processes both writing is exactly the case where WAL-mode SQLite serialises writers and surfaces `SQLITE_BUSY`. The concurrency story becomes "retry until the lock frees" rather than "the database evaluated my predicate atomically and told me I lost".
2. **The correctness mechanisms I want to demonstrate are Postgres features**: partial unique indexes, composite foreign keys used to enforce namespace isolation, `FOR UPDATE SKIP LOCKED`, `EvalPlanQual` re-check semantics under `READ COMMITTED`, GIN + HNSW, `EXPLAIN ANALYZE`.
3. **It does not foreclose the remote phase.** Streamable HTTP with one shared server process needs a real server database.

That is the honest trade: paying operational complexity to buy a substrate where the interesting invariants are *expressible and testable*. That framing is worth more in an interview than "Postgres because it scales".

---

## 2. System boundary

```
   +----------------------+            +----------------------+
   |   Claude Desktop     |            |       Cursor         |
   |  (MCP host + model)  |            |  (MCP host + model)  |
   +----------+-----------+            +----------+-----------+
              | spawns                            | spawns
              | stdio subprocess                  | stdio subprocess
   ===========+===================================+==============  process boundary
              | JSON-RPC over stdin/stdout        |
   +----------v-----------+            +----------v-----------+
   |  memhub server #1    |            |  memhub server #2    |   <- two OS processes.
   |  (stateless)         |            |  (stateless)         |      NO shared memory.
   +----------+-----------+            +----------+-----------+      NO shared cache.
              |                                   |
              +----------------+------------------+
                               | asyncpg pool
                     +---------v----------+
                     |    PostgreSQL      |  <- the ONLY shared state.
                     |    + pgvector      |     The only synchronisation primitive.
                     +---------+----------+
                               |  outbox: SELECT ... FOR UPDATE SKIP LOCKED
                     +---------v----------+
                     | embedding worker   |  (in-process asyncio task per server)
                     |  -> EmbeddingPort  |---> local model / hosted API / deterministic fake
                     +--------------------+
```

### The single most important sentence in this document

**The two server processes share nothing but PostgreSQL.** No in-memory cache holds authoritative state. No process-local lock protects anything that matters. Every invariant in §5 is enforced by the database, because the database is the only component that can see both writers.

This is not an academic exercise: it is a direct consequence of stdio transport, where each MCP host spawns its **own** server subprocess. The concurrency work in this project is real precisely *because* the transport forces it.

### Layer responsibilities

| Layer | Owns | Must never |
|---|---|---|
| **MCP host** (Claude Desktop / Cursor) | Deciding *when* to call a tool, rendering results, holding the transcript | — |
| **MCP interface** (`memhub.mcp`) | Schema validation, mapping domain results to tool results/resources, error classification, request-id propagation | Contain SQL or business logic |
| **Service layer** (`memhub.services`) | Transactions, invariants, orchestration, idempotency, dedup, ranking policy | Know that MCP exists |
| **Persistence** (`memhub.persistence`) | ORM mapping, hand-written CAS SQL, connection pool, repositories that *require* a project scope | Decide policy |
| **Retrieval** (`memhub.retrieval`) | Lexical, semantic, fusion, ranking, context assembly | Mutate anything |
| **Embeddings** (`memhub.embeddings`) | A `Protocol` with fake / local / hosted implementations | Be required for a write to succeed |
| **PostgreSQL** | Durability, isolation, uniqueness, referential integrity, atomicity | — |

Everything in the service layer is testable by calling Python functions. No test needs an MCP client except the protocol tests in §12.

---

## 3. MCP design

### 3.1 Tool vs resource — the actual decision rule

The two primitives differ on **who initiates** and **whether the address is stable**:

- **Resources** are *application-driven*: the host fetches them and decides whether to put them in context. Stable URI, read-only, cacheable.
- **Tools** are *model-driven*: the model decides to invoke one with arguments it constructs.

The rule:

> **If the thing is addressed by identity, has no side effects, and the client can reasonably decide to attach it, it is a resource. If it takes a query the model composes, or it changes state, it is a tool.**

Applying it:

- Retrieval takes a natural-language query and a budget the model chooses. There is no stable URI for "the memories relevant to what I am about to ask". → **tool**.
- "The current brief for project X" *is* identity-addressed and stable. → **resource**, and a good one: a client can attach it at session start with no model turn spent.
- All mutation → **tool**, trivially.
- "Memory #abc and its history" is identity-addressed → naturally a **resource**.

**But** there is a practical constraint I will not paper over: resource fetching is client-mediated, and clients differ in whether the *model* can pull a resource on demand. Anything the model must reach autonomously has to be a tool. Therefore `memory_history` is a **tool**, and the identity-addressed reads are *additionally* exposed as resources for hosts and humans that support browsing. That duplication is a deliberate concession to client reality, documented rather than hidden.

**No MCP prompts.** We would only add them to claim primitive coverage. Cut. **No sampling, no elicitation** in V1 — the server has nothing to ask the model.

### 3.2 The tool surface (7 tools)

| Tool | Kind | Purpose |
|---|---|---|
| `project_use` | write (idempotent) | Resolve or explicitly create a project namespace. Returns the canonical `project_id`. |
| `memory_remember` | write | Assert a new memory. Handles dedup, idempotency, and `supersedes` in one transaction. |
| `memory_revise` | write (CAS) | Refine an existing logical memory. Requires `expected_revision`. |
| `memory_forget` | write | Tombstone a memory. Reversible. Never destroys content. |
| `memory_search` | read | Filtered, ranked retrieval. Active memories only by default. |
| `memory_context` | read | Budgeted project brief. The flagship. |
| `memory_history` | read | Revision lineage + supersession chain + provenance. The audit view. |

Deliberately absent: `memory_supersede` (folded into `remember`), any purge/hard-delete (operator CLI only), `memory_list` (that is `search` with no query), `project_create` (that is `project_use` with `create: true`), and any bulk/batch tool until measurements demand one.

### 3.3 Sketched signatures

```
project_use(
  slug?: str, project_id?: uuid,
  git_remote?: str, workspace_path?: str,
  create: bool = false
) -> { project_id, slug, display_name, created: bool }
```

Resolution order: `project_id`, then `slug`, then normalised `git_remote`, then `workspace_path`. Two candidates → **error listing both**, never a guess. No match with `create=false` → error instructing the caller to pass `create: true`. Silent auto-creation from a working directory is how a client opened in the wrong folder forks your memory in half.

```
memory_remember(
  project_id, type, content,
  tags?: [str], importance?: 0..100, expires_at?: ts,
  supersedes?: [memory_id],
  source?: str, author_kind?: "agent" | "human_confirmed",
  client_request_id?: str
) -> { memory_id, revision_no,
       outcome: "created" | "deduplicated" | "idempotent_replay",
       superseded: [memory_id], attestation_count }
```

```
memory_revise(project_id, memory_id, expected_revision, content,
              change_reason?, client_request_id?)

  ok       -> { memory_id, revision_no, previous_revision }

  conflict -> isError tool result (NOT a JSON-RPC error):
              { error: "REVISION_CONFLICT",
                expected: 4, current_revision: 5,
                current_content: "...",
                changed_by: "cursor", changed_at: "...",
                guidance: "Re-read current content, retry with expected_revision=5." }
```

```
memory_search(project_id, query?, types?, tags?, limit=10,
              include_superseded=false, include_expired=false)
  -> { results: [{memory_id, revision_no, type, content, score, why:{...}}],
       semantic_coverage: 0.0..1.0, degraded?: "fts_only" }

memory_context(project_id, query?, token_budget=2000, focus?)
  -> { brief: str, items: [...],
       budget: { requested, estimated_used, utilisation, estimator },
       selection: { considered, selected, dropped_by_reason: {...} } }

memory_history(project_id, memory_id)
  -> { revisions: [...], supersedes: [...], superseded_by: {...},
       attestations: [...], audit: [...] }
```

### 3.4 Two MCP design points worth an interview

**(a) Conflicts are tool results, not protocol errors.** A JSON-RPC error means *the request was invalid or the server broke*. A version conflict means *the request was well-formed and the domain said no*. Returning it as a protocol error hides it from the model, which then cannot recover. We return `isError: true` with a **structured payload containing the current revision and content**, so the model has everything it needs to merge and retry in a single round trip. Malformed input, unknown project, and internal failure get protocol-level errors.

**(b) Tool descriptions are production surface, not documentation.** The description string is what steers the model. Ours carry the operating contract:

- "Never store credentials, API keys, or `.env` contents."
- "When recording a decision, state the rejected alternative *inside* the decision. Do not store 'Redis was considered' as a standalone fact — a standalone fact can later be retrieved as if it were current."

These sentences are load-bearing for correctness and belong under test (§12, golden manifest snapshot).

### 3.5 Resources

```
memory://projects                          list of projects
memory://projects/{slug}/context           default query-less brief (cacheable)
memory://memories/{memory_id}              current revision + metadata
memory://memories/{memory_id}/history      full lineage
```

URI parsing is strict and allow-listed: `{slug}` must match the slug regex, `{memory_id}` must parse as a UUID, and the memory's `project_id` is re-checked on every read. No path traversal, no wildcards, no cross-project reads by construction.

### 3.6 Transport

**V1: stdio.** One server subprocess per host. Consequences that shape the design:

- **stdout is the protocol channel.** Any stray `print()` corrupts the JSON-RPC stream. All logs go to **stderr**, enforced by logger configuration and a lint rule. This is the most common way an MCP server silently breaks.
- The server process is short-lived and holds no authoritative state, so restart is a non-event.
- Pull-based Prometheus scraping does not fit ephemeral subprocesses → **OTLP push** (§11).

**Later: Streamable HTTP**, one shared process. That phase adds authentication, per-caller authorisation, and rate limiting as its own design work. The service layer does not change; only `memhub.mcp` gains a second entry point. Client configuration for Claude Desktop and Cursor will be verified against current official docs at integration time, not copied from tutorials.
---

## 4. Data model

### 4.1 The three identity questions

Before any DDL, three things must be pinned down, because everything else hangs off them.

**Project identity.** Identity is a **server-issued UUID**. Nothing else. `slug` is a human-facing unique key (stable, but renameable in principle). `git_remote` and `workspace_path` are **resolution aliases, not identity** — a workspace path differs per machine and per clone, and a remote can be re-pointed. Aliases live in their own table with a **globally unique** `(kind, value_norm)` index, so an alias can never resolve to two projects; ambiguity is impossible by construction rather than by careful code.

**Memory identity.** A `memories` row is the *logical fact*. It is mutable but only in its lifecycle columns (status, current revision pointer, supersession pointer). It never holds content.

**Revision identity.** `(memory_id, revision_no)` — the primary key of an **append-only** table that holds all content. Content is never updated in place and never deleted by normal operation.

That split is the whole model: **a mutable identity/lifecycle row pointing into an immutable content log.**

### 4.2 Revision vs supersession — why both exist

| | Revision | Supersession |
|---|---|---|
| Question it answers | "How did *this fact* change?" | "Which fact replaced *that* fact?" |
| Cardinality | 1 memory → N revisions | N old memories → 1 new memory |
| Authorship | same logical fact, possibly different authors per revision | different memories, different authors, different creation times |
| Example | v1 "Postgres is the queue" → v2 "Postgres is the queue; `tasks` table, `FOR UPDATE SKIP LOCKED`" | #7 "Redis is the queue" is retired by #19 "Postgres is the queue; Redis removed in V1" |
| Storage | `memory_revisions` | `memories.superseded_by_id` |

Collapsing these into one mechanism (which your spec offered as an either/or) is the modelling mistake I most want to avoid. If #19 had to be "revision 2 of #7", we would have to lie about who authored it and when the *new* fact was first asserted, and we could not express one memory retiring three.

**Why `superseded_by_id` as a column rather than a `memory_links` table:** the shape is N→1 (many old, one winner). A nullable self-FK captures it exactly, costs one index, and lets the winner-lookup be a single join. A link table would be the right choice only if a memory could be superseded by *two* memories at once — which is a fork, and forks are exactly what we want to forbid. Other relation kinds (`REFINES`, `RELATES_TO`) are deferred until we have a concrete use; there is no `memory_links` table in V1.

### 4.3 Memory types: the behavioural test

A type earns its existence only if it changes at least one of: default TTL, ranking band, or context-budget quota.

| Type | Default TTL | Base importance | Recency half-life | Context quota |
|---|---|---|---|---|
| `CONSTRAINT` | none | 80 | effectively infinite | 25% |
| `DECISION` | none | 70 | 365 d | 40% |
| `FACT` | none | 50 | 180 d | 20% |
| `TASK` | **required, max 30 d** | 40 | 7 d | 15% |

**`TASK` is narrowly defined and stays that way.** It captures short-lived working state — "currently implementing worker heartbeat logic" — so a second client knows where you left off. That is one of the main reasons shared memory is worth having. It carries no assignee, no board, no sub-tasks, no estimate, no workflow state. Its distinct lifecycle is exactly the three columns above: a mandatory short TTL, a 7-day recency half-life that decays it out of rankings fast, and the smallest context quota. If it ever acquires a fourth behaviour, that is the project turning into an issue tracker and the type gets cut.

Cut and why: `OBSERVATION` → covered by `author_kind` (`agent` vs `human_confirmed`) plus importance. `BUG` and `SOLUTION` → a resolved bug is a `FACT` tagged `bug`; an open one is a `TASK`. `TEMPORARY_CONTEXT` → any type with an `expires_at`; making it a type would mean a memory changes type when its TTL is set, which is nonsense.

### 4.4 Schema (PostgreSQL 16+, pgvector)

#### projects

```sql
CREATE TABLE projects (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug         text NOT NULL,
  display_name text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  archived_at  timestamptz,
  CONSTRAINT projects_slug_format
    CHECK (slug ~ '^[a-z0-9][a-z0-9._-]{0,62}[a-z0-9]$')
);
CREATE UNIQUE INDEX projects_slug_key ON projects (slug);

CREATE TABLE project_aliases (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind       text NOT NULL CHECK (kind IN ('git_remote','workspace_path')),
  value_norm text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
-- Global, not per-project: an alias value can never map to two projects.
CREATE UNIQUE INDEX project_aliases_value_key ON project_aliases (kind, value_norm);
```

`value_norm` for `git_remote` normalises scheme, host case, `.git` suffix, and SSH-vs-HTTPS form, so `git@github.com:me/repo.git` and `https://github.com/me/repo` collapse to one value. That normaliser is pure, unit-tested, and versioned.

#### memories — logical identity and lifecycle

```sql
CREATE TABLE memories (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id          uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  type                text NOT NULL
    CHECK (type IN ('DECISION','CONSTRAINT','FACT','TASK')),
  status              text NOT NULL DEFAULT 'ACTIVE'
    CHECK (status IN ('ACTIVE','SUPERSEDED','DELETED')),
  current_revision_no integer NOT NULL DEFAULT 1 CHECK (current_revision_no >= 1),
  importance          smallint NOT NULL DEFAULT 50 CHECK (importance BETWEEN 0 AND 100),
  expires_at          timestamptz,
  superseded_by_id    uuid,
  superseded_at       timestamptz,
  deleted_at          timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),

  -- Needed so child tables can carry a composite FK that pins the project.
  CONSTRAINT memories_id_project_uq UNIQUE (id, project_id),

  -- A memory can only be superseded by a memory in the SAME project.
  -- Namespace isolation enforced by the schema, not by a WHERE clause someone might forget.
  CONSTRAINT memories_supersede_same_project
    FOREIGN KEY (superseded_by_id, project_id)
    REFERENCES memories (id, project_id),

  CONSTRAINT memories_no_self_supersede CHECK (superseded_by_id IS DISTINCT FROM id),
  CONSTRAINT memories_superseded_consistent
    CHECK ((status = 'SUPERSEDED') = (superseded_at IS NOT NULL)),
  CONSTRAINT memories_superseded_has_target
    CHECK (status <> 'SUPERSEDED' OR superseded_by_id IS NOT NULL),
  CONSTRAINT memories_deleted_consistent
    CHECK ((status = 'DELETED') = (deleted_at IS NOT NULL)),
  CONSTRAINT memories_task_needs_ttl
    CHECK (type <> 'TASK' OR expires_at IS NOT NULL)
);

CREATE INDEX memories_active_lookup
  ON memories (project_id, type, importance DESC)
  WHERE status = 'ACTIVE';

CREATE INDEX memories_expiry_sweep
  ON memories (expires_at)
  WHERE expires_at IS NOT NULL AND status = 'ACTIVE';

CREATE INDEX memories_superseded_by ON memories (superseded_by_id)
  WHERE superseded_by_id IS NOT NULL;
```

Note the absence of `EXPIRED`. Expiry is `expires_at <= now()`, evaluated at read time, so it is never stale. The sweep index exists for a *retention* job (archival), not for correctness.

The composite FK `(superseded_by_id, project_id) -> (id, project_id)` is the piece I would point at in an interview: **cross-project contamination through supersession is not prevented by application code, it is unrepresentable.**

#### memory_revisions — the append-only content log

```sql
CREATE TABLE memory_revisions (
  memory_id     uuid    NOT NULL,
  project_id    uuid    NOT NULL,
  revision_no   integer NOT NULL CHECK (revision_no >= 1),
  content       text    NOT NULL CHECK (length(content) BETWEEN 1 AND 8192),
  content_hash  bytea   NOT NULL,          -- sha256 of normalised content
  hash_version  smallint NOT NULL DEFAULT 1,
  tags          text[]  NOT NULL DEFAULT '{}',
  is_current    boolean NOT NULL,
  change_reason text,
  author_client text    NOT NULL,          -- 'claude-desktop' | 'cursor' | ...
  author_kind   text    NOT NULL
    CHECK (author_kind IN ('agent','human_confirmed','import')),
  created_at    timestamptz NOT NULL DEFAULT now(),

  content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,

  PRIMARY KEY (memory_id, revision_no),
  FOREIGN KEY (memory_id, project_id)
    REFERENCES memories (id, project_id) ON DELETE CASCADE,
  CONSTRAINT memory_revisions_tags_bounded CHECK (cardinality(tags) <= 16)
);

-- INVARIANT: exactly one current revision per logical memory.
CREATE UNIQUE INDEX memory_revisions_one_current
  ON memory_revisions (memory_id) WHERE is_current;

CREATE INDEX memory_revisions_fts
  ON memory_revisions USING gin (content_tsv) WHERE is_current;

CREATE INDEX memory_revisions_tags
  ON memory_revisions USING gin (tags) WHERE is_current;
```

Two invariants ride on indexes rather than on code:

- `PRIMARY KEY (memory_id, revision_no)` → revision numbers are unique per memory. Two concurrent writers cannot both create revision 5.
- `UNIQUE (memory_id) WHERE is_current` → at most one current revision, ever, even if the service layer has a bug.

`project_id` is denormalised into revisions for two reasons: it makes the composite FK possible (which is what pins isolation), and it lets project-scoped scans avoid a join.

**There is no generic `attrs jsonb` column, deliberately.** An open extensibility bag is where a structured domain model goes to die: fields that should have been columns with constraints accumulate inside it, unvalidated and unindexable, and the schema stops describing the data. If provenance later needs provider-specific metadata, we add a narrowly scoped, purpose-named column — `source_metadata`, with a stated size limit and a documented set of producers — for that concrete case. Never generic extensibility ahead of a concrete need.

#### memory_dedup_keys — deduplication as a real constraint

```sql
CREATE TABLE memory_dedup_keys (
  project_id   uuid     NOT NULL,
  hash_version smallint NOT NULL,
  content_hash bytea    NOT NULL,
  memory_id    uuid     NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, hash_version, content_hash),
  FOREIGN KEY (memory_id, project_id)
    REFERENCES memories (id, project_id) ON DELETE CASCADE
);
```

**Why a separate table rather than a partial unique index on revisions:** the rule we want is "no two *active* memories in a project have identical normalised content". `is_current` lives on the revision but `status` lives on the memory, and a unique index cannot span two tables. A dedicated table whose rows we insert on create and delete on forget/supersede gives us a genuine unique constraint over exactly the right set, and it makes dedup a single race-free statement:

```sql
INSERT INTO memory_dedup_keys (project_id, hash_version, content_hash, memory_id)
VALUES (:pid, :hv, :hash, :new_id)
ON CONFLICT (project_id, hash_version, content_hash) DO NOTHING
RETURNING memory_id;
```

Zero rows returned means someone already asserted this fact — we then read the existing `memory_id` and record an attestation instead of creating a duplicate. No "check then insert", no race.

`hash_version` is in the primary key so a future change to the normaliser can be rolled out alongside the old one rather than as a big-bang migration.

**Normalisation for the hash** (v1, pure and unit-tested): NFKC Unicode normalisation, lowercase, collapse internal whitespace, strip leading/trailing whitespace and trailing sentence punctuation. Deliberately conservative — "Postgres is the queue" and "postgres is the queue." collapse; "Postgres is the queue" and "We use Postgres for queueing" do not. Semantic dedup is a later, separate, non-blocking mechanism (§7.5).

#### memory_attestations — corroboration as a signal

```sql
CREATE TABLE memory_attestations (
  memory_id     uuid NOT NULL,
  project_id    uuid NOT NULL,
  client_name   text NOT NULL,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz NOT NULL DEFAULT now(),
  times_seen    integer NOT NULL DEFAULT 1 CHECK (times_seen >= 1),
  PRIMARY KEY (memory_id, client_name),
  FOREIGN KEY (memory_id, project_id)
    REFERENCES memories (id, project_id) ON DELETE CASCADE
);
```

When a dedup hit occurs we do not throw the event away — we record that a *second independent client* asserted the same fact. `COUNT(DISTINCT client_name)` becomes a small ranking prior. This turns duplicate writes from a nuisance into evidence, and it is a nice thing to be able to show: "Claude Desktop and Cursor independently asserted this; it is probably load-bearing."

#### idempotency_keys

```sql
CREATE TABLE idempotency_keys (
  project_id          uuid NOT NULL,
  client_request_id   text NOT NULL
    CHECK (length(client_request_id) BETWEEN 8 AND 128),
  operation           text NOT NULL,
  request_fingerprint bytea NOT NULL,
  state               text NOT NULL CHECK (state IN ('IN_PROGRESS','COMPLETED')),
  response            jsonb,
  created_at          timestamptz NOT NULL DEFAULT now(),
  completed_at        timestamptz,
  expires_at          timestamptz NOT NULL DEFAULT now() + interval '7 days',
  PRIMARY KEY (project_id, client_request_id),
  CONSTRAINT idem_completed_has_response
    CHECK (state <> 'COMPLETED' OR response IS NOT NULL)
);
CREATE INDEX idempotency_keys_gc ON idempotency_keys (expires_at);
```

#### memory_embeddings and the outbox

```sql
CREATE TABLE memory_embeddings (
  memory_id   uuid     NOT NULL,
  revision_no integer  NOT NULL,
  project_id  uuid     NOT NULL,
  model       text     NOT NULL,
  dim         smallint NOT NULL,
  embedding   vector(768) NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (memory_id, revision_no, model),
  FOREIGN KEY (memory_id, revision_no)
    REFERENCES memory_revisions (memory_id, revision_no) ON DELETE CASCADE
);

CREATE INDEX memory_embeddings_hnsw
  ON memory_embeddings USING hnsw (embedding vector_cosine_ops);

CREATE TABLE embedding_jobs (
  id              bigserial PRIMARY KEY,
  memory_id       uuid NOT NULL,
  revision_no     integer NOT NULL,
  project_id      uuid NOT NULL,
  model           text NOT NULL,
  state           text NOT NULL DEFAULT 'PENDING'
    CHECK (state IN ('PENDING','DONE','DEAD')),
  attempts        smallint NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (memory_id, revision_no, model),
  FOREIGN KEY (memory_id, revision_no)
    REFERENCES memory_revisions (memory_id, revision_no) ON DELETE CASCADE
);
CREATE INDEX embedding_jobs_claim
  ON embedding_jobs (next_attempt_at) WHERE state = 'PENDING';
```

Embeddings live in their own table, not as a column on `memory_revisions`, for three reasons: the hot revision row stays narrow (a 768-dim vector is ~3 KB and would wreck sequential-scan and index-only-scan behaviour on the metadata path); re-embedding under a new model does not rewrite the content log; and the model name is part of the key, so vectors from different models can coexist without ever being compared to each other.

`vector(768)` pins the dimension per deployment — pgvector requires a fixed dimension per column. The `dim` column is an assertion, not flexibility. Multi-dimension support, if ever needed, means a second table, and that is a conscious deferral.

#### audit_events

```sql
CREATE TABLE audit_events (
  id           bigserial PRIMARY KEY,
  at           timestamptz NOT NULL DEFAULT now(),
  project_id   uuid,
  memory_id    uuid,
  revision_no  integer,
  action       text NOT NULL,     -- remember | revise | forget | supersede | purge
  outcome      text NOT NULL,     -- ok | conflict | dedup | rejected
  actor_client text NOT NULL,
  request_id   text,
  detail       jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_events_memory ON audit_events (memory_id, at DESC);
```

Never contains memory content — only hashes, lengths, and identifiers. See §10.

### 4.5 Index rationale

| Index | Serves | Note |
|---|---|---|
| `memories (project_id, type, importance DESC) WHERE ACTIVE` | context builder's per-type candidate pull | partial index keeps it small; project first because it is in every predicate |
| `memory_revisions USING gin (content_tsv) WHERE is_current` | full-text | partial: retired revisions are ~90% of rows at steady state and are never text-searched |
| `memory_embeddings USING hnsw` | ANN | see §7.3 for the filtered-ANN problem |
| `memory_revisions (memory_id) WHERE is_current` (unique) | invariant + current-revision lookup | does double duty |
| `embedding_jobs (next_attempt_at) WHERE PENDING` | outbox claim | partial index shrinks to near-zero at steady state |

Every one of these will be justified with `EXPLAIN ANALYZE` output committed to `docs/perf/` before it is called done. Any index that does not show up in a plan gets deleted.

---

## 5. Concurrency model

### 5.1 The scenario

Claude Desktop's server process reads memory `M` at revision 4. Cursor's server process reads the same. Claude revises it to 5 and commits. Cursor now submits a revision based on 4.

Cursor's write **must not** land. Not "should usually not". Must not.

### 5.2 The mechanism: single-statement compare-and-set

Inside one transaction at `READ COMMITTED`:

```sql
-- Statement 1: the serialisation point AND the predicate check, atomically.
UPDATE memories
   SET current_revision_no = current_revision_no + 1,
       updated_at          = now()
 WHERE id                  = :memory_id
   AND project_id          = :project_id
   AND current_revision_no = :expected_revision
   AND status              = 'ACTIVE'
RETURNING current_revision_no AS new_revision;
```

**Zero rows returned → `REVISION_CONFLICT`.** Roll back, return the structured conflict result from §3.3. One row returned → we hold the row lock on `M` and we are the unique winner:

```sql
-- Statement 2: demote the old current revision.
UPDATE memory_revisions
   SET is_current = false
 WHERE memory_id = :memory_id AND revision_no = :expected_revision;

-- Statement 3: append the new immutable revision.
INSERT INTO memory_revisions
  (memory_id, project_id, revision_no, content, content_hash, hash_version,
   tags, is_current, change_reason, author_client, author_kind)
VALUES (:memory_id, :project_id, :new_revision, ...,  true, ...);

COMMIT;
```

### 5.3 The SQL-level correctness argument

This is the part that must be airtight, because "what guarantees does the database give you" is the question.

**Step 1 — the UPDATE takes a row-level exclusive lock.** Two concurrent transactions both matching row `M` cannot both proceed. The second one blocks on the first transaction's lock, held until commit or rollback.

**Step 2 — `EvalPlanQual`: the loser re-checks its predicate against the *new* row version.** This is the crux, and it is a `READ COMMITTED` behaviour specifically. When T1 commits, T2 wakes up. Under `READ COMMITTED`, PostgreSQL does **not** simply proceed with the row version T2 originally found; it walks to the newest committed version of that row and **re-evaluates the `WHERE` clause against it**. T2's predicate says `current_revision_no = 4`; the row now says `5`. The predicate fails, the row is skipped, and the `UPDATE` reports **0 rows affected**.

So the conflict is detected by the storage engine's own visibility machinery. There is no read-then-write window for us to lose in, because the read and the write are the same statement.

**Step 3 — the isolation level is a deliberate choice.** Under `REPEATABLE READ` or `SERIALIZABLE`, the same collision raises `40001 could not serialize access due to concurrent update` — an exception the application must catch and retry. That is *also* correct, but it converts a clean deterministic domain outcome into a retry loop, and under 50-way contention it produces 49 retry storms. `READ COMMITTED` + explicit CAS gives us **deterministic, allocation-free conflict detection with no retries**, and it makes the test assertion exact: 1 success, 49 conflicts, always. We choose the isolation level *because of* the conflict semantics we want, and that is worth saying out loud.

**Step 4 — defence in depth.** Suppose the service layer had a bug and skipped statement 1:

- `PRIMARY KEY (memory_id, revision_no)` rejects a second revision 5 with `23505`.
- `UNIQUE (memory_id) WHERE is_current` rejects a second current revision with `23505`.

The invariant survives a wrong code path. This is what "enforce invariants with constraints, not documentation" means concretely.

**Step 5 — deadlock freedom.** Every writer takes the `memories` row lock **first**, before touching `memory_revisions`. A single, consistent lock order across all write paths means no cycle can form. `memory_remember` with `supersedes=[...]` locks its target memories in **ascending UUID order** for the same reason.

**Step 6 — no lost update via a third path.** `memory_forget` and the supersede path also go through a CAS `UPDATE ... WHERE status = 'ACTIVE'` on the same row, so they participate in the same serialisation point rather than racing around it.

### 5.4 Alternatives considered and rejected

| Approach | Why rejected |
|---|---|
| `SELECT ... FOR UPDATE` then check in Python | Correct, but two statements where one suffices, and it expresses the intent worse. The CAS `UPDATE` acquires the lock and evaluates the predicate in one atomic step. |
| `SERIALIZABLE` isolation | Correct, but turns a domain outcome into a retryable exception; 49 concurrent retries where we want 49 clean conflicts. |
| Postgres advisory locks | Not tied to row lifetime, invisible to any code path that forgets to take them, and they protect nothing after commit. |
| Python `asyncio.Lock` | Useless: the two writers are **different OS processes**. This is the trap the architecture makes obvious. |
| `xmin` / system-column optimistic locking | Works, but exposes an opaque internal value to clients. `revision_no` is meaningful to a model and to a human reading history. |

### 5.5 Where concurrency is *not* needed

Reads take no locks. The context builder runs entirely on a single snapshot — a stale-but-consistent read is fine and preferable to blocking a write. Attestation upserts use `ON CONFLICT DO UPDATE` and are commutative. The embedding worker uses `SKIP LOCKED` so workers never contend.

---

## 6. Idempotency model

### 6.1 Idempotency is not deduplication

They are constantly conflated; keeping them apart is a design point.

| | Idempotency | Deduplication |
|---|---|---|
| Trigger | *One* client retries *the same request* | *Two* clients independently assert *the same fact* |
| Key | `client_request_id` (opaque, client-generated) | normalised content hash |
| Correct response | byte-identical replay of the original response | pointer to the existing memory + a new attestation |
| Failure if absent | duplicate memory from a network retry | corpus fills with near-identical rows |

### 6.2 The protocol

`check-then-insert` races, so we do not do it. Inside the write transaction:

```sql
-- 1. Claim the key.
INSERT INTO idempotency_keys
  (project_id, client_request_id, operation, request_fingerprint, state)
VALUES (:pid, :rid, :op, :fp, 'IN_PROGRESS')
ON CONFLICT (project_id, client_request_id) DO NOTHING
RETURNING 1;
```

**Case A — one row returned: we own the key.** Perform the write, then in the same transaction:

```sql
UPDATE idempotency_keys
   SET state='COMPLETED', response=:json, completed_at=now()
 WHERE project_id=:pid AND client_request_id=:rid;
COMMIT;
```

Because the claim, the write, and the completion all commit atomically, there is no state in which the key says `COMPLETED` but the memory does not exist.

**Case B — zero rows returned: someone else owns it.** The owner may still be *in flight and uncommitted*. So:

```sql
SELECT state, response, request_fingerprint
  FROM idempotency_keys
 WHERE project_id=:pid AND client_request_id=:rid
   FOR SHARE;
```

`FOR SHARE` **blocks** until the owning transaction commits or aborts. That blocking is the point — it is how we wait for a concurrent duplicate without polling.

- Owner committed → the row reads `COMPLETED`; return its stored `response`. Identical result, no second write.
- Owner rolled back → the row is **gone** (the insert was rolled back too); `SELECT` returns nothing; loop back to step 1 and retry the claim. Bounded to a small number of attempts.

**Fingerprint mismatch.** If the key exists but `request_fingerprint` differs from this request's, we return an error (`IDEMPOTENCY_KEY_REUSED`) rather than the old response. Silently returning a result for a *different* request is worse than failing.

**Fingerprint definition:** SHA-256 over a canonical JSON encoding (sorted keys, normalised whitespace) of the semantically meaningful arguments — deliberately excluding `source` and free-text metadata, so a retry that differs only in an incidental field still replays.

**GC:** rows expire after 7 days; a bounded `DELETE ... WHERE expires_at < now()` runs at startup and hourly. Bounded because an unbounded delete on a busy table is its own outage.

### 6.3 Which operations get idempotency

| Operation | Idempotency key | Rationale |
|---|---|---|
| `memory_remember` | optional but strongly recommended | naturally non-idempotent; the retry hazard is real |
| `memory_revise` | optional | already protected by CAS — a retry of a *committed* revise fails with `REVISION_CONFLICT`, which is safe but confusing. The key converts it into a clean replay. |
| `memory_forget` | not needed | naturally idempotent: tombstoning a tombstoned memory is a no-op returning the same state |
| `project_use` | not needed | naturally idempotent via the unique slug |
| reads | n/a | no side effects |

This is worth noting: **`memory_revise` is already safe without a key** — the CAS makes a duplicate write impossible. The key exists purely to give the caller a *good* answer instead of a confusing one. Understanding when idempotency is required for correctness versus for ergonomics is the actual insight.

### 6.4 What idempotency buys the failure model

The reason we can safely retry a write after a `08006 connection failure` — where the client genuinely does not know whether the transaction committed — is that the retry carries the same key. Without it, "unknown outcome" is unrecoverable and the only safe action is to give up. §9 depends on this section.
---

## 7. Retrieval architecture

### 7.1 The stage-0 filter, which is the actual product

Before any ranking, every retrieval path applies the same non-negotiable predicate:

```sql
FROM memory_revisions r
JOIN memories m
  ON m.id = r.memory_id AND m.project_id = r.project_id
WHERE m.project_id = :project_id            -- namespace isolation
  AND m.status     = 'ACTIVE'               -- excludes SUPERSEDED and DELETED
  AND (m.expires_at IS NULL OR m.expires_at > now())   -- expiry, derived
  AND r.is_current                          -- current revision only
```

This is written **once**, in one function, and every ranking strategy composes on top of it. There is no code path that can rank a superseded memory, because there is no code path that can *see* one without explicitly passing `include_superseded=true` (which only `memory_history` and debug queries do).

That five-line predicate is the answer to the Redis-vs-Postgres demo. Everything after it is relevance tuning.

**Consciously chosen inefficiency:** filtering `m.status` requires a join to `memories`, since the FTS index lives on `memory_revisions`. Denormalising a `retrievable` boolean into revisions would remove the join — and would introduce a value that can go stale. I am starting with the join, and will only denormalise if `EXPLAIN ANALYZE` at 100k memories shows it matters. Committing the before/after plans is a better story than either choice alone.

### 7.2 Stage 1 — lexical

```sql
ts_rank_cd(r.content_tsv, websearch_to_tsquery('english', :q)) AS lex_score
```

`websearch_to_tsquery` because it tolerates the phrasing a model actually produces (quoted phrases, `or`, `-term`) instead of raising a syntax error on a stray operator, which `to_tsquery` does. `ts_rank_cd` over `ts_rank` because cover density rewards proximity, which matters for short memory texts.

`ts_rank_cd` is unbounded and corpus-dependent, so its absolute value is meaningless across queries. We only ever use its **rank order**. See §7.4.

### 7.3 Stage 2 — semantic

Cosine distance over pgvector HNSW, restricted to the current embedding model.

The real engineering problem here is **filtered ANN**. An HNSW index scan returns `ef_search` candidates by vector distance and *then* the planner applies our `WHERE` clause. With a restrictive filter (one project out of many, active only), the ANN scan can return 40 neighbours of which 3 survive filtering, and we silently under-return. Mitigations, in order of preference:

1. `SET LOCAL hnsw.iterative_scan = relaxed_order` (pgvector 0.8+) so the scan continues until enough rows survive the filter.
2. Raise `hnsw.ef_search` for this query only, via `SET LOCAL`.
3. If a single project ever dominates, a partial HNSW index per project — rejected for now as unbounded index proliferation.

We will **measure** post-filter recall against exact search on the eval corpus and record it. "I discovered my ANN recall was 0.7 after filtering and fixed it with iterative scans, here is the before/after" is a far better answer than "I added pgvector".

### 7.4 Stage 3 — fusion via Reciprocal Rank Fusion

Do not sum `ts_rank_cd` and cosine similarity. They have different units, different distributions, and different query-dependence. Min-max normalising them per query is also wrong: it makes a query with only weak matches look identical to one with a perfect match.

**RRF** operates purely in rank space:

```
rrf(d) = sum over each retriever i of  1 / (k + rank_i(d)),   k = 60
```

Properties that make it the right tool: scale-free, no per-query normalisation, robust to one retriever returning garbage, and it degrades gracefully when a document is missing from one list (it simply contributes nothing — which is exactly what should happen to a memory whose embedding has not been computed yet).

Then, and only then, a small number of **interpretable multiplicative priors**:

```
final(d) = rrf(d)
         * (1 + w_imp  * importance(d)/100)
         * (1 + w_rec  * recency(d))
         * (1 + w_att  * min(attestations(d) - 1, 2)/2)
         * type_weight(d)

recency(d) = 0.5 ^ (age_days / half_life(type))
```

Four weights, each with a one-line justification, all tuned against the eval set in §12 — not by taste. The response includes a `why` block per result showing the component ranks and the applied priors, which makes ranking debuggable instead of mystical.

### 7.5 Semantic near-duplicate handling

Distinct from exact dedup (§4.4). Once embeddings exist, a write whose cosine similarity to an existing active memory exceeds a threshold does **not** get merged automatically — merging on a similarity score is how you silently destroy a real distinction. Instead:

- The `remember` response includes `similar: [{memory_id, similarity, content}]`.
- The tool description instructs the model: if one of these is the same fact, call `memory_revise` or pass `supersedes`.

**The model decides; the server surfaces evidence.** Automatic semantic merging is a Phase-N+1 idea at best, and possibly never.

### 7.6 Staging summary

| Phase | Mechanism | Depends on |
|---|---|---|
| 1 | stage-0 filter + exact/tag/type lookup | schema only |
| 2 | + FTS (`tsvector`, GIN, `ts_rank_cd`) | nothing external |
| 3 | + eval harness with FTS baseline recorded | Phase 2 |
| 4 | + pgvector, deterministic fake embedder | Phase 3 (measure first) |
| 5 | + RRF fusion, prior weights tuned on the eval set | Phase 4 |

Embeddings are never on the critical path for a write to succeed (§9.3).

---

## 8. Context-builder design

`memory_context(project_id, query?, token_budget, focus?)` is the feature that distinguishes this from a search box. It is a **constrained selection problem**, and it should be presented as one.

### 8.1 Pipeline

```
1. CANDIDATES     stage-0 filter + retrieval (§7). Over-fetch ~5x the budget's worth.
2. HARD FILTERS   drop anything over-budget by itself; drop explicit exclusions.
3. SCORE          §7.4 final score.
4. QUOTAS         per-type budget shares (§4.3). Unused share is redistributed,
                  highest-scoring bucket first, so a project with no open TASKs
                  spends that 15% on DECISIONs rather than wasting it.
5. DIVERSIFY      MMR: score' = L*score - (1-L)*max_sim(d, already_selected), L=0.7.
                  Prevents three restatements of the same decision eating the budget.
6. FILL           greedy by score-per-token, subject to the remaining budget.
                  (Knapsack; greedy ratio-fill is within a known bound and is O(n log n).
                  Exact DP is not worth it at n<500 candidates and would be non-obvious
                  to justify.)
7. ORDER          stable sort: CONSTRAINT, DECISION, FACT, TASK; then score desc; then
                  memory_id asc. Identical inputs produce byte-identical output.
8. RENDER         grouped markdown brief + machine-readable items + budget report.
```

Step 7 matters more than it looks: **deterministic output makes the whole thing testable and cacheable.** A context builder whose output shuffles between identical calls cannot be snapshot-tested and cannot be safely cached.

### 8.2 Token estimation, honestly

The server cannot tokenise for the client's model, because it does not know the model, and tokenisers differ. Options and the choice:

- `tiktoken` — wrong tokeniser for Claude; precise-looking and wrong.
- A hosted token-counting API — a network dependency in the read path. No.
- **A calibrated heuristic** — `ceil(len(chars)/3.6) + per_item_overhead`, with the constant fitted against real tokenisations of the eval corpus, plus a **10% safety margin** so we bias toward under-filling.

This is exposed as an injectable `TokenEstimator` protocol, and the response reports which estimator produced the number. The documented contract is:

> **We never exceed the stated budget. We may under-fill it by up to ~10%.**

Being explicit that this is an estimate with a measured error bound and a deliberate conservative bias is a much stronger position than pretending to exactness. The measured p95 estimation error goes in `docs/perf/`.

### 8.3 Measurable objectives

| Metric | Target | Why |
|---|---|---|
| Budget adherence | 100% (hard) | exceeding the budget is a correctness bug, not a quality issue |
| Budget utilisation | 85–95% | under-filling wastes the agent's context |
| Stale inclusion rate | **0%** | a superseded memory in a brief is a total failure |
| Coverage@budget | maximise on eval set | fraction of the query's known-relevant memories included |
| p95 latency | < 150 ms at 10k memories | it runs at session start; it cannot feel slow |

---

## 9. Failure model

| Failure | Detection | Behaviour | Invariant preserved |
|---|---|---|---|
| Postgres unavailable at startup | connect timeout | server starts, advertises tools, every call returns a structured `BACKEND_UNAVAILABLE` error with a retry hint | none violated; nothing was written |
| Postgres drops mid-transaction | `08006` / `57P01` | transaction is aborted by the server; classify as *retryable*; retry **only** with an idempotency key (§6.4), else surface `UNKNOWN_OUTCOME` and tell the caller to re-read | no partial write: single transaction |
| Connection pool exhausted | pool acquire timeout | fail fast (~2 s) with `BACKEND_BUSY`; never queue unboundedly | — |
| Two clients revise the same memory | CAS returns 0 rows | one wins, the other gets `REVISION_CONFLICT` with the current revision + content | exactly one revision N+1 |
| Duplicate write from a retry | idempotency key conflict | replay the stored response | no duplicate memory |
| Two clients assert the same fact | dedup-key conflict | return the existing memory, record an attestation | no duplicate memory |
| Embedding provider down | exception in worker | **write already committed.** Job retried with exponential backoff + jitter; after N attempts → `DEAD` + metric + log | metadata and vector never disagree: absence is an explicit state, not a lie |
| Embedding partially written | — | cannot happen: the vector insert and the job completion are one transaction | — |
| Server killed mid-request | — | transaction rolls back; process holds no authoritative state; restart is a non-event | — |
| Client disconnects mid-call | stdio EOF | the transaction either committed or did not; the client re-attaches and either replays its idempotency key or re-reads | — |
| Malformed tool input | Pydantic validation | protocol-level error naming the offending fields; nothing touched | — |
| Oversized content | `CHECK` + pre-validation | rejected at 8 KB with the actual size in the message | — |
| Search exceeds deadline | `statement_timeout` + per-request deadline | `memory_search` errors; `memory_context` **degrades to FTS-only** and sets `degraded: "fts_only"` in the response | budget never exceeded |
| Some memories lack embeddings | `semantic_coverage < 1.0` | results still returned via FTS; coverage reported in the response | search is never silently partial |
| Clock skew between processes | — | **all timestamps come from the database** (`now()`), never from an application clock | ordering is total and consistent |
| Schema drift / app older than DB | Alembic head check at startup | server **refuses to start** and prints the required migration | no writes against an unexpected schema |
| Retention job deletes live data | — | retention only *archives*; the only destructive path is operator-invoked `purge`, which is audited and confirmed | history is never lost accidentally |

### 9.1 The consistency decision: where does embedding generation belong?

Three options, and the reasoning:

**(a) Inside the write transaction.** Simple to state, unacceptable in practice: it holds a Postgres transaction — and the `memories` row lock from §5.2 — open across a slow, network-fallible call. Under contention, one slow embedding call blocks every other writer on that memory. It also makes a provider outage into a *write* outage: you can no longer record a decision because a model is down. That is an absurd coupling.

**(b) After the transaction, fire-and-forget.** The classic dual-write bug. Crash between commit and enqueue, and the memory exists forever with no vector and nothing knows to fix it.

**(c) Transactional outbox — chosen.** The `embedding_jobs` row is inserted **in the same transaction as the revision**. Either both exist or neither does. A worker then claims jobs with:

```sql
SELECT * FROM embedding_jobs
 WHERE state = 'PENDING' AND next_attempt_at <= now()
 ORDER BY next_attempt_at
 FOR UPDATE SKIP LOCKED
 LIMIT 16;
```

`SKIP LOCKED` lets workers in both server processes drain the queue without contending or double-processing. The vector insert and the `state='DONE'` update are one transaction, so there is no window where a vector exists but the job looks pending, or vice versa.

**Consistency achieved: eventual, with an explicit, observable, queryable pending state.** The system is never *silently* inconsistent — `embedding_status` is a fact you can query, `semantic_coverage` is reported in every search response, and a metric tracks the backlog. The tradeoff is stated plainly: a memory written 200 ms ago is findable by full-text search but may not yet be findable by semantic search. For this product that window is irrelevant, and we say so rather than pretending it does not exist.

There is a pleasing self-reference worth putting in the README: **this project uses PostgreSQL as its durable job queue** (`FOR UPDATE SKIP LOCKED`), which is the exact architectural decision used as the running example throughout the spec, and the exact reason we did not add Redis.

---

## 10. Security and privacy model

Proportionate to a local, single-user service — but designed, not skipped.

### 10.1 Why explicit shared memory rather than automatic chat collection

Three independent reasons, in increasing order of importance:

1. **Protocol reality.** An MCP server receives tool-call arguments. It does not receive the transcript. Automatic collection is not something we are declining to build — it is not available to build. The README says this in plain language, and no marketing copy will ever imply otherwise.
2. **Retrieval quality.** Even with a transcript, ingesting everything destroys the corpus. Precision collapses under noise, and every retrieval competes against thousands of irrelevant fragments. A curated 500-memory corpus outperforms a 500,000-message one, and this is measurable on our eval set.
3. **Consent and provenance.** A memory only means something because someone *decided* it was worth keeping. That decision is what makes supersession meaningful — you can retire an asserted fact, but you cannot retire an overheard one. Explicit writes give us `author_client`, `author_kind`, and a defensible answer to "why does your service have this sentence in it".

### 10.2 Secret hygiene

The system must be actively hostile to becoming a secret store:

- **Write-time screening**: pattern rules (AWS keys, GitHub PATs, private-key headers, `Bearer` tokens, JWT shape, common `KEY=value` env lines) plus a Shannon-entropy heuristic on long unbroken tokens.
- **Default action: reject**, with a message naming the rule that fired. Not "store and warn" — a warning in a transcript is not a control.
- Override requires an explicit `acknowledge_sensitive: true`, which is recorded in the audit log. There is always a real case (a memory *about* a credential's location) and refusing to model it just pushes people to obfuscate.
- **Structural rejection** of content that looks like a dumped file (`.env` contents, `-----BEGIN`, `id_rsa`) regardless of entropy.
- **Purge path**: an operator CLI command that hard-deletes every revision, embedding, dedup key, and attestation for a memory, replaces its `audit_events.detail` with a redaction marker, and writes a purge record. Soft delete is insufficient for a leaked secret; this is the only place the system destroys history, and it is deliberately not reachable from MCP.

### 10.3 Isolation and input validation

- Every repository method takes a `ProjectScope`; there is no method that can read a memory without one. Enforced by types, not discipline.
- Cross-project supersession is unrepresentable (composite FK, §4.4).
- Resource URIs are strictly parsed and re-checked against the memory's project.
- Hard limits: content 8 KB, tags 16, `client_request_id` 128 chars, search `limit` 100, `token_budget` 32k. All as `CHECK` constraints *and* Pydantic validators — the constraint is the guarantee, the validator is the good error message.
- **Logs never contain memory content.** They contain `content_hash`, `length`, `memory_id`, `type`. A redaction filter on the log formatter enforces this, and a test asserts that a known secret string never appears in captured log output.

### 10.4 Deferred to the remote phase

Authentication, per-caller authorisation, rate limiting, and multi-tenant row-level security. Attempting them in a local stdio V1 would be ceremony. Naming them as a distinct phase with a sketch of the approach is better engineering than a half-built IAM.

---

## 11. Observability

### 11.1 The transport problem, stated first

The server is a **short-lived subprocess**. Prometheus pull-scraping cannot find it, and a metrics endpoint on a random port is a poor fit. So: **OTLP push** to a local collector, with the collector exposing a Prometheus endpoint. In HTTP mode later, direct scraping becomes available. Recognising that pull-based metrics do not fit ephemeral processes is itself the interesting observation.

### 11.2 Metrics

All labels are **bounded cardinality**. `memory_id`, `project_id`, and content never appear as labels — they belong in traces and logs.

```
memhub_tool_calls_total{tool, outcome}                      counter
memhub_tool_latency_seconds{tool}                           histogram
memhub_writes_total{type, outcome}                          counter    # created|dedup|idempotent_replay
memhub_revisions_total{outcome}                             counter    # ok|conflict
memhub_conflicts_total{}                                    counter
memhub_supersessions_total{}                                counter
memhub_search_latency_seconds{strategy}                     histogram  # fts|vector|hybrid
memhub_search_results{strategy}                             histogram
memhub_semantic_coverage_ratio                              gauge
memhub_context_tokens_returned                              histogram
memhub_context_items_selected{type}                         histogram
memhub_context_budget_utilisation                           histogram
memhub_context_items_dropped_total{reason}                  counter    # budget|diversity|quota
memhub_embedding_queue_depth                                gauge
memhub_embedding_failures_total{reason}                     counter
memhub_secret_rejections_total{rule}                        counter
memhub_db_pool_in_use                                       gauge
```

`client_name` is a label only where it matters and only from an allow-list (`claude-desktop`, `cursor`, `other`) — an unbounded client string would be a cardinality bomb.

### 11.3 Traces

One root span per MCP tool call (`mcp.tool.<name>`), carrying `project_id`, `memory_id`, `client_name`, `request_id` as **span attributes** (high cardinality is fine here — that is the point of traces). Child spans: `db.transaction`, `db.cas_update`, `retrieval.fts`, `retrieval.vector`, `retrieval.fuse`, `context.select`, `embed.generate`. The conflict path is explicitly traced with the expected and actual revision, so a lost-update investigation is one trace away.

`request_id` is derived from the JSON-RPC request id and propagated into logs, traces, and `audit_events.request_id` — one identifier ties all three together.

### 11.4 Logs

Structured JSON, **to stderr** (§3.6), one line per event, with `request_id`, `tool`, `project_id`, `memory_id`, `outcome`, `duration_ms`. Never content. `INFO` for tool calls and lifecycle, `WARNING` for conflicts and rejections, `ERROR` for unexpected failures only — a version conflict is expected behaviour and must not page anyone.

### 11.5 The audit log is not the application log

`audit_events` is durable, queryable, and part of the product — it answers "who created this memory and what happened to it" inside `memory_history`. Application logs are operational and ephemeral. Conflating them is a common mistake; keeping them separate means the audit trail survives log rotation.

---

## 12. Test strategy

### 12.1 Layers

| Layer | Scope | Database |
|---|---|---|
| Unit | normalisers, hashing, token estimator, RRF, MMR, quota allocation, ranking priors | none |
| Integration | services against real Postgres: dedup, supersession, expiry, history | real |
| **Concurrency** | CAS, idempotency, outbox claims | real, dedicated |
| Protocol | MCP tool/resource contract via an in-memory client/server pair | real |
| Retrieval eval | Recall / nDCG / stale-inclusion | real + fake embedder |
| Migration | up/down/up; ORM-vs-schema drift | real |
| Failure | injected DB and provider failures | real + fault injection |
| Performance | 1k / 10k / 100k corpora | real |

**Real PostgreSQL for anything that touches SQL.** SQLite-in-tests would invalidate every guarantee in §5. Fast isolation via `CREATE DATABASE ... TEMPLATE` per test module rather than a full migration run per test.

### 12.2 The concurrency test — and a trap

```
1. Create memory M at revision 1.
2. Launch 50 coroutines, each with its OWN connection, gated on an asyncio.Barrier
   so they hit the CAS within microseconds of each other.
3. Each calls revise(M, expected_revision=1, content=f"attempt {i}").
4. Assert:
     - exactly 1 result is ok
     - exactly 49 results are REVISION_CONFLICT
     - memories.current_revision_no == 2
     - count(memory_revisions where memory_id=M) == 2
     - exactly one revision has is_current
     - the surviving content matches the winner's payload
     - audit_events has 1 ok + 49 conflict rows
5. Run the invariant suite (§12.3).
6. Repeat 20 times to shake out ordering luck.
```

**The trap, and it is a good interview detail:** if the connection pool holds 10 connections, you do not get 50-way concurrency — you get five waves of ten, and the test passes for the wrong reason. The fixture must assert `pool_size >= 51` (or hand each task its own engine). Getting this wrong produces a test that is green and meaningless.

### 12.3 The invariant suite

A set of SQL assertions run after every concurrency and failure test — the machine-checkable version of §13:

```sql
-- more than one current revision
SELECT memory_id FROM memory_revisions WHERE is_current
GROUP BY memory_id HAVING count(*) > 1;

-- pointer disagrees with the current revision
SELECT m.id FROM memories m JOIN memory_revisions r
  ON r.memory_id = m.id AND r.is_current
 WHERE r.revision_no <> m.current_revision_no;

-- gaps in the revision sequence
SELECT memory_id FROM memory_revisions
GROUP BY memory_id HAVING max(revision_no) <> count(*);

-- superseded without a target, or targeting another project
SELECT id FROM memories
 WHERE status = 'SUPERSEDED' AND superseded_by_id IS NULL;

-- dedup key pointing at a non-active memory
SELECT d.content_hash FROM memory_dedup_keys d JOIN memories m ON m.id = d.memory_id
 WHERE m.status <> 'ACTIVE';

-- embedding for a non-existent revision  (should be impossible: FK)
-- retired revision carrying is_current                     (impossible: partial unique)
```

The last two are listed even though the schema makes them impossible — running them proves the constraints are actually deployed, which catches a migration that silently failed to apply an index.

### 12.4 Idempotency test

50 concurrent `remember` calls, same `client_request_id`, same payload → exactly one `memories` row, one `memory_revisions` row, all 50 responses carry the same `memory_id` and `revision_no`, exactly one has `outcome="created"` and 49 have `outcome="idempotent_replay"`. A variant with a *different* payload under the same key asserts `IDEMPOTENCY_KEY_REUSED` for the losers.

### 12.5 Retrieval evaluation

- **Corpus:** 200 memories across 3 synthetic projects, hand-written, committed as YAML. Deliberately includes 15 superseded/current pairs (the Redis/Postgres shape), near-duplicates, and cross-project traps ("the queue" mentioned in two projects).
- **Queries:** ~60, each with graded relevance (2 = must appear, 1 = useful, 0 = irrelevant) and, for the stale pairs, an explicit `must_not_appear` list.
- **Embeddings:** a deterministic fake (seeded hash → unit vector with controlled cosine structure) so CI needs **no external API and no GPU**, plus a real-embedder run behind a `--real-embeddings` marker.
- **Metrics:** `nDCG@10` (primary), `Recall@10`, and **`stale_inclusion_rate` — the fraction of queries where a superseded memory appears in the top 10. Target: exactly 0.**
- **Comparison table** committed at `docs/eval/results.md`: FTS-only vs vector-only vs hybrid, so the claim "hybrid is better" is a number with a date on it, not an assertion.
- **CI regression gate:** metrics are compared against a committed baseline JSON; a drop beyond a tolerance fails the build. This is the thing that turns an eval harness from a one-off demo into engineering.

`stale_inclusion_rate` is the metric that proves the project's thesis. Recall and nDCG show the retrieval is competent; stale inclusion shows it solves the problem the retrieval-only systems do not.

### 12.6 Protocol tests

Driven through an in-memory MCP client/server pair — no subprocess, no Claude Desktop:

- `tools/list` matches a **committed golden snapshot**, including descriptions. Descriptions are behaviour (§3.4b); an accidental edit should fail CI.
- A revision conflict returns `isError: true` with a structured payload — **asserted specifically**, because returning it as a JSON-RPC error is the easy regression.
- Resource URIs: valid ones resolve; malformed UUIDs, traversal attempts, and cross-project reads are rejected.
- Every tool's declared output schema matches what it actually returns.

### 12.7 Migration tests

`upgrade head` on empty → seed → `downgrade -1` → `upgrade head`, asserting data survives where the migration claims it should. Plus a **drift test**: `alembic revision --autogenerate` against the migrated schema must produce an empty diff. That single test catches the whole class of "someone changed the model and forgot the migration".

### 12.8 Performance

At 1k / 10k / 100k memories: write p50/p95/p99, search p50/p95/p99 per strategy, context-build latency, concurrent write throughput, pool saturation behaviour. **`EXPLAIN ANALYZE` output committed** for the five hot queries at each scale, before any optimisation. No cache is added until a plan justifies it.

---

## 13. Invariants (enforced, not documented)

| # | Invariant | Enforced by |
|---|---|---|
| 1 | Exactly one current revision per memory | `UNIQUE (memory_id) WHERE is_current` |
| 2 | Revision numbers unique and gapless per memory | `PRIMARY KEY (memory_id, revision_no)` + CAS increment |
| 3 | A stale write can never overwrite the current revision | CAS `UPDATE ... WHERE current_revision_no = :expected` (§5.3) |
| 4 | A memory can only be superseded within its own project | composite FK `(superseded_by_id, project_id) -> (id, project_id)` |
| 5 | A memory cannot supersede itself | `CHECK (superseded_by_id IS DISTINCT FROM id)` |
| 6 | Status columns and their timestamps agree | paired `CHECK` constraints |
| 7 | An idempotency key produces at most one write | `PRIMARY KEY (project_id, client_request_id)` + `ON CONFLICT DO NOTHING` |
| 8 | No two active memories share normalised content | `memory_dedup_keys` primary key |
| 9 | Superseded / deleted / expired never appear in normal retrieval | the single stage-0 predicate (§7.1), one function, one place |
| 10 | Every revision belongs to a memory in the same project | composite FK on `memory_revisions` |
| 11 | Every `TASK` has an expiry | `CHECK (type <> 'TASK' OR expires_at IS NOT NULL)` |
| 12 | Content is never destroyed by a normal operation | append-only table; no `UPDATE`/`DELETE` on content outside the audited purge path |
| 13 | A vector never exists without its revision | FK with `ON DELETE CASCADE` |
| 14 | Every timestamp comes from the database clock | server-side `now()` defaults; no application-supplied timestamps |

Nine of fourteen are schema-level. That ratio is the point.

---

## 14. Repository structure

```
mcp_shared_memory_hub/
  src/memhub/
    domain/          models.py enums.py errors.py invariants.py   # pure, no I/O
    services/        projects.py remember.py revise.py forget.py
                     dedup.py idempotency.py context.py
    persistence/     engine.py uow.py
                     repositories/{projects,memories,revisions,embeddings,audit}.py
                     sql/{cas_revise.sql, claim_idempotency.sql, claim_jobs.sql}
    retrieval/       filters.py lexical.py semantic.py fusion.py ranking.py
    embeddings/      base.py fake.py local.py remote.py worker.py
    observability/   logging.py metrics.py tracing.py
    mcp/             server.py tools/ resources/ mapping.py schemas.py
    cli/             admin.py            # purge, retention, reindex — NOT over MCP
    config.py
  migrations/
  tests/             unit/ integration/ concurrency/ protocol/ eval/ perf/ failure/
  eval/dataset/      memories.yaml queries.yaml baseline.json
  docs/              architecture.md decisions/ perf/ eval/
  docker-compose.yml  pyproject.toml  .github/workflows/
```

Rules: MCP handlers are ≤ 20 lines (validate, call a service, map the result). No SQL outside `persistence/`. No `services.py`. `domain/` imports nothing from the other packages. The one hand-written-SQL exception is the concurrency-critical statements in `persistence/sql/`, kept as readable `.sql` files precisely because they are the part a reviewer should read closely.

An `docs/decisions/` folder of short ADRs (one per major call in this document) is worth more to an interviewer than any amount of code comments.

---

## 15. Revised roadmap

Each milestone has an exit criterion. Nothing is "done" without it.

| # | Milestone | Contents | Exit criterion |
|---|---|---|---|
| **0** | Skeleton | Docker Compose (Postgres + pgvector), Alembic, ruff, mypy strict, pytest, CI, structured logging to stderr | `docker compose up` + `pytest` green in CI |
| **1** | **End-to-end thin slice** | `projects` + `memories` + `memory_revisions`, `project_use`/`memory_remember`/`memory_search` (exact match only), stdio server, **wired into a real client** | A real MCP client writes a memory and reads it back in a new session |
| **2** | Correctness core | CAS revise, immutable revisions, all §13 constraints, idempotency, audit log, core metrics + traces | 50-way concurrency test and 50-way idempotency test green; invariant suite green |
| **3** | Truth maintenance | dedup keys + attestations, `supersedes` in `remember`, `forget`, expiry, `memory_history` | The stale-memory demo (§16.2) passes as an automated test |
| **4** | Client integration | Claude Desktop + Cursor configs verified against current docs, resources, protocol test suite + golden manifest | Both clients share state; demo §16.1 runs end to end |
| **5** | Lexical retrieval | `tsvector`, GIN, `ts_rank_cd`, stage-0 filter as one function, ranking priors | FTS search p95 < 50 ms at 10k |
| **6** | **Eval harness** | 200-memory corpus, 60 graded queries, nDCG/Recall/stale-inclusion, fake embedder, **FTS baseline committed** | Baseline numbers in `docs/eval/results.md` |
| **7** | Semantic + hybrid | pgvector, outbox worker, `EmbeddingPort`, RRF fusion, filtered-ANN recall fix, weight tuning | Hybrid beats FTS baseline on nDCG@10 with the delta published |
| **8** | Context builder | quotas, MMR, knapsack fill, token estimator + calibration, budget report | 100% budget adherence, 0% stale inclusion, p95 < 150 ms |
| **9** | Failure + performance | fault injection, degradation paths, retention, 1k/10k/100k benchmarks, committed `EXPLAIN ANALYZE` | Failure matrix (§9) fully covered by tests |
| **10** | Polish | README, ADRs, demo scripts, Grafana dashboard if the data warrants it | A stranger can clone, run, and see both demos in 10 minutes |

**Dependency reasoning behind the reordering** (versus your Phase 0–11):

- **Milestone 1 exists** because everything downstream assumes MCP behaves as documented. Validate that in week one, against a real client, before it can invalidate three phases of work.
- **Idempotency merged into Milestone 2**, not a separate phase: it changes the signature of every write. Retrofitting it means rewriting the write API and every test that calls it.
- **Supersession pulled from Phase 8 to Milestone 3.** It is the thesis. A late thesis is a bolt-on.
- **Eval (6) before vectors (7).** Build the ruler before the thing you want to measure, or you will measure what you built.
- **Observability moved into Milestone 2.** It is a debugging tool for the concurrency work, not a garnish.
- **Testing is not a phase.** Each milestone ships its tests. A "testing phase" at the end is where projects die.

---

## 16. The two demos, as automated tests

Both flagship demos should exist as **pytest integration tests first** and shell/asciinema demos second. A demo that is also a test cannot rot.

### 16.1 Interoperability + concurrency

Two independent connections standing in for two client processes: A writes the Postgres-queue decision; B reads it via `memory_context` in a fresh session; B revises it to revision 2; A attempts a revise with `expected_revision=1` and receives `REVISION_CONFLICT` carrying B's content; `memory_history` shows v1 → v2 with distinct `author_client` values; search returns only v2. Traces show both callers hitting one database.

### 16.2 Stale memory

`remember(FACT, "Redis is the task queue")` → later `remember(DECISION, "PostgreSQL is the task queue; Redis removed in V1", supersedes=[redis_id])` → `search("what queue does this project use")` returns **only** the Postgres decision, and asserts the Redis memory is absent from the results *and* absent from `memory_context` at every budget from 200 to 8000 tokens → `memory_history(redis_id)` still shows it, with `superseded_by` pointing at the decision and the timestamp of retirement.

The second assertion — absent at *every* budget — is the one that matters, because it proves suppression is structural rather than an accident of ranking.

---

## 17. Scope cuts

Cut for V1, beyond your list, with reasons:

| Cut | Why |
|---|---|
| MCP prompts, sampling, elicitation | primitive-count theatre |
| A `memory_links` graph (`RELATES_TO`, `REFINES`) | no concrete use yet; `superseded_by_id` covers the case that matters |
| Automatic semantic merging | destroys real distinctions on a threshold; surface candidates, let the model decide |
| Automatic extraction/summarisation of conversations | contradicts §1.2 and §10.1 |
| Cross-project memory sharing / inheritance | isolation is a stated invariant; sharing is its opposite and needs its own design |
| A web UI or dashboard | `memory_history` + the CLI cover inspection |
| REST/GraphQL API | MCP **is** the API. A second interface doubles the surface for zero learning. |
| Row-level security | single-tenant; revisit in the remote phase |
| Streaming / partial tool results | responses are kilobytes |
| MCP resource subscriptions, update notifications, subscriber tracking | dynamic context is served by the `memory_context` **tool**, which is always current by construction and needs no invalidation. Read-only resources may still be added where they genuinely improve usability; the cache-coherence machinery is deferred until client integration proves it necessary. |
| Generic `attrs jsonb` on revisions | see §4.4: an extensibility bag undoes the structured model |
| Hosted embedding provider in V1 | the real adapter is a small **local** model, so the demo is reproducible, private, and free of an external API; CI uses the deterministic fake. The port stays provider-independent so a hosted adapter can be added later. |
| Bulk import/export tools | until there is data worth moving |
| Grafana | after Milestone 9, only if the metrics have something to say |
| Memory "confidence decay" models | unfalsifiable without data; recency priors already cover the real need |
| Multi-dimension embedding support | one model at a time; the table key allows it later without a migration |

**Watch item:** if `TASK` starts accumulating states, assignees, or ordering, cut the type entirely. That is the project turning into a bad issue tracker, and it is the most likely scope failure here.

---

## 18. Interview story

### 18.1 Strongest backend concepts

1. **Optimistic concurrency with a real correctness argument.** Not "I added a version column" but "I chose `READ COMMITTED` *because* EvalPlanQual re-checks the predicate against the updated row, which turns a lost-update race into a deterministic 0-row result, and I can show you the 50-way test that proves it and the two unique indexes that would catch it even if my code were wrong."
2. **Invariants in the schema rather than in the application.** Partial unique indexes, composite foreign keys used to make cross-namespace references *unrepresentable*, paired CHECK constraints. Nine of fourteen invariants enforced by the database.
3. **Race-free idempotency**, including the `FOR SHARE` wait on an in-flight duplicate and the fingerprint-mismatch rule — and the sharper point that `revise` does not *need* idempotency for correctness, only for a good error message.
4. **Transactional outbox** for embeddings, with an explicit argument for why the enrichment must not sit inside the write transaction, and `FOR UPDATE SKIP LOCKED` as the claim mechanism.
5. **Append-only content log behind a mutable lifecycle row** — the modelling decision that makes history free and audit trivial.
6. **Measured, not asserted**: `EXPLAIN ANALYZE` committed, an eval baseline with a CI regression gate, a documented token-estimation error bound.

### 18.2 Strongest MCP concepts

1. **A defensible tool/resource boundary** with a stated rule (identity-addressed and side-effect-free versus model-composed or mutating) — *and* the honest note that client capability differences force `memory_history` to be a tool anyway.
2. **Domain errors as tool results, protocol errors as JSON-RPC errors**, with the conflict payload carrying everything the model needs to recover in one round trip. This is the MCP design question most people get wrong.
3. **Understanding what MCP is not.** Being able to say "the server sees tool arguments, not transcripts, so this is shared memory and not history sync" is a credibility marker — most MCP portfolio projects overclaim exactly here.
4. **Transport shaping architecture**: stdio means N processes and no shared memory, which is *why* the database has to be the synchronisation primitive. The concurrency work is not decoration; the transport requires it.
5. **Tool descriptions treated as production surface** under golden-snapshot test.

### 18.3 The three hardest problems

1. **Separating revision from supersession, and making suppression structural.** Realising these are different relations with different cardinalities, then proving that no retrieval path can surface a retired memory at any budget. This is where a naive design collapses: it either loses authorship by forcing everything into a revision chain, or it relies on ranking to bury stale facts, which fails the moment the stale phrasing matches the query better.
2. **Lost-update prevention across independent OS processes, proven deterministically.** The mechanism is short; the difficulty is arguing *why* it is correct at the storage-engine level, choosing the isolation level for its conflict semantics, and building a test that actually achieves 50-way concurrency (the connection-pool trap) rather than one that passes for the wrong reason.
3. **Context budgeting.** Turning "give me relevant memories" into a constrained optimisation — quota allocation, MMR diversity, greedy knapsack fill, deterministic ordering — against a token estimator that *cannot be exact* because the server does not know the client's tokeniser. Handling that honestly (documented bias, measured error, hard adherence guarantee) is more mature than faking precision.

### 18.4 What makes an interviewer call it deep

- Every significant claim has a number and a committed artifact behind it: an `EXPLAIN ANALYZE`, an eval baseline, a latency histogram, a passing invariant suite.
- The failure matrix in §9 exists and is covered by tests. Most portfolio projects have no failure model at all.
- The document argues *against* parts of its own brief (§0) and against its own stack (§1.3 on SQLite). Demonstrated judgment beats demonstrated enthusiasm.
- The invariants are enforced where they cannot be bypassed, and there is a test that proves the constraints are actually deployed.
- The AI angle is the *use case*, not the substance. Under the MCP surface it is a versioned store with CAS, an outbox, hybrid retrieval, and a budgeted selection algorithm. That transfers to any backend team.

### 18.5 README positioning

Use your accurate long form. Do not write resume bullets until Milestones 6–9 produce the numbers; the strong version of the bullet contains a measurement, and we do not have one yet.

The line to never write: anything implying the service reads conversations.

---

## 19. Locked decisions (v0.2)

The four questions this document previously left open are now closed. They are recorded here rather
than edited invisibly into the sections above, so the reasoning stays visible.

| Decision | Resolution |
|---|---|
| **`TASK` type** | **Kept, narrowly.** Short-lived working state only. Distinct lifecycle = mandatory short TTL + 7-day recency half-life + smallest context quota. No assignees, boards, sub-tasks, estimates, or workflow states. `BUG`, `SOLUTION`, `OBSERVATION` remain tags or provenance unless implementation proves they need distinct *system behaviour*. See §4.3. |
| **Embedding provider** | **Local model for the real/demo adapter** — reproducible, private, no external API in V1. **Deterministic fake for CI** — fast, hermetic, no network. `EmbeddingPort` stays provider-independent so a hosted adapter is a later addition, not a rewrite. |
| **Generic `attrs jsonb`** | **Removed from V1.** If provenance later needs provider-specific metadata, add a narrowly scoped `source_metadata` with explicit limits and a documented purpose. No generic extensibility without a concrete use case. See §4.4. |
| **MCP resource subscriptions** | **Deferred.** Dynamic context is served by the `memory_context` tool, which is current by construction. Read-only resources may still be added for usability, but update notifications, subscriber tracking, and cache invalidation wait until Claude Desktop / Cursor integration proves they are needed. See §17. |

Unchanged and confirmed: revision and supersession are separate concepts; `EXPIRED` is derived, not
stored; RRF is the initial hybrid-ranking strategy; the evaluation harness is built before
pgvector; the transactional outbox drives embedding generation; PostgreSQL is the only durable
source of truth; no Redis in V1; optimistic concurrency is single-statement CAS at `READ COMMITTED`;
project isolation is enforced by database constraints wherever possible; idempotency and
deduplication remain separate concepts; stale-memory inclusion is a first-class correctness metric
with a target of exactly zero in normal retrieval; concurrency tests must prove real concurrent
database access and must not serialize through a small connection pool.

**Change policy.** This document is closed to expansion. It changes only when implementation
surfaces a decision it genuinely does not cover — and such a change is a small, dated amendment,
not a new section.
