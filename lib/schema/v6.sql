-- Schema v6 — drop legacy TEXT `status` column from tool_calls.
-- Applied in order by lib.db._migrate when PRAGMA user_version < 6.
--
-- Per plan "Phase 3.5 — v5 schema expansion" in
--   ~/.claude/.claude/plans/observability-module.md
--
-- Rationale:
--   v5 introduced the `status_id` FK into `call_statuses` as the
--   single-source-of-truth for call status, but left the legacy TEXT
--   `status` column in place for transitional readers. With the recorder
--   now writing `status_id` exclusively, the TEXT column is a drift
--   hazard — every row written by an older recorder build arrives with
--   TEXT-only status and no FK, forcing a running backfill to stay in
--   sync. Dropping the column eliminates the class of drift entirely.
--
-- Prerequisites (enforced by the operator before running v6, NOT by this
-- migration):
--   * Every non-null `status` must already map to a matching `status_id`
--     — run the backfill in scripts/setup.sh (or equivalent) and verify
--     `SELECT COUNT(*) FROM tool_calls WHERE status IS NOT NULL AND
--     status_id IS NULL` returns 0 before applying v6. Otherwise the
--     drop loses data.
--
-- SQLite ≥ 3.35 supports DROP COLUMN natively. The observability venv
-- bundles 3.40+, well past that threshold.

-- The partial index `idx_tool_calls_status_ts` references the `status`
-- column (it's `WHERE status = 'pending'`). SQLite refuses to drop a
-- column that any index references, so drop the index first. A
-- functionally equivalent pending-row index on `status_id` already
-- exists as idx_tool_calls_status_id (covers everything, not just
-- pending); we don't recreate the partial form because status_id is
-- cheap to filter on.
DROP INDEX IF EXISTS idx_tool_calls_status_ts;

ALTER TABLE tool_calls DROP COLUMN status;
