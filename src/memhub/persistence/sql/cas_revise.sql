-- Compare-and-set on a logical memory.
--
-- This one statement is the entire concurrency mechanism, and the correctness
-- argument rests on three properties of PostgreSQL under READ COMMITTED.
--
-- 1. THE UPDATE TAKES A ROW-LEVEL EXCLUSIVE LOCK.
--    Two transactions both matching this row cannot proceed together. The
--    second blocks on the first's lock until it commits or rolls back.
--
-- 2. EvalPlanQual RE-CHECKS THE PREDICATE AGAINST THE *NEW* ROW VERSION.
--    This is the crux, and it is specific to READ COMMITTED. When the first
--    transaction commits, the second wakes up. PostgreSQL does not simply
--    proceed with the row version the second transaction originally found: it
--    walks to the newest committed version and re-evaluates this WHERE clause
--    against it. The loser asked for current_revision_no = 4; the row now says
--    5; the predicate fails; the row is skipped; the UPDATE reports 0 rows.
--
--    So the conflict is detected by the storage engine's own visibility
--    machinery. There is no read-then-write window to lose, because the read
--    and the write are the same statement.
--
-- 3. ZERO ROWS IS A DETERMINISTIC ANSWER, NOT AN EXCEPTION.
--    Under REPEATABLE READ or SERIALIZABLE the same collision raises
--    40001 (could not serialize access), which the application must catch and
--    retry. That is also correct, but it turns a clean domain outcome into a
--    retry loop - and with 50 concurrent writers it produces 49 retry storms.
--    READ COMMITTED plus an explicit CAS gives 1 success and 49 conflicts,
--    every time, with no retries. The isolation level is chosen *for* these
--    conflict semantics.
--
-- LOCK ORDERING. Every write path takes this row lock first, before touching
-- memory_revisions. A single consistent lock order across all writers means no
-- cycle can form, so this cannot deadlock.
--
-- DEFENCE IN DEPTH. If this statement were skipped by a buggy code path, two
-- writers could still not corrupt the corpus: PRIMARY KEY (memory_id,
-- revision_no) rejects a duplicate revision number, and the partial unique
-- index uq_memory_revisions_memory_id rejects a second current revision. The
-- invariant survives wrong application code.
--
-- The status condition matters as much as the revision condition: it makes
-- forget and supersede participate in the same serialisation point rather than
-- racing around it, and it means a revise against a tombstoned or superseded
-- memory reports a conflict instead of resurrecting it.

UPDATE memories
   SET current_revision_no = current_revision_no + 1,
       updated_at          = now()
 WHERE id                  = :memory_id
   AND project_id          = :project_id
   AND current_revision_no = :expected_revision
   AND status              = 'ACTIVE'
RETURNING current_revision_no AS new_revision;
