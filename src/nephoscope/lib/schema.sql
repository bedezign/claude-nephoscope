-- Projects + sessions are unchanged in shape; reuse the current structure.
CREATE TABLE projects (
  id                        INTEGER PRIMARY KEY AUTOINCREMENT,
  cwd                       TEXT    UNIQUE NOT NULL,
  name                      TEXT,
  root                      TEXT,
  first_seen                TEXT    NOT NULL,
  last_seen                 TEXT    NOT NULL,
  settings_json_path        TEXT,
  settings_json_sha256      TEXT,
  settings_json_last_synced TEXT,
  settings_json_mtime       REAL,
  additional_dirs           TEXT
);

CREATE TABLE sessions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  session_uuid    TEXT    UNIQUE NOT NULL,
  project_id      INTEGER REFERENCES projects(id),
  started_at      TEXT    NOT NULL,
  last_activity   TEXT    NOT NULL,
  transcript_path TEXT,
  extra_dirs      TEXT    NOT NULL DEFAULT '[]'
);

-- Global settings mirror metadata — singleton table.
CREATE TABLE global_mirror (
  id                        INTEGER PRIMARY KEY CHECK (id = 1),
  settings_json_path        TEXT NOT NULL,
  settings_json_sha256      TEXT,
  settings_json_last_synced TEXT,
  settings_json_mtime       REAL,
  additional_dirs           TEXT
);

-- Lookup tables (seed rows inserted at setup time).
CREATE TABLE tools            (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
CREATE TABLE subagent_types   (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
CREATE TABLE file_paths       (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE NOT NULL,
                               first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE TABLE permission_modes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
CREATE TABLE call_statuses    (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);

-- Observations.
CREATE TABLE tool_calls (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                  TEXT    NOT NULL,
  completed_ts        TEXT,
  session_id          INTEGER REFERENCES sessions(id),
  project_id          INTEGER REFERENCES projects(id),
  tool_id             INTEGER REFERENCES tools(id),
  status_id           INTEGER REFERENCES call_statuses(id),
  permission_mode_id  INTEGER REFERENCES permission_modes(id),
  subagent_type_id    INTEGER REFERENCES subagent_types(id),
  file_path_id        INTEGER REFERENCES file_paths(id),
  tool_use_id         TEXT,
  ok                  INTEGER,
  command             TEXT,
  pattern             TEXT,
  description         TEXT,
  args_json           TEXT
);
CREATE INDEX idx_tool_calls_ts            ON tool_calls(ts);
CREATE INDEX idx_tool_calls_session       ON tool_calls(session_id);
CREATE INDEX idx_tool_calls_project_ts    ON tool_calls(project_id, ts);
CREATE INDEX idx_tool_calls_tool_ts       ON tool_calls(tool_id, ts);
CREATE INDEX idx_tool_calls_subagent      ON tool_calls(subagent_type_id, ts)
  WHERE subagent_type_id IS NOT NULL;
CREATE INDEX idx_tool_calls_status_ts     ON tool_calls(status_id, ts)
  WHERE status_id IS NOT NULL;
CREATE INDEX idx_tool_calls_tool_use_id   ON tool_calls(tool_use_id);

CREATE TABLE tool_extras (
  tool_call_id INTEGER NOT NULL REFERENCES tool_calls(id) ON DELETE CASCADE,
  name         TEXT    NOT NULL,
  value        TEXT    NOT NULL,
  PRIMARY KEY (tool_call_id, name)
);
CREATE INDEX idx_tool_extras_name ON tool_extras(name);

-- Rule shapes (can contain patterns; referenced by permissions only).
CREATE TABLE rule_shapes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  verb       TEXT    NOT NULL,        -- literal, or "$VAR/..." prefix
  subcommand TEXT,
  flags      TEXT    NOT NULL,        -- minified JSON array, OR literal "*"
  path_spec  TEXT,                    -- NULL=any, ""=no-paths, "$VAR/**"=glob
  context    TEXT    NOT NULL DEFAULT 'any' CHECK (context IN ('any', 'toplevel', 'substitution')),  -- rule constraint
  first_seen TEXT    NOT NULL,
  last_seen  TEXT    NOT NULL
);
CREATE UNIQUE INDEX idx_rule_shapes_unique
  ON rule_shapes(verb, IFNULL(subcommand, ''), flags, IFNULL(path_spec, ''), context);

-- Consolidated permission decisions.
CREATE TABLE permissions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  rule_shape_id INTEGER NOT NULL REFERENCES rule_shapes(id) ON DELETE CASCADE,
  session_id    INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
  project_id    INTEGER REFERENCES projects(id) ON DELETE CASCADE,
  decision      TEXT    NOT NULL CHECK (decision IN ('approved', 'rejected', 'ask')),
  source        TEXT    NOT NULL,    -- 'session-ask', 'review', 'learner', 'seed', 'manual', 'migrated'
  reason        TEXT,
  decided_at    TEXT    NOT NULL,
  CHECK (NOT (session_id IS NOT NULL AND project_id IS NOT NULL))
);
CREATE INDEX idx_permissions_lookup  ON permissions(rule_shape_id, session_id, project_id);
CREATE INDEX idx_permissions_session ON permissions(session_id) WHERE session_id IS NOT NULL;
CREATE INDEX idx_permissions_project ON permissions(project_id) WHERE project_id IS NOT NULL;

-- Transient hook→recorder correlation. Shape fields inlined.
CREATE TABLE permission_ask_pending (
  tool_use_id TEXT    PRIMARY KEY,
  session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  verb        TEXT    NOT NULL,
  subcommand  TEXT,
  flags       TEXT    NOT NULL,
  asked_at    TEXT    NOT NULL
);

-- Learner accumulation + observation ledger (shape fields inlined — no separate
-- observed_shapes registry).
CREATE TABLE permission_candidates (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  verb              TEXT    NOT NULL,
  subcommand        TEXT,
  flags             TEXT    NOT NULL,
  observations      INTEGER NOT NULL DEFAULT 0,
  distinct_sessions INTEGER NOT NULL DEFAULT 0,
  first_seen        TEXT    NOT NULL,
  last_seen         TEXT    NOT NULL
);
CREATE UNIQUE INDEX idx_permission_candidates_unique
  ON permission_candidates(verb, IFNULL(subcommand, ''), flags);
CREATE INDEX idx_permission_candidates_last_seen ON permission_candidates(last_seen);

CREATE TABLE permission_candidate_sessions (
  candidate_id INTEGER NOT NULL REFERENCES permission_candidates(id) ON DELETE CASCADE,
  session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  last_seen    TEXT    NOT NULL,
  PRIMARY KEY (candidate_id, session_id)
);
CREATE INDEX idx_permission_candidate_sessions_last_seen ON permission_candidate_sessions(last_seen);

CREATE TABLE consumer_cursors (
  consumer          TEXT    PRIMARY KEY,
  last_processed_id INTEGER NOT NULL DEFAULT 0,
  updated_at        TEXT    NOT NULL
);

-- Views (collapsed onto the new tables).
CREATE VIEW v_tool_calls AS
  SELECT tc.id, tc.ts, tc.completed_ts,
         t.name AS tool, cs.name AS status, pm.name AS permission_mode,
         st.name AS subagent_type, fp.path AS file_path,
         s.session_uuid, pr.cwd AS project_cwd, pr.name AS project_name,
         tc.command, tc.pattern, tc.description, tc.args_json,
         tc.tool_use_id, tc.ok
    FROM tool_calls tc
    LEFT JOIN tools            t  ON t.id  = tc.tool_id
    LEFT JOIN call_statuses    cs ON cs.id = tc.status_id
    LEFT JOIN permission_modes pm ON pm.id = tc.permission_mode_id
    LEFT JOIN subagent_types   st ON st.id = tc.subagent_type_id
    LEFT JOIN file_paths       fp ON fp.id = tc.file_path_id
    LEFT JOIN sessions         s  ON s.id  = tc.session_id
    LEFT JOIN projects         pr ON pr.id = tc.project_id;

CREATE VIEW v_recent_bash AS
  SELECT * FROM v_tool_calls WHERE tool = 'Bash' ORDER BY ts DESC;

CREATE VIEW v_rule_shapes AS SELECT * FROM rule_shapes;

CREATE VIEW v_permissions AS
  SELECT p.id, p.decision, p.source, p.reason, p.decided_at,
         rs.verb, rs.subcommand, rs.flags, rs.path_spec, rs.context,
         p.session_id, p.project_id,
         CASE WHEN p.session_id IS NOT NULL THEN 'session'
              WHEN p.project_id IS NOT NULL THEN 'project'
              ELSE 'global' END AS tier
    FROM permissions p
    JOIN rule_shapes rs ON rs.id = p.rule_shape_id;

CREATE VIEW v_candidates AS
  SELECT id, verb, subcommand, flags, observations, distinct_sessions, first_seen, last_seen
    FROM permission_candidates;

CREATE VIEW v_session_summary AS
  SELECT s.session_uuid, pr.cwd AS project_cwd,
         SUM(CASE WHEN cs.name='ok'      THEN 1 ELSE 0 END) AS ok_count,
         SUM(CASE WHEN cs.name='err'     THEN 1 ELSE 0 END) AS err_count,
         SUM(CASE WHEN cs.name='denied'  THEN 1 ELSE 0 END) AS denied_count,
         SUM(CASE WHEN cs.name='orphan'  THEN 1 ELSE 0 END) AS orphan_count,
         SUM(CASE WHEN cs.name='pending' THEN 1 ELSE 0 END) AS pending_count
    FROM sessions s
    LEFT JOIN tool_calls     tc ON tc.session_id = s.id
    LEFT JOIN call_statuses  cs ON cs.id         = tc.status_id
    LEFT JOIN projects       pr ON pr.id         = s.project_id
   GROUP BY s.id;

-- Seed lookup rows. The INSERT OR IGNORE block alone is safe to re-run; the
-- full schema file is not (CREATE TABLE statements above lack IF NOT EXISTS).
-- Production bootstrap only applies schema.sql against a missing DB file.
INSERT OR IGNORE INTO permission_modes (name) VALUES
  ('default'), ('acceptEdits'), ('bypassPermissions'), ('plan'), ('auto');
INSERT OR IGNORE INTO call_statuses (name) VALUES
  ('pending'), ('ok'), ('err'), ('denied'), ('orphan');
