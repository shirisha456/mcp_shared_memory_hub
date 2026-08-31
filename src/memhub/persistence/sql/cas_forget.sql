-- Tombstone a memory.
--
-- Reversible by design, and content-preserving: this sets a status, it does not
-- delete a row. Every revision stays in the append-only log, so memory_history
-- can still answer "what did this say, and when did it stop applying?".
--
-- The only path that actually destroys content is an operator purge, which is
-- deliberately not reachable over MCP. A model should not hold an irreversible,
-- unrecoverable delete in its tool surface - and soft delete is the wrong tool
-- for the case that genuinely needs destruction (a credential stored by
-- mistake), which is precisely why that path is human-invoked and audited.
--
-- Compare-and-set on status, so this serialises with revise and supersede on
-- the same row lock. Zero rows means it was already retired: that is reported
-- as an idempotent no-op rather than an error, because forgetting something
-- twice is not a mistake worth failing on.

UPDATE memories
   SET status     = 'DELETED',
       deleted_at = now(),
       updated_at = now()
 WHERE id         = :memory_id
   AND project_id = :project_id
   AND status     = 'ACTIVE'
RETURNING id;
