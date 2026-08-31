# Failure modes: what is claimed, and what proves it

Architecture §9 lists eighteen ways this system can fail and states what it does
about each. A table of intentions is easy to write. This document maps every row
to the test that holds it up, and is candid about the three rows where no test
can and an argument has to do instead.

Written after the fact, and it found things. Three of the error codes §9
promised — `BACKEND_UNAVAILABLE`, `BACKEND_BUSY`, `UNKNOWN_OUTCOME` — did not
exist anywhere in the code, and the deadline row claimed a degradation path that
only triggered when the *embedder* failed, not when a query was cancelled. Both
gaps are now closed. That is the argument for writing this kind of document at
all: the prose had been true when it was written and had quietly stopped being
true, and nothing else would have noticed.

## How to read the table

**Covered** means a test fails if the behaviour is removed. Where that is not
obvious, the mechanism was mutation-tested: deliberately broken, the test
confirmed failing, then restored.

**Argued** means the property is structural — it follows from something that
cannot be turned off, like a single-statement transaction — and a test would be
testing PostgreSQL rather than this system. Each one says why.

---

## Backend failure

| §9 row | Behaviour | Covered by |
|---|---|---|
| Postgres unavailable at startup | `BACKEND_UNAVAILABLE`, nothing written, safe to retry | `test_connecting_to_a_dead_database_fails_fast`, `test_a_refused_connection_is_unavailable`, `test_an_unresolvable_host_is_unavailable`, `test_connection_sqlstates_are_unavailable` |
| Postgres drops mid-transaction | `UNKNOWN_OUTCOME`, **not** blindly retryable | `test_a_lost_connection_mid_flight_is_an_unknown_outcome`, `test_invalidation_outranks_the_sqlstate`, `test_it_names_both_ways_out` |
| Connection pool exhausted | `BACKEND_BUSY`, fail fast, never queue | `test_pool_exhaustion_fails_fast_rather_than_queueing`, `test_pool_exhaustion_is_busy_not_broken` |

The interesting one is the second row. It is the only genuinely ambiguous
failure in the system: the transaction either committed just before the
connection died or it did not, and the acknowledgement that would have said
which is precisely what was lost.

So the classifier does not guess. SQLAlchemy's `DBAPIError.connection_invalidated`
distinguishes a connection that had been established and then died from one that
never opened, and that is exactly the line between *nothing ran* and *something
may have run*. `test_invalidation_outranks_the_sqlstate` pins the branch order,
because both signals are present in a real mid-transaction drop — SQLSTATE
`08006` appears either way — and checking them the other way round would report
every mid-flight disconnect as safe to retry. That is how duplicate writes
happen. Mutation-tested: inverting the order fails three tests.

An unrecognised exception is deliberately *not* classified
(`test_an_unfamiliar_error_is_not_classified`). Dressing an unfamiliar failure
up as a known one would be worse than an opaque error, because it would arrive
with confident and possibly wrong advice about whether retrying is safe.

## Concurrent writers

| §9 row | Behaviour | Covered by |
|---|---|---|
| Two clients revise the same memory | one wins; the other gets the current revision and content | `test_exactly_one_of_fifty_writers_wins`, `test_losers_are_told_what_beat_them`, `test_a_second_round_advances_by_exactly_one`, `test_conflict_is_a_result_not_an_error` |
| Duplicate write from a retry | the stored response is replayed | `test_fifty_concurrent_retries_create_one_memory`, `test_retry_with_an_idempotency_key_replays`, `test_key_reuse_with_a_different_payload_is_rejected` |
| Two clients assert the same fact | the existing memory is returned and an attestation recorded | `test_identical_writes_deduplicate_even_without_a_key`, `test_deduplication_is_reported_as_corroboration` |

Fifty concurrent writers, one winner, forty-nine told what beat them. The
compare-and-set predicate was mutation-tested — dropping the revision check made
the test fail, which is the only evidence that it was ever doing anything.

## Eventual consistency

| §9 row | Behaviour | Covered by |
|---|---|---|
| Embedding provider down | write already committed; job retried with backoff, then `DEAD` | `test_a_broken_embedder_does_not_break_writes`, `test_failure_backs_off_rather_than_spinning`, `test_repeated_failure_ends_in_dead_not_an_infinite_retry` |
| Embedding partially written | cannot happen: vector insert and job completion are one transaction | `test_a_write_enqueues_a_job_in_the_same_transaction`, `test_a_rolled_back_write_leaves_no_job`, `test_two_workers_never_process_the_same_job` |
| Some memories lack embeddings | results still returned; coverage reported | `test_coverage_reports_the_pending_window_honestly`, `test_partial_coverage_is_reported_as_a_fraction`, `test_an_empty_project_is_fully_covered` |

`test_a_rolled_back_write_leaves_no_job` is the one that matters. It is the
difference between a transactional outbox and the dual-write bug: roll the write
back and the job has to disappear with it. If it did not, a memory could exist
with no vector and nothing to notice.

## Deadlines and degradation

| §9 row | Behaviour | Covered by |
|---|---|---|
| Search exceeds deadline | `memory_search` errors; `memory_context` degrades and says so | `test_a_statement_timeout_is_enforced_by_the_server`, `test_the_configured_timeout_reaches_the_backend`, `test_a_cancelled_statement_is_a_deadline_not_an_outage`, `test_a_semantic_leg_over_the_deadline_degrades_rather_than_fails` |
| Oversized content | rejected at 8 KB with the actual size | `test_oversized_content_is_rejected_by_the_database`, `test_oversized_content_is_rejected_with_the_actual_size` |
| Malformed tool input | protocol error naming the fields; nothing touched | `test_malformed_uuid_is_rejected`, `test_unknown_type_is_rejected_with_the_allowed_values`, `test_missing_project_is_a_tool_error_not_a_protocol_error` |

**A correction to §9.** The degraded marker is `lexical_only: <reason>`, not
`fts_only` as written in the architecture. The mechanism is as described; the
string is not.

**And a gap that was open until now.** The degradation path caught
`EmbeddingError` only. A cancelled statement in the semantic leg — which is the
leg most likely to hit the timeout, being an HNSW probe over the whole project —
propagated and took the entire search down, the exact opposite of what §9
promised. It now degrades, because the lexical results are already in hand by
that point and half an answer beats an error.

Only `57014` degrades. `test_an_unrelated_database_error_is_not_swallowed`
exists because catching every `DBAPIError` there would turn a genuine bug in the
vector query — a bad cast, a missing column — into a search that silently
returns lexical results forever while reporting itself as merely degraded. That
is a worse outcome than a crash, since nothing would ever surface it.

There is a subtlety worth stating: after PostgreSQL cancels a statement the
transaction is aborted, so any further query would fail with `25P02`. The
degradation is safe only because nothing downstream issues one — the hydration
step only fetches rows found by the *semantic* retriever, and there are none
when the semantic leg produced nothing. That reasoning is written into the
`except` block, because it is the kind of thing a later refactor breaks without
realising.

## Schema and time

| §9 row | Behaviour | Covered by |
|---|---|---|
| Schema drift, either direction | server refuses to start, prints the migration | `test_an_unmigrated_database_is_refused_with_the_remedy`, `test_an_unknown_revision_is_refused_as_too_new`, `test_a_migrated_database_verifies` |
| Clock skew between processes | all timestamps come from `now()` | `test_expiry_uses_the_database_clock` |

Refusing in *both* directions is the deliberate part. A database that is behind
fails loudly on the first missing column. A database that is *ahead* — migrated
by a colleague, or by a deploy that already rolled forward — mostly works, until
this process writes a row that the newer constraints were added to prevent. The
quiet direction is the dangerous one.

Clock skew is prevented by never having an application clock to skew. The filter
compares against `now()` inside the query, and `test_expiry_uses_the_database_clock`
asserts on the compiled SQL rather than on behaviour, because a Python
`datetime.now()` would produce identical results on a single developer machine
and diverge only in production.

## Retention and erasure

| §9 row | Behaviour | Covered by |
|---|---|---|
| Retention deletes live data | retention never removes a memory; only operator `purge` destroys | `test_garbage_collection_never_removes_a_memory`, `test_expired_idempotency_keys_are_collected`, `test_purge_actually_erases_content`, `test_purge_clears_every_derived_table`, `test_the_audit_record_outlives_the_memory` |

`test_purge_clears_every_derived_table` guards a specific way this could go
wrong. Content is copied into embeddings, dedup keys and attestations, and a
purge that missed one of them would leave a leaked credential recoverable while
reporting success. A partial erasure is not an erasure.

The audit row survives the memory it describes, with its detail redacted. That
is the reason `audit_events` has no foreign key to `memories`: a `CASCADE` would
delete the evidence that a purge happened along with its subject.

---

## The three that are argued, not tested

**Server killed mid-request.** Testing this means killing the process
mid-transaction and inspecting the aftermath, which tests that PostgreSQL rolls
back uncommitted transactions. It does. The property that belongs to *this*
system is that the process holds no authoritative state to lose — every write is
a single transaction, and nothing is cached between calls that a restart would
have to rebuild. `test_a_failed_write_leaves_nothing_behind` and
`test_a_constraint_violation_does_not_leave_an_orphan` cover the same ground
from the reachable side: a write that fails part-way leaves nothing behind.

**Client disconnects mid-call.** From the server's side this is indistinguishable
from a client that stopped reading. The transaction had already committed or had
not, and neither outcome depends on anyone being there to hear it. What the
client does next *is* covered: replaying an idempotency key returns the stored
response, and `test_two_sessions_share_state_over_stdio` shows a reconnecting
session reading what a previous one wrote.

**Embedding partially written.** Listed in §9 as *cannot happen* rather than as a
handled case. The vector insert and the job's completion are one transaction, so
there is no interleaving that produces one without the other. A test would have
to construct a state the database's atomicity forbids.

The honest summary: these three are exactly as strong as the claim that each
write is a single transaction. That claim is worth checking directly, and
`test_a_rolled_back_write_leaves_no_job` is where it is checked.

## Reproducing

```bash
pytest tests/failure tests/concurrency -q
```

The classification tests need no database:

```bash
pytest tests/unit/test_error_classification.py -q
```
