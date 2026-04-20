-- Schema v2 — add pre/post tool-call lifecycle columns.
-- Applied in order by lib.db._migrate when PRAGMA user_version < 2.
--
-- status enum values: pending | ok | err | denied | orphan
--   pending — pre row inserted, post not yet seen
--   ok/err  — post row updated, success/failure
--   denied  — blocked by permission hook before execution
--   orphan  — pre row present but no post ever arrived (set by janitor, Phase 5)

ALTER TABLE tool_calls ADD COLUMN tool_use_id TEXT;
ALTER TABLE tool_calls ADD COLUMN completed_ts TEXT;
ALTER TABLE tool_calls ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';

CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_use_id ON tool_calls(tool_use_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_status_ts ON tool_calls(status, ts)
  WHERE status = 'pending';
