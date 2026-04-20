-- v10: convenience views over the observability schema.
--
-- Views are cheap (zero storage cost, always up-to-date) and make ad-hoc
-- sqlite3 sessions pleasant — no more typing a 6-JOIN query to figure out
-- what shapes are active or which Bash calls ran in the last hour. Treat
-- them as the user-facing query surface.
--
-- All views resolve FK lookups back to their string names, so queries read
-- naturally: `SELECT * FROM v_recent_bash WHERE status='denied';`
-- works without remembering that status_id=4 means denied.
--
-- Adding or changing a view? Drop + recreate in this file — the migration
-- is only applied once per DB (via PRAGMA user_version). To amend an
-- existing view after v10 has applied, write a v11 that DROPs it and
-- recreates it.

-- Tool calls with string-valued FK resolutions. The recorder's hot table
-- normalised down to int FKs for storage; this view undoes that for
-- human queries.
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
  tc.command,
  tc.description,
  tc.pattern,
  s.session_uuid AS session_uuid,
  p.cwd         AS project_cwd,
  p.name        AS project_name,
  tc.tool_use_id,
  tc.ok,
  tc.args_json
FROM tool_calls tc
LEFT JOIN tools          t  ON t.id  = tc.tool_id
LEFT JOIN call_statuses  cs ON cs.id = tc.status_id
LEFT JOIN permission_modes pm ON pm.id = tc.permission_mode_id
LEFT JOIN subagent_types st ON st.id = tc.subagent_type_id
LEFT JOIN file_paths     fp ON fp.id = tc.file_path_id
LEFT JOIN sessions       s  ON s.id  = tc.session_id
LEFT JOIN projects       p  ON p.id  = tc.project_id;

-- Bash calls only, sorted most-recent first. Shortcut for the common
-- case of "what Bash commands ran recently".
CREATE VIEW v_recent_bash AS
SELECT id, ts, completed_ts, status, permission_mode, command, project_name
  FROM v_tool_calls
 WHERE tool = 'Bash'
 ORDER BY ts DESC;

-- Command shapes, already-resolved flags rendered as plain JSON. Useful
-- when you want to eyeball the shape universe without JSON-decoding the
-- flags column yourself.
CREATE VIEW v_shapes AS
SELECT
  id,
  verb,
  subcommand,
  flags,
  first_seen,
  last_seen
FROM command_shapes;

-- Active allowlist joined to its shape. One row per auto-approval rule.
CREATE VIEW v_active AS
SELECT
  cs.verb,
  cs.subcommand,
  cs.flags,
  pa.source,
  pa.promoted_at
FROM permission_active pa
JOIN command_shapes cs ON cs.id = pa.command_shape_id
ORDER BY cs.verb, IFNULL(cs.subcommand, ''), cs.flags;

-- Rejected shapes joined to their shape. One row per explicit "no".
CREATE VIEW v_rejected AS
SELECT
  cs.verb,
  cs.subcommand,
  cs.flags,
  r.reason,
  r.rejected_at
FROM permission_rejected r
JOIN command_shapes cs ON cs.id = r.command_shape_id
ORDER BY cs.verb, IFNULL(cs.subcommand, ''), cs.flags;

-- Candidates with eligibility flags. `eligible` = meets thresholds and
-- not already active, not rejected. `above_obs_threshold` and
-- `above_session_threshold` let you see how close a shape is to being
-- proposed without consulting learner.toml.
CREATE VIEW v_candidates AS
SELECT
  cs.verb,
  cs.subcommand,
  cs.flags,
  c.observations,
  c.distinct_sessions,
  c.first_seen,
  c.last_seen,
  CASE WHEN a.command_shape_id IS NOT NULL THEN 1 ELSE 0 END AS already_active,
  CASE WHEN r.command_shape_id IS NOT NULL THEN 1 ELSE 0 END AS already_rejected
FROM permission_candidates c
JOIN command_shapes cs ON cs.id = c.command_shape_id
LEFT JOIN permission_active   a ON a.command_shape_id = c.command_shape_id
LEFT JOIN permission_rejected r ON r.command_shape_id = c.command_shape_id
ORDER BY c.observations DESC, c.last_seen DESC;

-- How many times each active shape has actually fired. Useful for
-- deciding which entries in permission_active to prune (zero observations
-- = speculative seed that never paid off).
CREATE VIEW v_active_usage AS
SELECT
  cs.verb,
  cs.subcommand,
  cs.flags,
  pa.source,
  COUNT(tcs.tool_call_id) AS observations
FROM permission_active pa
JOIN command_shapes cs ON cs.id = pa.command_shape_id
LEFT JOIN tool_call_shapes tcs ON tcs.command_shape_id = cs.id
GROUP BY pa.command_shape_id
ORDER BY observations DESC, cs.verb, cs.flags;

-- Top shapes overall by observation count. Handy for "which commands does
-- Claude actually run?" queries without needing permission_active at all.
CREATE VIEW v_shape_usage AS
SELECT
  cs.verb,
  cs.subcommand,
  cs.flags,
  COUNT(tcs.tool_call_id) AS observations,
  MIN(tc.ts)              AS first_observed,
  MAX(tc.ts)              AS last_observed
FROM command_shapes cs
JOIN tool_call_shapes tcs ON tcs.command_shape_id = cs.id
JOIN tool_calls       tc  ON tc.id                = tcs.tool_call_id
GROUP BY cs.id
ORDER BY observations DESC;

-- Session summary: start/end timestamps, call count, denials, project.
CREATE VIEW v_session_summary AS
SELECT
  s.id              AS session_id,
  s.session_uuid,
  p.name            AS project_name,
  p.cwd             AS project_cwd,
  s.started_at,
  s.last_activity,
  s.transcript_path,
  COUNT(tc.id)                                              AS total_calls,
  SUM(CASE WHEN cs.name='ok'      THEN 1 ELSE 0 END)        AS ok_calls,
  SUM(CASE WHEN cs.name='err'     THEN 1 ELSE 0 END)        AS err_calls,
  SUM(CASE WHEN cs.name='denied'  THEN 1 ELSE 0 END)        AS denied_calls,
  SUM(CASE WHEN cs.name='orphan'  THEN 1 ELSE 0 END)        AS orphan_calls,
  SUM(CASE WHEN cs.name='pending' THEN 1 ELSE 0 END)        AS pending_calls
FROM sessions s
LEFT JOIN projects      p  ON p.id  = s.project_id
LEFT JOIN tool_calls    tc ON tc.session_id = s.id
LEFT JOIN call_statuses cs ON cs.id = tc.status_id
GROUP BY s.id
ORDER BY s.last_activity DESC;
