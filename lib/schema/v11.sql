-- v11: scope classification for tool calls.
--
-- Every tool call gets classified against its session's project root
-- ("within_project" / "outside_project" / "mixed" / "no_path"). The "any"
-- scope is reserved for permission rules that should match regardless of
-- the tool call's scope — it's never stored on tool_calls itself.
--
-- Wave 1: infrastructure only (capture + store). Wave 2 layers
-- session-approvals on top; Wave 3 adds scope to the global permission
-- tables.

-- Project root (resolved at project creation): strip trailing /repository
-- (three-dir workspace convention), else git toplevel, else cwd.
ALTER TABLE projects ADD COLUMN root TEXT;

-- Scope lookup table. Use real rows (not NULL) so composite unique indexes
-- downstream work cleanly.
CREATE TABLE tool_call_scopes (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT    UNIQUE NOT NULL
);
INSERT INTO tool_call_scopes (name) VALUES
  ('within_project'),
  ('outside_project'),
  ('mixed'),
  ('no_path'),
  ('any');

-- FK column on tool_calls. NULL during migration; recorder populates for
-- new rows. Backfilling old rows is intentionally skipped — pre-v11 data
-- pre-dates the project_root concept.
ALTER TABLE tool_calls ADD COLUMN scope_id INTEGER REFERENCES tool_call_scopes(id);
CREATE INDEX idx_tool_calls_scope_id ON tool_calls(scope_id);

-- Refresh v_tool_calls to expose the scope name alongside the other
-- FK-resolved columns. DROP first because SQLite treats CREATE VIEW as
-- idempotent only for the same definition.
DROP VIEW IF EXISTS v_tool_calls;
CREATE VIEW v_tool_calls AS
SELECT
  tc.id,
  tc.ts,
  tc.completed_ts,
  t.name        AS tool,
  cs.name       AS status,
  pm.name       AS permission_mode,
  st.name       AS subagent_type,
  fp.path       AS file_path,
  sc.name       AS scope,
  tc.command,
  tc.description,
  tc.pattern,
  s.session_uuid AS session_uuid,
  p.cwd         AS project_cwd,
  p.name        AS project_name,
  p.root        AS project_root,
  tc.tool_use_id,
  tc.ok,
  tc.args_json
FROM tool_calls tc
LEFT JOIN tools              t  ON t.id  = tc.tool_id
LEFT JOIN call_statuses      cs ON cs.id = tc.status_id
LEFT JOIN permission_modes   pm ON pm.id = tc.permission_mode_id
LEFT JOIN subagent_types     st ON st.id = tc.subagent_type_id
LEFT JOIN file_paths         fp ON fp.id = tc.file_path_id
LEFT JOIN tool_call_scopes   sc ON sc.id = tc.scope_id
LEFT JOIN sessions           s  ON s.id  = tc.session_id
LEFT JOIN projects           p  ON p.id  = tc.project_id;
