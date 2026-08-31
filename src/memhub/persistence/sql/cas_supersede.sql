-- Retire a memory because a different memory has replaced it.
--
-- SUPERSESSION IS NOT REVISION. A revision is the same logical fact refined
-- (v1 -> v2, one memory, many revisions). Supersession is a *different* memory
-- retiring this one: "Redis is the queue" is retired by "PostgreSQL is the
-- queue", which has its own author, its own timestamp, and may retire several
-- old memories at once. Forcing the new fact to be a revision of the old one
-- would mean lying about who wrote it and when it was first asserted.
--
-- The shape is many-to-one, which is why superseded_by_id is a column rather
-- than a link table: N old memories point at 1 winner. A memory superseded by
-- *two* memories would be a fork, and forks are exactly what we forbid.
--
-- This is a compare-and-set for the same reason revise is. The status predicate
-- means supersede, forget and revise all serialise on the same row lock rather
-- than racing around each other: two clients retiring the same memory
-- concurrently produce one winner and one "already retired", not two
-- conflicting supersession records.
--
-- Zero rows means the memory was already retired, already deleted, or belongs
-- to another project. All three are reported to the caller rather than being
-- silently skipped, because "I retired 3 memories" when only 2 existed is a lie
-- the caller would act on.
--
-- The composite foreign key on memories makes the cross-project case
-- unrepresentable rather than merely unlikely: superseded_by_id and project_id
-- are checked together, so a memory can only ever be retired by a memory in its
-- own project.

UPDATE memories
   SET status           = 'SUPERSEDED',
       superseded_at    = now(),
       superseded_by_id = :winner_id,
       updated_at       = now()
 WHERE id         = :memory_id
   AND project_id = :project_id
   AND status     = 'ACTIVE'
RETURNING id;
