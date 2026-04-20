-- Schema v8 — drop legacy TEXT columns now that recorder writes FK columns only.
-- Applied in order by lib.db._migrate when PRAGMA user_version < 8.
--
-- Per plan "Phase 3.6 — v7+v8: normalize repeating fields" in
--   ~/.claude/.claude/plans/observability-module.md
--
-- Two shapes of rewrite happen here:
--   1. On `tool_calls` the legacy TEXT columns (`tool`, `subagent_type`,
--      `file_path`, `session_id`) are plain nullable attributes — a
--      simple `ALTER TABLE … DROP COLUMN` suffices. The new INTEGER FK
--      `session_id_new` is then renamed back to `session_id`.
--   2. On `permission_candidate_sessions` the legacy TEXT `session_id`
--      is part of the composite PRIMARY KEY (`command_shape_id,
--      session_id`), so SQLite refuses `DROP COLUMN`. We rebuild the
--      table via the create-copy-drop dance, lifting the primary key
--      onto the INTEGER FK column.
--
-- Critical ordering prerequisite — before this migration runs the recorder
-- MUST be writing FK columns only. Any new INSERT that still targets the
-- TEXT columns will fail once they're dropped, so the switch order is:
--   1. Update recorder/run.py (FK-only writes).
--   2. Backfill any drift accumulated on TEXT-writing rows.
--   3. Apply v8.
--
-- Atomicity:
--   Wrap the whole migration in a transaction so readers never observe a
--   half-rebuilt permission_candidate_sessions. SQLite's executescript is
--   autocommit by default — the BEGIN/COMMIT is intentional.

BEGIN;

-- --- drop indexes that reference the TEXT columns --------------------------

DROP INDEX IF EXISTS idx_tool_calls_tool_ts;
DROP INDEX IF EXISTS idx_tool_calls_session;
DROP INDEX IF EXISTS idx_tool_calls_subagent;

-- --- tool_calls: plain DROP COLUMN on the four legacy TEXT attributes -----

ALTER TABLE tool_calls DROP COLUMN tool;
ALTER TABLE tool_calls DROP COLUMN subagent_type;
ALTER TABLE tool_calls DROP COLUMN file_path;
ALTER TABLE tool_calls DROP COLUMN session_id;
ALTER TABLE tool_calls RENAME COLUMN session_id_new TO session_id;

-- --- permission_candidate_sessions: rebuild to shed the TEXT PK column ----
-- The legacy TEXT session_id is half of the composite PRIMARY KEY, which
-- blocks ALTER TABLE DROP COLUMN. Copy surviving rows (those with
-- session_id_new populated — v7 backfilled them) into a new table keyed on
-- the INTEGER FK, then swap names. Related indexes are dropped first and
-- recreated on the canonical column afterwards.

DROP INDEX IF EXISTS idx_permission_candidate_sessions_last_seen;
DROP INDEX IF EXISTS idx_permission_candidate_sessions_session_id_new;

CREATE TABLE permission_candidate_sessions_v8 (
  command_shape_id INTEGER NOT NULL REFERENCES command_shapes(id),
  session_id       INTEGER NOT NULL REFERENCES sessions(id),
  last_seen        TEXT    NOT NULL,
  PRIMARY KEY (command_shape_id, session_id)
);

INSERT INTO permission_candidate_sessions_v8
  (command_shape_id, session_id, last_seen)
  SELECT command_shape_id, session_id_new, last_seen
    FROM permission_candidate_sessions
   WHERE session_id_new IS NOT NULL;

DROP TABLE permission_candidate_sessions;
ALTER TABLE permission_candidate_sessions_v8
  RENAME TO permission_candidate_sessions;

CREATE INDEX idx_permission_candidate_sessions_last_seen
  ON permission_candidate_sessions(last_seen);

-- --- recreate equivalent indexes on the FK columns ------------------------

CREATE INDEX idx_tool_calls_tool_id_ts ON tool_calls(tool_id, ts);
CREATE INDEX idx_tool_calls_session_id ON tool_calls(session_id);

COMMIT;
