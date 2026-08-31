-- Wait for the transaction that owns this idempotency key, then read its result.
--
-- FOR SHARE is the point of this statement. It takes a shared row lock, which
-- blocks until the owning transaction commits or rolls back. That is how a
-- duplicate request waits for the original without polling and without a sleep
-- loop.
--
-- Two outcomes, and they are distinguished by whether a row comes back at all:
--
--   ONE ROW  - the owner committed. state is COMPLETED and `response` holds the
--              result to replay. The caller returns that instead of writing
--              anything, so the retry produces no second memory.
--
--   NO ROWS  - the owner rolled back, which rolled its INSERT back too. The key
--              is free. The caller loops and tries to claim it again.
--
-- The bounded retry in the caller matters: without it, two clients repeatedly
-- failing and retrying the same key could ping-pong indefinitely.

SELECT state,
       response,
       request_fingerprint,
       operation
  FROM idempotency_keys
 WHERE project_id        = :project_id
   AND client_request_id = :client_request_id
   FOR SHARE;
