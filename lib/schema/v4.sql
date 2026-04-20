-- Schema v4 — junction table for permission-candidate session attribution.
-- Applied in order by lib.db._migrate when PRAGMA user_version < 4.
--
-- Why: Phase 3's learner computed `distinct_sessions` with a LIKE '%verb%'
-- heuristic against tool_calls.command, which badly overcounts common verbs
-- (ls, echo, cat) — any row whose text happens to contain the verb inflates
-- the count, regardless of whether canonicalization would have produced the
-- same leaf. Since distinct_sessions is the threshold gate for promotion,
-- the wrong count leads to wrong promotions.
--
-- Fix: keep exact session attribution per canonical leaf in a junction
-- table. Each (verb, subcommand, flags, session_id) row represents "this
-- canonical shape was observed in this session at least once". last_seen
-- gets bumped on re-observation via INSERT ... ON CONFLICT DO UPDATE.
--
-- NULL subcommand handling: permission_candidates.subcommand is TEXT NULL
-- (see v3.sql) and the learner compares with `subcommand IS ?`. But SQLite
-- treats NULLs in a primary key as distinct, which breaks ON CONFLICT
-- DO UPDATE — repeated observations of a (verb, NULL, flags, session_id)
-- tuple would insert duplicate rows instead of bumping last_seen. To keep
-- upserts sound, this junction stores subcommand as NOT NULL TEXT with
-- empty string `''` substituting for NULL. The learner converts at the
-- boundary; the convention is isolated to this table.
--
-- flags is the same JSON-array convention as permission_candidates.flags.

CREATE TABLE IF NOT EXISTS permission_candidate_sessions (
  verb TEXT NOT NULL,
  subcommand TEXT NOT NULL DEFAULT '',
  flags TEXT NOT NULL,
  session_id TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  PRIMARY KEY (verb, subcommand, flags, session_id)
);

CREATE INDEX IF NOT EXISTS idx_permission_candidate_sessions_last_seen
  ON permission_candidate_sessions(last_seen);
