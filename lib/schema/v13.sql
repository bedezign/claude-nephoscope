-- v13: scope-aware global permission tables.
--
-- Adds ``scope_id`` to ``permission_active`` and ``permission_rejected``
-- so users can express "rm approved within_project, denied outside". The
-- ``any`` scope sentinel means "match regardless of the call's scope" —
-- use it for truly global rules. Existing rows migrate as scope=any so
-- behaviour is identical until new scope-qualified rules are added.
--
-- Semantics expansion on ``permission_rejected``: was "don't propose this
-- shape as a candidate"; now ALSO "hard-deny at runtime for matching
-- scope". The hook checks rejected BEFORE active and BEFORE ask, so a
-- user rejection is authoritative regardless of other signals.
--
-- The learner's candidate accumulation tables stay scope-agnostic for
-- now — that's a follow-up refinement if per-shape granularity turns out
-- to need per-scope splits.

-- Dependent views (created in v10) reference the tables we're about to
-- replace. SQLite's schema-integrity check rejects ALTER TABLE RENAME
-- while a view has a dangling reference, so drop views first; recreated
-- at the end with scope awareness.
DROP VIEW IF EXISTS v_candidates;
DROP VIEW IF EXISTS v_active_usage;
DROP VIEW IF EXISTS v_active;
DROP VIEW IF EXISTS v_rejected;

-- SQLite < 3.35 can't DROP PRIMARY KEY; use the create-copy-drop dance.
CREATE TABLE permission_active_new (
  command_shape_id INTEGER NOT NULL REFERENCES command_shapes(id),
  scope_id         INTEGER NOT NULL REFERENCES tool_call_scopes(id),
  promoted_at      TEXT    NOT NULL,
  source           TEXT    NOT NULL CHECK (source IN ('learner', 'manual')),
  PRIMARY KEY (command_shape_id, scope_id)
);
INSERT INTO permission_active_new
  (command_shape_id, scope_id, promoted_at, source)
  SELECT
    command_shape_id,
    (SELECT id FROM tool_call_scopes WHERE name = 'any'),
    promoted_at,
    source
  FROM permission_active;
DROP TABLE permission_active;
ALTER TABLE permission_active_new RENAME TO permission_active;

CREATE TABLE permission_rejected_new (
  command_shape_id INTEGER NOT NULL REFERENCES command_shapes(id),
  scope_id         INTEGER NOT NULL REFERENCES tool_call_scopes(id),
  rejected_at      TEXT    NOT NULL,
  reason           TEXT,
  PRIMARY KEY (command_shape_id, scope_id)
);
INSERT INTO permission_rejected_new
  (command_shape_id, scope_id, rejected_at, reason)
  SELECT
    command_shape_id,
    (SELECT id FROM tool_call_scopes WHERE name = 'any'),
    rejected_at,
    reason
  FROM permission_rejected;
DROP TABLE permission_rejected;
ALTER TABLE permission_rejected_new RENAME TO permission_rejected;

-- Rebuild views with scope included.
CREATE VIEW v_active AS
SELECT
  cs.verb,
  cs.subcommand,
  cs.flags,
  sc.name AS scope,
  pa.source,
  pa.promoted_at
FROM permission_active pa
JOIN command_shapes cs     ON cs.id = pa.command_shape_id
JOIN tool_call_scopes sc   ON sc.id = pa.scope_id
ORDER BY cs.verb, IFNULL(cs.subcommand, ''), cs.flags, sc.name;

CREATE VIEW v_rejected AS
SELECT
  cs.verb,
  cs.subcommand,
  cs.flags,
  sc.name AS scope,
  r.reason,
  r.rejected_at
FROM permission_rejected r
JOIN command_shapes cs     ON cs.id = r.command_shape_id
JOIN tool_call_scopes sc   ON sc.id = r.scope_id
ORDER BY cs.verb, IFNULL(cs.subcommand, ''), cs.flags, sc.name;

-- v_candidates: scope-agnostic check (only scope=any rules block a
-- candidate from proposal; scope-qualified rules don't).
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
LEFT JOIN permission_active   a
       ON a.command_shape_id = c.command_shape_id
      AND a.scope_id = (SELECT id FROM tool_call_scopes WHERE name = 'any')
LEFT JOIN permission_rejected r
       ON r.command_shape_id = c.command_shape_id
      AND r.scope_id = (SELECT id FROM tool_call_scopes WHERE name = 'any')
ORDER BY c.observations DESC, c.last_seen DESC;

-- v_active_usage: one row per (shape, scope) entry, with observation count.
CREATE VIEW v_active_usage AS
SELECT
  cs.verb,
  cs.subcommand,
  cs.flags,
  sc.name   AS scope,
  pa.source,
  COUNT(tcs.tool_call_id) AS observations
FROM permission_active pa
JOIN command_shapes cs     ON cs.id = pa.command_shape_id
JOIN tool_call_scopes sc   ON sc.id = pa.scope_id
LEFT JOIN tool_call_shapes tcs ON tcs.command_shape_id = cs.id
GROUP BY pa.command_shape_id, pa.scope_id
ORDER BY observations DESC, cs.verb, cs.flags;
