-- Schema v5 — heavy-capture + normalization expansion (Phase 3.5).
-- Applied in order by lib.db._migrate when PRAGMA user_version < 5.
--
-- Per plan "Phase 3.5 — v5 schema expansion" in
--   ~/.claude/.claude/plans/observability-module.md
--
-- Changes:
--   * Sidecar `tool_extras` for heavy text (payload/response) — DELETEable
--     without losing audit trail.
--   * Lookup tables `permission_modes` and `call_statuses` — int-FK enums per
--     global rules/database-design.md. New `permission_mode_id` and
--     `status_id` columns on `tool_calls`; existing TEXT `status` preserved
--     for transitional readers.
--   * `sessions.transcript_path` — one path per session, not per row.
--   * `command_shapes` — base canonical-shape registry. Lazy-populated by the
--     learner. `permission_*` tables FK into it via `command_shape_id`.
--   * `tool_call_shapes` M2M — one Bash call can map to multiple leaves.
--   * The `permission_candidates` / `permission_active` /
--     `permission_candidate_sessions` tables are re-shaped via the SQLite
--     "create-copy-drop" dance (SQLite < 3.35 can't DROP COLUMN). Existing
--     rows are backfilled by inserting into `command_shapes` first (unique
--     shapes from the legacy tables) and joining on the shape tuple.

-- --- sidecar for heavy text blobs -------------------------------------------

CREATE TABLE tool_extras (
  tool_call_id INTEGER NOT NULL REFERENCES tool_calls(id) ON DELETE CASCADE,
  name         TEXT    NOT NULL,
  value        TEXT    NOT NULL,
  PRIMARY KEY (tool_call_id, name)
);
CREATE INDEX idx_tool_extras_name ON tool_extras(name);

-- --- permission_mode lookup (int-FK enum) -----------------------------------

CREATE TABLE permission_modes (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT    UNIQUE NOT NULL
);
INSERT INTO permission_modes (name) VALUES
  ('default'), ('acceptEdits'), ('bypassPermissions'), ('plan'), ('auto');

ALTER TABLE tool_calls ADD COLUMN permission_mode_id INTEGER
  REFERENCES permission_modes(id);
CREATE INDEX idx_tool_calls_permission_mode
  ON tool_calls(permission_mode_id);

-- --- status lookup (int-FK enum) --------------------------------------------
-- Existing TEXT status column is preserved for backward compat during the
-- recorder transition; new code reads/writes status_id only. Backfill maps
-- any non-null legacy status into the lookup id.

CREATE TABLE call_statuses (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT    UNIQUE NOT NULL
);
INSERT INTO call_statuses (name) VALUES
  ('pending'), ('ok'), ('err'), ('denied'), ('orphan');

ALTER TABLE tool_calls ADD COLUMN status_id INTEGER
  REFERENCES call_statuses(id);
UPDATE tool_calls
   SET status_id = (SELECT id FROM call_statuses WHERE name = tool_calls.status)
 WHERE status IS NOT NULL;
CREATE INDEX idx_tool_calls_status_id ON tool_calls(status_id);

-- --- transcript_path on sessions --------------------------------------------

ALTER TABLE sessions ADD COLUMN transcript_path TEXT;

-- --- command_shapes base registry -------------------------------------------

CREATE TABLE command_shapes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  verb       TEXT    NOT NULL,
  subcommand TEXT,
  flags      TEXT    NOT NULL,  -- minified JSON array, sorted, deduped
  first_seen TEXT    NOT NULL,
  last_seen  TEXT    NOT NULL
);
CREATE UNIQUE INDEX idx_command_shapes_unique
  ON command_shapes(verb, IFNULL(subcommand, ''), flags);
CREATE INDEX idx_command_shapes_last_seen
  ON command_shapes(last_seen);

-- Seed command_shapes from distinct shapes already present in the legacy
-- permission tables (union of candidates + candidate_sessions — active is
-- empty at migration time but included for safety). Normalise empty-string
-- subcommand back to NULL on entry; the partial UNIQUE index uses
-- IFNULL(subcommand,''), so this stays consistent either way. first_seen /
-- last_seen are pulled from whatever the source row had; if the same shape
-- appears in both sources, we keep the earliest first_seen and latest
-- last_seen via GROUP BY + MIN/MAX.
INSERT INTO command_shapes (verb, subcommand, flags, first_seen, last_seen)
SELECT verb,
       CASE WHEN subcommand = '' THEN NULL ELSE subcommand END AS subcommand,
       flags,
       MIN(first_seen) AS first_seen,
       MAX(last_seen)  AS last_seen
  FROM (
    SELECT verb,
           COALESCE(subcommand, '') AS subcommand,
           flags,
           first_seen,
           last_seen
      FROM permission_candidates
    UNION ALL
    SELECT verb,
           COALESCE(subcommand, '') AS subcommand,
           flags,
           last_seen AS first_seen,  -- sessions has no first_seen; re-use
           last_seen
      FROM permission_candidate_sessions
  )
 GROUP BY verb, subcommand, flags;

-- --- tool_call ↔ command_shape M2M junction ---------------------------------

CREATE TABLE tool_call_shapes (
  tool_call_id     INTEGER NOT NULL REFERENCES tool_calls(id) ON DELETE CASCADE,
  command_shape_id INTEGER NOT NULL REFERENCES command_shapes(id),
  leaf_index       INTEGER NOT NULL,
  PRIMARY KEY (tool_call_id, leaf_index)
);
CREATE INDEX idx_tool_call_shapes_shape
  ON tool_call_shapes(command_shape_id);

-- --- permission_candidates refactor (create-copy-drop dance) ----------------

CREATE TABLE permission_candidates_new (
  command_shape_id  INTEGER PRIMARY KEY REFERENCES command_shapes(id),
  observations      INTEGER NOT NULL DEFAULT 0,
  distinct_sessions INTEGER NOT NULL DEFAULT 0,
  first_seen        TEXT    NOT NULL,
  last_seen         TEXT    NOT NULL
);

-- Backfill via join on the shape tuple. Legacy rows may have NULL subcommand;
-- command_shapes does too, so we match on IFNULL to treat both consistently.
INSERT INTO permission_candidates_new (
  command_shape_id, observations, distinct_sessions, first_seen, last_seen
)
SELECT cs.id,
       pc.observations,
       pc.distinct_sessions,
       pc.first_seen,
       pc.last_seen
  FROM permission_candidates pc
  JOIN command_shapes cs
    ON cs.verb = pc.verb
   AND IFNULL(cs.subcommand, '') = IFNULL(pc.subcommand, '')
   AND cs.flags = pc.flags;

DROP TABLE permission_candidates;
ALTER TABLE permission_candidates_new RENAME TO permission_candidates;
CREATE INDEX idx_permission_candidates_last_seen
  ON permission_candidates(last_seen);

-- --- permission_active refactor ---------------------------------------------

CREATE TABLE permission_active_new (
  command_shape_id INTEGER PRIMARY KEY REFERENCES command_shapes(id),
  promoted_at      TEXT    NOT NULL,
  source           TEXT    NOT NULL CHECK (source IN ('learner', 'manual'))
);

-- Backfill (active is expected to be empty, but the join is harmless).
INSERT INTO permission_active_new (command_shape_id, promoted_at, source)
SELECT cs.id, pa.promoted_at, pa.source
  FROM permission_active pa
  JOIN command_shapes cs
    ON cs.verb = pa.verb
   AND IFNULL(cs.subcommand, '') = IFNULL(pa.subcommand, '')
   AND cs.flags = pa.flags;

DROP TABLE permission_active;
ALTER TABLE permission_active_new RENAME TO permission_active;

-- --- permission_candidate_sessions refactor ---------------------------------

CREATE TABLE permission_candidate_sessions_new (
  command_shape_id INTEGER NOT NULL REFERENCES command_shapes(id),
  session_id       TEXT    NOT NULL,
  last_seen        TEXT    NOT NULL,
  PRIMARY KEY (command_shape_id, session_id)
);

-- Backfill. Legacy subcommand was NOT NULL with '' placeholder per v4 (per
-- file header; actual DB has it as plain NULLable TEXT — either way,
-- IFNULL(...,'') normalises). Deduplicate on (shape, session_id) in case the
-- legacy data has any drift (shouldn't, primary key protected it).
INSERT INTO permission_candidate_sessions_new (
  command_shape_id, session_id, last_seen
)
SELECT cs.id, pcs.session_id, MAX(pcs.last_seen)
  FROM permission_candidate_sessions pcs
  JOIN command_shapes cs
    ON cs.verb = pcs.verb
   AND IFNULL(cs.subcommand, '') = IFNULL(pcs.subcommand, '')
   AND cs.flags = pcs.flags
 GROUP BY cs.id, pcs.session_id;

DROP TABLE permission_candidate_sessions;
ALTER TABLE permission_candidate_sessions_new
  RENAME TO permission_candidate_sessions;
CREATE INDEX idx_permission_candidate_sessions_last_seen
  ON permission_candidate_sessions(last_seen);
