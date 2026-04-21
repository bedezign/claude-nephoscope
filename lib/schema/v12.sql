-- v12: per-session auto-allow for ask-tier Bash calls.
--
-- Pre hook writes one `permission_ask_pending` row per ask'd leaf; Post
-- phase of the recorder promotes matching rows into
-- `permission_session_approvals` on status=ok, and drops them either way.
-- A GC step (see lib/gc_sessions.py) drops stale approvals after 7d of
-- session inactivity and ask_pending rows older than 1h (orphans from
-- user-denied asks, where PostToolUse never fires).
--
-- Keyed on (session, shape, scope) — the same ``rm`` shape approved
-- ``within_project`` does NOT auto-allow ``outside_project`` on the next
-- call. Wave 3 will extend the model to the global permission tables; this
-- migration is session-local only.

-- Short-lived tracking table. PK includes leaf_index because a single
-- Bash call can have multiple ask'd leaves (``rm /a && mv /b /c``).
CREATE TABLE permission_ask_pending (
  tool_use_id      TEXT    NOT NULL,
  leaf_index       INTEGER NOT NULL,
  session_id       INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  command_shape_id INTEGER NOT NULL REFERENCES command_shapes(id),
  scope_id         INTEGER NOT NULL REFERENCES tool_call_scopes(id),
  asked_at         TEXT    NOT NULL,
  PRIMARY KEY (tool_use_id, leaf_index)
);
CREATE INDEX idx_ask_pending_session ON permission_ask_pending(session_id);
CREATE INDEX idx_ask_pending_asked_at ON permission_ask_pending(asked_at);

-- Durable per-session auto-allow. Composite PK = same (shape, scope) pair
-- only recorded once per session regardless of how many times it's approved.
CREATE TABLE permission_session_approvals (
  session_id       INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  command_shape_id INTEGER NOT NULL REFERENCES command_shapes(id),
  scope_id         INTEGER NOT NULL REFERENCES tool_call_scopes(id),
  approved_at      TEXT    NOT NULL,
  PRIMARY KEY (session_id, command_shape_id, scope_id)
);
CREATE INDEX idx_psa_session ON permission_session_approvals(session_id);

-- Convenience view: session approvals with shape fields resolved.
CREATE VIEW v_session_approvals AS
SELECT
  s.session_uuid,
  p.name         AS project_name,
  cs.verb,
  cs.subcommand,
  cs.flags,
  sc.name        AS scope,
  psa.approved_at
FROM permission_session_approvals psa
JOIN sessions          s  ON s.id  = psa.session_id
LEFT JOIN projects     p  ON p.id  = s.project_id
JOIN command_shapes    cs ON cs.id = psa.command_shape_id
JOIN tool_call_scopes  sc ON sc.id = psa.scope_id
ORDER BY s.last_activity DESC, cs.verb;
