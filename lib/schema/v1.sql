-- Schema v1 — baseline (extracted verbatim from continuous-learning-v2 observe.py).
-- Applied in order by lib.db._migrate when PRAGMA user_version < 1.

CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cwd TEXT UNIQUE NOT NULL,
  name TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  project_id INTEGER REFERENCES projects(id),
  started_at TEXT NOT NULL,
  last_activity TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  session_id TEXT,
  project_id INTEGER REFERENCES projects(id),
  tool TEXT NOT NULL,
  ok INTEGER,
  subagent_type TEXT,
  command TEXT,
  file_path TEXT,
  pattern TEXT,
  description TEXT,
  args_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_ts ON tool_calls(ts);
CREATE INDEX IF NOT EXISTS idx_tool_calls_proj_ts ON tool_calls(project_id, ts);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_ts ON tool_calls(tool, ts);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_subagent ON tool_calls(subagent_type, ts)
  WHERE subagent_type IS NOT NULL;

CREATE TABLE IF NOT EXISTS consumer_cursors (
  consumer TEXT PRIMARY KEY,
  last_processed_id INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
