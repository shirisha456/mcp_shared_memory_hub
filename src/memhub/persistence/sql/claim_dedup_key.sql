-- Claim the dedup key for a piece of content, or discover who already holds it.
--
-- One statement, no check-then-insert, so two clients asserting the same fact at
-- the same instant cannot both create a memory. The primary key adjudicates.
--
-- Returns the claiming memory_id on success. Zero rows means someone else got
-- there first; the caller then reads the existing holder and records an
-- attestation instead of creating a duplicate.
--
-- Note this is deduplication, not idempotency. Idempotency is one client
-- retrying one request, keyed on a caller-supplied request id. This is two
-- *different* clients independently asserting the same fact, keyed on content.
-- The right answers differ too: a retry replays its own response, whereas a
-- duplicate assertion returns the existing memory and adds corroboration.

INSERT INTO memory_dedup_keys (project_id, hash_version, content_hash, memory_id)
VALUES (:project_id, :hash_version, :content_hash, :memory_id)
ON CONFLICT (project_id, hash_version, content_hash) DO NOTHING
RETURNING memory_id;
