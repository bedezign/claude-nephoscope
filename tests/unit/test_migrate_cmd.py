"""Tests for nephoscope.cli.migrate_cmd.

Covers:
  - _apply_schema_delta: idempotent column/index additions on old DBs
  - _normalize_rule_shapes: in-place flag normalization, collision merging
  - _normalize_candidates: in-place flag normalization, collision merging
  - _normalize_ask_pending: in-place flag normalization
  - _migrate: full run output and ROLLBACK on exception
  - main: non-existent path returns 1, valid DB returns 0

All tests use in-memory or tmp_path SQLite DBs. The production
observations.db is never opened.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nephoscope.cli.migrate_cmd import (
    _apply_schema_delta,
    _migrate,
    _normalize_ask_pending,
    _normalize_candidates,
    _normalize_rule_shapes,
    main,
)

_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent.parent / "src/nephoscope/lib/schema.sql"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_schema(conn: sqlite3.Connection) -> None:
    """Apply the full schema.sql to a connection."""
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)


def _make_current_db() -> sqlite3.Connection:
    """Return an in-memory DB at the current schema (both new columns present)."""
    conn = sqlite3.connect(":memory:")
    _load_schema(conn)
    return conn


def _make_old_db(missing: set[str] | None = None) -> sqlite3.Connection:
    """Return an in-memory DB with selected columns removed to simulate an old schema.

    Supported values in *missing*:
      'rule_shapes.tool'          — drops tool column and rebuilds old index
      'permissions.danger_accepted' — drops danger_accepted column
    """
    if missing is None:
        missing = set()

    conn = sqlite3.connect(":memory:")
    _load_schema(conn)

    if "rule_shapes.tool" in missing:
        # Rebuild rule_shapes without the 'tool' column.
        # Drop the views that reference rule_shapes first so the table rename
        # does not invalidate them; recreate views afterward.
        conn.executescript("""
            PRAGMA foreign_keys = OFF;
            DROP VIEW IF EXISTS v_rule_shapes;
            DROP VIEW IF EXISTS v_permissions;
            ALTER TABLE rule_shapes RENAME TO _rule_shapes_old;
            CREATE TABLE rule_shapes (
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              verb       TEXT    NOT NULL,
              subcommand TEXT,
              flags      TEXT    NOT NULL,
              path_spec  TEXT,
              context    TEXT    NOT NULL DEFAULT 'any',
              first_seen TEXT    NOT NULL,
              last_seen  TEXT    NOT NULL
            );
            INSERT INTO rule_shapes (id, verb, subcommand, flags, path_spec, context,
                                     first_seen, last_seen)
              SELECT id, verb, subcommand, flags, path_spec, context,
                     first_seen, last_seen
              FROM _rule_shapes_old;
            DROP TABLE _rule_shapes_old;
            DROP INDEX IF EXISTS idx_rule_shapes_unique;
            CREATE UNIQUE INDEX idx_rule_shapes_unique
              ON rule_shapes(verb, IFNULL(subcommand, ''), flags,
                             IFNULL(path_spec, ''), context);
            CREATE VIEW v_rule_shapes AS SELECT * FROM rule_shapes;
            CREATE VIEW v_permissions AS
              SELECT p.id, p.decision, p.source, p.reason, p.decided_at,
                     rs.verb, rs.subcommand, rs.flags, rs.path_spec, rs.context,
                     p.session_id, p.project_id,
                     CASE WHEN p.session_id IS NOT NULL THEN 'session'
                          WHEN p.project_id IS NOT NULL THEN 'project'
                          ELSE 'global' END AS tier,
                     p.hit_count, p.last_hit_at
                FROM permissions p
                JOIN rule_shapes rs ON rs.id = p.rule_shape_id;
            PRAGMA foreign_keys = ON;
        """)

    if "permissions.danger_accepted" in missing:
        conn.execute("ALTER TABLE permissions DROP COLUMN danger_accepted")

    return conn


# ---------------------------------------------------------------------------
# _apply_schema_delta
# ---------------------------------------------------------------------------


class TestApplySchemaDeltas:
    def test_current_db_returns_empty_list(self):
        """Already-current schema: both columns present, no-op, returns []."""
        conn = _make_current_db()
        result = _apply_schema_delta(conn)
        assert result == []

    def test_missing_tool_adds_column_and_index(self):
        """Old DB missing rule_shapes.tool: adds column + index, reports item."""
        conn = _make_old_db(missing={"rule_shapes.tool"})
        result = _apply_schema_delta(conn)
        assert result == ["rule_shapes.tool + index"]

        cols = {r[1] for r in conn.execute("PRAGMA table_info(rule_shapes)")}
        assert "tool" in cols

        indexes = {r[1] for r in conn.execute("PRAGMA index_list(rule_shapes)")}
        assert "idx_rule_shapes_unique" in indexes

    def test_missing_danger_accepted_adds_column(self):
        """Old DB missing permissions.danger_accepted: adds column, reports item."""
        conn = _make_old_db(missing={"permissions.danger_accepted"})
        result = _apply_schema_delta(conn)
        assert result == ["permissions.danger_accepted"]

        cols = {r[1] for r in conn.execute("PRAGMA table_info(permissions)")}
        assert "danger_accepted" in cols

    def test_missing_both_reports_both_items(self):
        """DB missing both columns: returns both items in the reported list."""
        conn = _make_old_db(missing={"rule_shapes.tool", "permissions.danger_accepted"})
        result = _apply_schema_delta(conn)
        assert set(result) == {
            "rule_shapes.tool + index",
            "permissions.danger_accepted",
        }

    def test_idempotent_second_call_is_noop(self):
        """Calling _apply_schema_delta twice leaves the schema unchanged."""
        conn = _make_old_db(missing={"rule_shapes.tool", "permissions.danger_accepted"})
        _apply_schema_delta(conn)
        result2 = _apply_schema_delta(conn)
        assert result2 == []


# ---------------------------------------------------------------------------
# _normalize_rule_shapes
# ---------------------------------------------------------------------------


def _insert_rule_shape(
    conn: sqlite3.Connection,
    *,
    verb: str,
    flags: str,
    subcommand: str | None = None,
    path_spec: str | None = None,
    context: str = "any",
    tool: str = "Bash",
    first_seen: str = "2024-01-01T00:00:00Z",
    last_seen: str = "2024-01-02T00:00:00Z",
) -> int:
    cur = conn.execute(
        "INSERT INTO rule_shapes(verb, subcommand, flags, path_spec, context, tool,"
        "  first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (verb, subcommand, flags, path_spec, context, tool, first_seen, last_seen),
    )
    return int(cur.lastrowid or 0)


class TestNormalizeRuleShapes:
    def test_empty_table_returns_zero_zero(self):
        """Empty rule_shapes: no rows to process, returns (0, 0)."""
        conn = _make_current_db()
        result = _normalize_rule_shapes(conn)
        assert result == (0, 0)

    def test_single_row_no_collision(self):
        """Single row: normalized in-place, no merge needed, returns (1, 0)."""
        conn = _make_current_db()
        _insert_rule_shape(conn, verb="ls", flags="[]")
        total, merged = _normalize_rule_shapes(conn)
        assert total == 1
        assert merged == 0

    def test_no_collision_three_distinct_rows(self):
        """Three rows with distinct normalized keys: all preserved, returns (3, 0)."""
        conn = _make_current_db()
        _insert_rule_shape(conn, verb="ls", flags='["-l"]')
        _insert_rule_shape(conn, verb="ls", flags='["-a"]')
        _insert_rule_shape(conn, verb="git", flags="[]", subcommand="status")
        total, merged = _normalize_rule_shapes(conn)
        assert total == 3
        assert merged == 0
        remaining = conn.execute("SELECT COUNT(*) FROM rule_shapes").fetchone()[0]
        assert remaining == 3

    def test_collision_winner_keeps_lower_id(self):
        """Two rows that normalize to same key: lower id wins, loser is deleted."""
        conn = _make_current_db()
        winner_id = _insert_rule_shape(
            conn,
            verb="rm",
            flags='["-rf"]',
            first_seen="2024-01-01T00:00:00Z",
            last_seen="2024-01-05T00:00:00Z",
        )
        loser_id = _insert_rule_shape(
            conn,
            verb="rm",
            flags='["-f","-r"]',
            first_seen="2024-01-03T00:00:00Z",
            last_seen="2024-01-10T00:00:00Z",
        )

        total, merged = _normalize_rule_shapes(conn)
        assert total == 2
        assert merged == 1

        remaining_ids = [r[0] for r in conn.execute("SELECT id FROM rule_shapes")]
        assert winner_id in remaining_ids
        assert loser_id not in remaining_ids

    def test_collision_dates_merged_correctly(self):
        """Collision: winner gets MIN(first_seen) and MAX(last_seen) from both rows."""
        conn = _make_current_db()
        _insert_rule_shape(
            conn,
            verb="rm",
            flags='["-rf"]',
            first_seen="2024-01-01T00:00:00Z",
            last_seen="2024-01-05T00:00:00Z",
        )
        _insert_rule_shape(
            conn,
            verb="rm",
            flags='["-f","-r"]',
            first_seen="2024-01-03T00:00:00Z",
            last_seen="2024-01-10T00:00:00Z",
        )

        _normalize_rule_shapes(conn)
        row = conn.execute(
            'SELECT first_seen, last_seen FROM rule_shapes WHERE verb = "rm"'
        ).fetchone()
        assert row[0] == "2024-01-01T00:00:00Z"
        assert row[1] == "2024-01-10T00:00:00Z"

    def test_collision_permissions_fk_remapped(self):
        """Loser FK in permissions is remapped to winner before loser is deleted."""
        conn = _make_current_db()
        winner_id = _insert_rule_shape(
            conn,
            verb="rm",
            flags='["-rf"]',
            first_seen="2024-01-01T00:00:00Z",
            last_seen="2024-01-02T00:00:00Z",
        )
        loser_id = _insert_rule_shape(
            conn,
            verb="rm",
            flags='["-f","-r"]',
            first_seen="2024-01-03T00:00:00Z",
            last_seen="2024-01-04T00:00:00Z",
        )

        now = "2024-01-05T00:00:00Z"
        conn.execute(
            "INSERT INTO permissions(rule_shape_id, decision, source, decided_at)"
            ' VALUES (?, "rejected", "learner", ?)',
            (loser_id, now),
        )

        _normalize_rule_shapes(conn)

        perm_shape_ids = [
            r[0] for r in conn.execute("SELECT rule_shape_id FROM permissions")
        ]
        assert all(s == winner_id for s in perm_shape_ids)


# ---------------------------------------------------------------------------
# _normalize_candidates
# ---------------------------------------------------------------------------


def _insert_candidate(
    conn: sqlite3.Connection,
    *,
    verb: str,
    flags: str,
    subcommand: str | None = None,
    observations: int = 1,
    distinct_sessions: int = 1,
    first_seen: str = "2024-01-01T00:00:00Z",
    last_seen: str = "2024-01-02T00:00:00Z",
    positional_paths: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO permission_candidates"
        "(verb, subcommand, flags, observations, distinct_sessions,"
        " first_seen, last_seen, positional_paths)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            verb,
            subcommand,
            flags,
            observations,
            distinct_sessions,
            first_seen,
            last_seen,
            positional_paths,
        ),
    )
    return int(cur.lastrowid or 0)


def _insert_session(conn: sqlite3.Connection) -> int:
    """Insert a minimal sessions row for FK integrity and return its id."""
    cur = conn.execute(
        "INSERT INTO sessions(session_uuid, started_at, last_activity)"
        " VALUES (?, '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')",
        ("test-session-uuid",),
    )
    return int(cur.lastrowid or 0)


class TestNormalizeCandidates:
    def test_empty_table_returns_zero_zero(self):
        """Empty permission_candidates: returns (0, 0)."""
        conn = _make_current_db()
        result = _normalize_candidates(conn)
        assert result == (0, 0)

    def test_single_row_no_collision(self):
        """Single row: normalized in-place, returns (1, 0)."""
        conn = _make_current_db()
        _insert_candidate(conn, verb="ls", flags="[]")
        total, merged = _normalize_candidates(conn)
        assert total == 1
        assert merged == 0

    def test_no_collision_two_distinct_rows(self):
        """Two candidates with distinct normalized keys: both preserved, returns (2, 0)."""
        conn = _make_current_db()
        _insert_candidate(conn, verb="ls", flags='["-l"]', observations=3)
        _insert_candidate(conn, verb="ls", flags='["-a"]', observations=5)
        total, merged = _normalize_candidates(conn)
        assert total == 2
        assert merged == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM permission_candidates").fetchone()[0]
            == 2
        )

    def test_collision_winner_keeps_lower_id(self):
        """Two rows normalizing to same key: lower id wins, loser deleted."""
        conn = _make_current_db()
        winner_id = _insert_candidate(conn, verb="rm", flags='["-rf"]', observations=2)
        loser_id = _insert_candidate(
            conn, verb="rm", flags='["-f","-r"]', observations=3
        )

        total, merged = _normalize_candidates(conn)
        assert total == 2
        assert merged == 1

        remaining = [r[0] for r in conn.execute("SELECT id FROM permission_candidates")]
        assert winner_id in remaining
        assert loser_id not in remaining

    def test_collision_observations_summed(self):
        """Collision: winner's observations = sum of both rows."""
        conn = _make_current_db()
        _insert_candidate(conn, verb="rm", flags='["-rf"]', observations=2)
        _insert_candidate(conn, verb="rm", flags='["-f","-r"]', observations=3)

        _normalize_candidates(conn)
        row = conn.execute(
            'SELECT observations FROM permission_candidates WHERE verb = "rm"'
        ).fetchone()
        assert row[0] == 5

    def test_collision_distinct_sessions_max(self):
        """Collision: winner's distinct_sessions = MAX of both rows."""
        conn = _make_current_db()
        _insert_candidate(conn, verb="rm", flags='["-rf"]', distinct_sessions=2)
        _insert_candidate(conn, verb="rm", flags='["-f","-r"]', distinct_sessions=5)

        _normalize_candidates(conn)
        row = conn.execute(
            'SELECT distinct_sessions FROM permission_candidates WHERE verb = "rm"'
        ).fetchone()
        assert row[0] == 5

    def test_collision_dates_merged_correctly(self):
        """Collision: winner gets MIN(first_seen) and MAX(last_seen)."""
        conn = _make_current_db()
        _insert_candidate(
            conn,
            verb="rm",
            flags='["-rf"]',
            first_seen="2024-01-01T00:00:00Z",
            last_seen="2024-01-05T00:00:00Z",
        )
        _insert_candidate(
            conn,
            verb="rm",
            flags='["-f","-r"]',
            first_seen="2024-01-03T00:00:00Z",
            last_seen="2024-01-10T00:00:00Z",
        )

        _normalize_candidates(conn)
        row = conn.execute(
            'SELECT first_seen, last_seen FROM permission_candidates WHERE verb = "rm"'
        ).fetchone()
        assert row[0] == "2024-01-01T00:00:00Z"
        assert row[1] == "2024-01-10T00:00:00Z"

    def test_collision_positional_paths_first_non_null(self):
        """Collision: positional_paths = first non-null value (row with lower id)."""
        conn = _make_current_db()
        _insert_candidate(
            conn,
            verb="rm",
            flags='["-rf"]',
            positional_paths='["/home/user/file.txt"]',
        )
        _insert_candidate(
            conn,
            verb="rm",
            flags='["-f","-r"]',
            positional_paths='["/tmp/other.txt"]',
        )

        _normalize_candidates(conn)
        row = conn.execute(
            'SELECT positional_paths FROM permission_candidates WHERE verb = "rm"'
        ).fetchone()
        assert row[0] == '["/home/user/file.txt"]'

    def test_collision_loser_candidate_sessions_deleted(self):
        """Collision: loser's permission_candidate_sessions rows are deleted."""
        conn = _make_current_db()
        session_id = _insert_session(conn)
        _insert_candidate(conn, verb="rm", flags='["-rf"]')
        loser_id = _insert_candidate(conn, verb="rm", flags='["-f","-r"]')

        conn.execute(
            "INSERT INTO permission_candidate_sessions(candidate_id, session_id, last_seen)"
            " VALUES (?, ?, '2024-01-01T00:00:00Z')",
            (loser_id, session_id),
        )

        _normalize_candidates(conn)

        orphan_count = conn.execute(
            "SELECT COUNT(*) FROM permission_candidate_sessions WHERE candidate_id = ?",
            (loser_id,),
        ).fetchone()[0]
        assert orphan_count == 0


# ---------------------------------------------------------------------------
# _normalize_ask_pending
# ---------------------------------------------------------------------------


def _insert_session_for_ask_pending(conn: sqlite3.Connection) -> int:
    """Insert a sessions row for the ask_pending FK and return its id."""
    cur = conn.execute(
        "INSERT INTO sessions(session_uuid, started_at, last_activity)"
        " VALUES ('ask-session', '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')"
    )
    return int(cur.lastrowid or 0)


class TestNormalizeAskPending:
    def test_empty_table_returns_zero(self):
        """Empty permission_ask_pending: returns 0."""
        conn = _make_current_db()
        result = _normalize_ask_pending(conn)
        assert result == 0

    def test_already_normalized_flags_returns_zero(self):
        """Flags already in normalized form: no updates needed, returns 0."""
        conn = _make_current_db()
        session_id = _insert_session_for_ask_pending(conn)
        conn.execute(
            "INSERT INTO permission_ask_pending"
            "(tool_use_id, session_id, verb, subcommand, flags, asked_at)"
            " VALUES ('uid1', ?, 'ls', NULL, '[\"-l\"]', '2024-01-01T00:00:00Z')",
            (session_id,),
        )
        result = _normalize_ask_pending(conn)
        assert result == 0

    def test_unnormalized_flags_updated_in_place(self):
        """Flags stored as cluster (e.g. ["-rf"]) are expanded and updated."""
        conn = _make_current_db()
        session_id = _insert_session_for_ask_pending(conn)
        conn.execute(
            "INSERT INTO permission_ask_pending"
            "(tool_use_id, session_id, verb, subcommand, flags, asked_at)"
            " VALUES ('uid1', ?, 'rm', NULL, '[\"-rf\"]', '2024-01-01T00:00:00Z')",
            (session_id,),
        )
        result = _normalize_ask_pending(conn)
        assert result == 1

        row = conn.execute(
            "SELECT flags FROM permission_ask_pending WHERE tool_use_id = ?",
            ("uid1",),
        ).fetchone()
        normalized = json.loads(row[0])
        assert sorted(normalized) == ["-f", "-r"]


# ---------------------------------------------------------------------------
# _migrate
# ---------------------------------------------------------------------------


class TestMigrate:
    def test_successful_run_output_contains_done(self, tmp_path: Path, capsys):
        """Full _migrate run: all four sections printed, output contains 'Done.'."""
        db_path = tmp_path / "obs.db"
        conn = sqlite3.connect(str(db_path))
        _load_schema(conn)
        conn.close()

        rc = _migrate(db_path)

        assert rc == 0
        captured = capsys.readouterr()
        assert "Done." in captured.out
        assert "rule_shapes" in captured.out
        assert "permission_candidates" in captured.out
        assert "permission_ask_pending" in captured.out

    def test_exception_triggers_rollback(self, tmp_path: Path, monkeypatch):
        """When _normalize_rule_shapes raises, the transaction is rolled back.

        The schema delta runs before _normalize_rule_shapes inside the same
        transaction. If that transaction is rolled back, columns added by
        _apply_schema_delta must not appear on the post-exception DB state.
        We use an old DB (missing rule_shapes.tool) so the delta actually runs,
        then inject a failure to verify rollback undoes it.
        """
        db_path = tmp_path / "obs_rollback.db"
        conn = sqlite3.connect(str(db_path))
        _load_schema(conn)
        # Rebuild without 'tool' column to make the delta meaningful.
        conn.executescript("""
            PRAGMA foreign_keys = OFF;
            DROP VIEW IF EXISTS v_rule_shapes;
            DROP VIEW IF EXISTS v_permissions;
            ALTER TABLE rule_shapes RENAME TO _rs_old;
            CREATE TABLE rule_shapes (
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              verb       TEXT    NOT NULL,
              subcommand TEXT,
              flags      TEXT    NOT NULL,
              path_spec  TEXT,
              context    TEXT    NOT NULL DEFAULT 'any',
              first_seen TEXT    NOT NULL,
              last_seen  TEXT    NOT NULL
            );
            INSERT INTO rule_shapes (id, verb, subcommand, flags, path_spec, context,
                                     first_seen, last_seen)
              SELECT id, verb, subcommand, flags, path_spec, context,
                     first_seen, last_seen
              FROM _rs_old;
            DROP TABLE _rs_old;
            DROP INDEX IF EXISTS idx_rule_shapes_unique;
            CREATE UNIQUE INDEX idx_rule_shapes_unique
              ON rule_shapes(verb, IFNULL(subcommand, ''), flags,
                             IFNULL(path_spec, ''), context);
            PRAGMA foreign_keys = ON;
        """)
        conn.close()

        def _raise(_: sqlite3.Connection):
            raise RuntimeError("simulated normalization failure")

        monkeypatch.setattr("nephoscope.cli.migrate_cmd._normalize_rule_shapes", _raise)

        with pytest.raises(RuntimeError, match="simulated normalization failure"):
            _migrate(db_path)

        post_conn = sqlite3.connect(str(db_path))
        cols = {r[1] for r in post_conn.execute("PRAGMA table_info(rule_shapes)")}
        post_conn.close()
        assert "tool" not in cols


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestMain:
    def test_non_existent_db_path_returns_1(self, tmp_path: Path):
        """--db pointing to a missing file: returns 1."""
        missing = tmp_path / "nonexistent.db"
        rc = main(["--db", str(missing)])
        assert rc == 1

    def test_valid_db_returns_0(self, tmp_path: Path):
        """--db pointing to a valid initialized DB: returns 0."""
        db_path = tmp_path / "valid.db"
        conn = sqlite3.connect(str(db_path))
        _load_schema(conn)
        conn.close()

        rc = main(["--db", str(db_path)])
        assert rc == 0
