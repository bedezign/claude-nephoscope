-- Schema v3 — permission learner tables.
-- Applied in order by lib.db._migrate when PRAGMA user_version < 3.
--
-- permission_candidates: patterns observed in Bash calls that may be
--   promoted once thresholds (observations, distinct_sessions) are met.
-- permission_active:     patterns approved for auto-allow by the runtime
--   hook. source='learner' for automatic promotions, 'manual' for explicit
--   operator insertions.
--
-- flags is stored as a JSON array of sorted, deduped flag strings so the
-- primary key is stable regardless of how the pattern was first observed.

CREATE TABLE IF NOT EXISTS permission_candidates (
  verb TEXT NOT NULL,
  subcommand TEXT,
  flags TEXT NOT NULL,
  observations INTEGER NOT NULL DEFAULT 0,
  distinct_sessions INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  PRIMARY KEY (verb, subcommand, flags)
);

CREATE INDEX IF NOT EXISTS idx_permission_candidates_last_seen
  ON permission_candidates(last_seen);

CREATE TABLE IF NOT EXISTS permission_active (
  verb TEXT NOT NULL,
  subcommand TEXT,
  flags TEXT NOT NULL,
  promoted_at TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('learner', 'manual')),
  PRIMARY KEY (verb, subcommand, flags)
);
