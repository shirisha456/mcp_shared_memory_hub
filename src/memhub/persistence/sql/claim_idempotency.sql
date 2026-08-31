-- Claim an idempotency key.
--
-- "SELECT to see if it exists, then INSERT" races: two retries can both pass
-- the SELECT and both insert. This single statement cannot, because the primary
-- key adjudicates atomically.
--
-- Returns one row if this caller now owns the key, zero rows if someone else
-- already does.
--
-- SUBTLETY WORTH KNOWING. Under READ COMMITTED, if a *concurrent uncommitted*
-- transaction has already inserted this key, ON CONFLICT DO NOTHING does not
-- return immediately - it waits for that transaction to finish, then returns
-- zero rows. So a zero-row result here already means "the other writer has
-- committed or rolled back", not "might still be in flight". The follow-up
-- SELECT ... FOR SHARE in wait_idempotency.sql then distinguishes the two:
-- a committed owner left a row behind, a rolled-back one did not.

INSERT INTO idempotency_keys
    (project_id, client_request_id, operation, request_fingerprint, state)
VALUES
    (:project_id, :client_request_id, :operation, :request_fingerprint, 'IN_PROGRESS')
ON CONFLICT (project_id, client_request_id) DO NOTHING
RETURNING client_request_id;
