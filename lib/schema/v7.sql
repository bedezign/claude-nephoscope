-- Schema v7 — normalize repeating fields to int-FK lookups (Phase 3.6).
-- Applied in order by lib.db._migrate when PRAGMA user_version < 7.
--
-- Per plan "Phase 3.6 — v7+v8: normalize repeating fields" in
--   ~/.claude/.claude/plans/observability-module.md
--
-- Changes:
--   * New lookup tables: `tools`, `subagent_types`, `file_paths`.
--   * `sessions` flips from TEXT PK → INTEGER PK. The UUID moves to the new
--     `session_uuid TEXT UNIQUE NOT NULL` column. Uses the SQLite
--     create-copy-drop dance (atomically — see BEGIN/COMMIT below).
--   * `tool_calls` gains four FK columns: `tool_id`, `subagent_type_id`,
--     `file_path_id`, `session_id_new` (INTEGER → sessions.id; renamed to
--     `session_id` in v8 once the TEXT version is dropped).
--   * `permission_candidate_sessions` gains `session_id_new` pointing at the
--     new INTEGER sessions.id.
--   * All existing TEXT columns (`tool`, `subagent_type`, `file_path`,
--     `session_id`) stay in place — the recorder keeps writing them until
--     v8, at which point the DROP COLUMN runs. v7 is a pure add-and-backfill.
--
-- Atomicity:
--   The sessions PK refactor (create sessions_new → copy rows → DROP sessions
--   → RENAME sessions_new) would leave readers seeing a half-migrated state
--   mid-script on a hot DB. SQLite's `executescript` runs in autocommit
--   unless an explicit transaction brackets the work — hence the BEGIN /
--   COMMIT wrapping everything below. Foreign-key checks are deferred inside
--   the transaction so the DROP TABLE sessions step (while tool_calls still
--   references the old shape through its TEXT `session_id`) doesn't trip
--   the FK validator.

BEGIN;

-- --- lookup tables ----------------------------------------------------------

CREATE TABLE tools (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT    UNIQUE NOT NULL
);

CREATE TABLE subagent_types (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT    UNIQUE NOT NULL
);

CREATE TABLE file_paths (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  path       TEXT    UNIQUE NOT NULL,
  first_seen TEXT    NOT NULL,
  last_seen  TEXT    NOT NULL
);

-- --- sessions int-PK refactor (create-copy-drop dance) ----------------------
-- The existing `sessions.id` is a TEXT UUID. We want `id INTEGER PRIMARY KEY`
-- with the UUID living on `session_uuid TEXT UNIQUE NOT NULL`. Because
-- tool_calls.session_id (TEXT) and permission_candidate_sessions.session_id
-- (TEXT) both still point at the old string shape, we hold off creating
-- anything that FKs into the new sessions until the backfill has mapped
-- every legacy string session to its new integer id.
--
-- NB: we do NOT add a FK on sessions_new.project_id → projects(id) on the
-- new table at creation time. The original `sessions` had the FK and it's
-- preserved below on the renamed table — see the final CREATE INDEX block.

CREATE TABLE sessions_new (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  session_uuid    TEXT    UNIQUE NOT NULL,
  project_id      INTEGER REFERENCES projects(id),
  started_at      TEXT    NOT NULL,
  last_activity   TEXT    NOT NULL,
  transcript_path TEXT
);

INSERT INTO sessions_new (session_uuid, project_id, started_at, last_activity, transcript_path)
  SELECT id, project_id, started_at, last_activity, transcript_path
    FROM sessions
   ORDER BY started_at;  -- preserves monotonic-ish ordering of new integer ids

-- --- FK columns on tool_calls ----------------------------------------------
-- TEXT columns stay in place; v8 will DROP them after the recorder flips to
-- FK-only writes.

ALTER TABLE tool_calls ADD COLUMN tool_id INTEGER
  REFERENCES tools(id);
ALTER TABLE tool_calls ADD COLUMN subagent_type_id INTEGER
  REFERENCES subagent_types(id);
ALTER TABLE tool_calls ADD COLUMN file_path_id INTEGER
  REFERENCES file_paths(id);
ALTER TABLE tool_calls ADD COLUMN session_id_new INTEGER
  REFERENCES sessions_new(id);

-- --- FK column on permission_candidate_sessions -----------------------------

ALTER TABLE permission_candidate_sessions ADD COLUMN session_id_new INTEGER
  REFERENCES sessions_new(id);

-- --- backfill lookup tables from existing data ------------------------------

INSERT OR IGNORE INTO tools (name)
  SELECT DISTINCT tool FROM tool_calls WHERE tool IS NOT NULL;

INSERT OR IGNORE INTO subagent_types (name)
  SELECT DISTINCT subagent_type
    FROM tool_calls
   WHERE subagent_type IS NOT NULL;

INSERT OR IGNORE INTO file_paths (path, first_seen, last_seen)
  SELECT file_path, MIN(ts), MAX(ts)
    FROM tool_calls
   WHERE file_path IS NOT NULL
   GROUP BY file_path;

-- --- backfill FK columns on tool_calls --------------------------------------

UPDATE tool_calls
   SET tool_id = (SELECT id FROM tools WHERE name = tool_calls.tool)
 WHERE tool IS NOT NULL;

UPDATE tool_calls
   SET subagent_type_id = (
     SELECT id FROM subagent_types WHERE name = tool_calls.subagent_type
   )
 WHERE subagent_type IS NOT NULL;

UPDATE tool_calls
   SET file_path_id = (
     SELECT id FROM file_paths WHERE path = tool_calls.file_path
   )
 WHERE file_path IS NOT NULL;

UPDATE tool_calls
   SET session_id_new = (
     SELECT id FROM sessions_new WHERE session_uuid = tool_calls.session_id
   )
 WHERE session_id IS NOT NULL;

-- --- backfill FK column on permission_candidate_sessions --------------------

UPDATE permission_candidate_sessions
   SET session_id_new = (
     SELECT id FROM sessions_new
      WHERE session_uuid = permission_candidate_sessions.session_id
   )
 WHERE session_id IS NOT NULL;

-- --- swap old sessions for the new one --------------------------------------
-- By this point every row that referenced sessions by UUID has a matching
-- session_id_new populated, so dropping the old TEXT-PK sessions is safe.

DROP TABLE sessions;
ALTER TABLE sessions_new RENAME TO sessions;

-- --- indexes on the new FK columns ------------------------------------------

CREATE INDEX idx_tool_calls_tool_id
  ON tool_calls(tool_id);
CREATE INDEX idx_tool_calls_subagent_type_id
  ON tool_calls(subagent_type_id, ts)
  WHERE subagent_type_id IS NOT NULL;
CREATE INDEX idx_tool_calls_file_path_id
  ON tool_calls(file_path_id)
  WHERE file_path_id IS NOT NULL;
CREATE INDEX idx_tool_calls_session_id_new
  ON tool_calls(session_id_new);

CREATE INDEX idx_permission_candidate_sessions_session_id_new
  ON permission_candidate_sessions(session_id_new)
  WHERE session_id_new IS NOT NULL;

COMMIT;
