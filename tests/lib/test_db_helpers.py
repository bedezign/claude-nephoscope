"""Tests for lib/db.py helpers."""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from nephoscope.lib import db
from nephoscope.lib.paths import canonicalize
from nephoscope.recorder import run as recorder


@pytest.fixture
def two_connections(tmp_path, monkeypatch):
    """Open two independent connections to a fresh DB in tmp_path.

    Yields (conn_a, conn_b). Closes both on teardown.
    Intended for tests that exercise concurrent-writer / ON CONFLICT paths.
    """
    db_file = tmp_path / "race.db"
    monkeypatch.setenv("OBSERVABILITY_DB", str(db_file))
    conn_a = db._open()
    conn_b = db._open()
    try:
        yield conn_a, conn_b
    finally:
        conn_a.close()
        conn_b.close()


@pytest.fixture
def temp_db():
    """Create a temporary database with the current schema."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_path = f.name

    with mock.patch.dict("os.environ", {"OBSERVABILITY_DB": temp_path}):
        conn = db._open()
        conn.executescript(
            """
            INSERT OR IGNORE INTO permission_modes (name) VALUES
              ('default'),('acceptEdits'),('bypassPermissions'),('plan'),('auto');
            INSERT OR IGNORE INTO call_statuses (name) VALUES
              ('pending'),('ok'),('err'),('denied'),('orphan');
            """
        )
        try:
            yield conn
        finally:
            conn.close()
            Path(temp_path).unlink(missing_ok=True)


class TestNow:
    """Tests for _now() timestamp generation."""

    def test_now_returns_iso8601_with_z_suffix(self):
        """Verify _now() returns ISO-8601 with Z suffix."""
        ts = db._now()
        assert ts.endswith("Z")
        assert "T" in ts
        # Should be parseable as ISO-8601 if we strip the Z
        assert dt.datetime.fromisoformat(ts[:-1] + "+00:00") is not None
        # Basic format check: YYYY-MM-DDTHH:MM:SS.mmmZ
        assert len(ts) > 20


class TestTruncate:
    """Tests for _truncate() string capping."""

    def test_short_string_unchanged(self):
        """Short strings pass through unmodified."""
        assert db._truncate("hello") == "hello"

    def test_long_string_capped(self):
        """Strings longer than MAX_STR get ellipsis."""
        long_str = "x" * (db.MAX_STR + 10)
        result = db._truncate(long_str)
        assert len(result) == db.MAX_STR + 1  # +1 for ellipsis
        assert result.endswith("…")

    def test_non_string_unchanged(self):
        """Non-string values pass through unchanged."""
        assert db._truncate(42) == 42
        assert db._truncate(None) is None


class TestOpen:
    """Tests for _open() database initialization."""

    def test_open_creates_tables(self, temp_db):
        """_open() creates all tables from schema.sql."""
        # Check for presence of key tables
        cursor = temp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
        )
        tables = {row[0] for row in cursor.fetchall()}

        required_tables = {
            "projects",
            "sessions",
            "tools",
            "subagent_types",
            "file_paths",
            "permission_modes",
            "call_statuses",
            "tool_calls",
            "tool_extras",
            "rule_shapes",
            "permissions",
            "permission_ask_pending",
            "permission_candidates",
            "permission_candidate_sessions",
            "consumer_cursors",
        }
        assert required_tables.issubset(tables)

    def test_open_creates_views(self, temp_db):
        """_open() creates all views from schema.sql."""
        cursor = temp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name;"
        )
        views = {row[0] for row in cursor.fetchall()}

        required_views = {
            "v_tool_calls",
            "v_recent_bash",
            "v_rule_shapes",
            "v_permissions",
            "v_candidates",
            "v_session_summary",
        }
        assert required_views.issubset(views)

    def test_wal_mode_enabled(self, temp_db):
        """Connection uses WAL mode."""
        mode = temp_db.execute("PRAGMA journal_mode;").fetchone()[0]
        assert mode.lower() == "wal"


class TestUpsertProject:
    """Tests for upsert_project()."""

    def test_insert_new_project(self, temp_db):
        """First call inserts a new project row."""
        now = db._now()
        proj_id = db.upsert_project(temp_db, "/work/myproject", now)

        assert proj_id > 0
        row = temp_db.execute(
            "SELECT cwd, name, first_seen, last_seen FROM projects WHERE id = ?;",
            (proj_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == "/work/myproject"
        assert row[1] == "myproject"  # derived name
        assert row[2] == now

    def test_touch_existing_project(self, temp_db):
        """Second call updates last_seen without changing first_seen."""
        now1 = db._now()
        proj_id1 = db.upsert_project(temp_db, "/work/myproject", now1)

        now2 = db._now()
        proj_id2 = db.upsert_project(temp_db, "/work/myproject", now2)

        assert proj_id1 == proj_id2
        row = temp_db.execute(
            "SELECT first_seen, last_seen FROM projects WHERE id = ?;", (proj_id1,)
        ).fetchone()
        assert row[0] == now1  # unchanged
        assert row[1] == now2  # updated


class TestUpsertSession:
    """Tests for upsert_session()."""

    def test_insert_new_session(self, temp_db):
        """First call inserts a new session."""
        now = db._now()
        sess_id = db.upsert_session(temp_db, "uuid-123", None, now)

        assert sess_id > 0
        row = temp_db.execute(
            "SELECT session_uuid, project_id, started_at FROM sessions WHERE id = ?;",
            (sess_id,),
        ).fetchone()
        assert row[0] == "uuid-123"
        assert row[1] is None  # no project_id
        assert row[2] == now

    def test_touch_existing_session(self, temp_db):
        """Second call updates last_activity."""
        now1 = db._now()
        sess_id1 = db.upsert_session(temp_db, "uuid-123", None, now1)

        now2 = db._now()
        sess_id2 = db.upsert_session(temp_db, "uuid-123", None, now2)

        assert sess_id1 == sess_id2
        row = temp_db.execute(
            "SELECT started_at, last_activity FROM sessions WHERE id = ?;",
            (sess_id1,),
        ).fetchone()
        assert row[0] == now1  # unchanged
        assert row[1] == now2  # updated


class TestLookupSessionIdByUuid:
    """Tests for lookup_session_id_by_uuid()."""

    def test_returns_session_id_when_uuid_present(self, temp_db):
        """Upserting a session and looking it up by UUID returns the same id."""
        now = db._now()
        sess_id = db.upsert_session(temp_db, "uuid-lookup-1", None, now)

        looked_up = db.lookup_session_id_by_uuid(temp_db, "uuid-lookup-1")

        assert looked_up == sess_id

    def test_returns_none_when_uuid_absent(self, temp_db):
        """Lookup against an empty sessions table returns None."""
        result = db.lookup_session_id_by_uuid(temp_db, "ghost-uuid")
        assert result is None

    def test_uses_session_uuid_column_not_id(self, temp_db):
        """Contract: helper queries the session_uuid column from schema.sql.

        Inserts a row via raw SQL using the exact column names from
        schema.sql (not via upsert_session). If lookup_session_id_by_uuid
        ever switched to querying by id or another column, this would fail.
        """
        now = db._now()
        cur = temp_db.execute(
            "INSERT INTO sessions(session_uuid, project_id, started_at, last_activity)"
            " VALUES (?, ?, ?, ?);",
            ("contract-uuid-xyz", None, now, now),
        )
        inserted_id = int(cur.lastrowid or 0)
        assert inserted_id > 0

        looked_up = db.lookup_session_id_by_uuid(temp_db, "contract-uuid-xyz")

        assert looked_up == inserted_id


class TestUpsertCandidate:
    """Tests for upsert_candidate()."""

    def test_insert_new_candidate(self, temp_db):
        """First call inserts a new candidate."""
        now = db._now()
        # Create a session first
        sess_id = db.upsert_session(temp_db, "uuid-test", None, now)

        flags_json = db.minify_json(["-q"])
        cand_id = db.upsert_candidate(temp_db, "Read", None, flags_json, sess_id, now)

        assert cand_id > 0
        row = temp_db.execute(
            "SELECT verb, subcommand, flags, observations, distinct_sessions"
            " FROM permission_candidates WHERE id = ?;",
            (cand_id,),
        ).fetchone()
        assert row[0] == "Read"
        assert row[1] is None  # no subcommand
        assert row[2] == flags_json
        assert row[3] == 1  # observations
        assert row[4] == 1  # distinct_sessions

    def test_touch_existing_candidate_same_session(self, temp_db):
        """Second call for same candidate+session increments observations."""
        now = db._now()
        sess_id = db.upsert_session(temp_db, "uuid-test", None, now)

        flags_json = db.minify_json(["-q"])
        cand_id1 = db.upsert_candidate(temp_db, "Read", None, flags_json, sess_id, now)
        cand_id2 = db.upsert_candidate(temp_db, "Read", None, flags_json, sess_id, now)

        assert cand_id1 == cand_id2
        row = temp_db.execute(
            "SELECT observations, distinct_sessions FROM permission_candidates WHERE id = ?;",
            (cand_id1,),
        ).fetchone()
        assert row[0] == 2  # incremented
        assert row[1] == 1  # unchanged (same session)

    def test_different_session_increments_distinct(self, temp_db):
        """Same candidate from different session increments distinct_sessions."""
        now = db._now()
        sess_id1 = db.upsert_session(temp_db, "uuid-1", None, now)
        sess_id2 = db.upsert_session(temp_db, "uuid-2", None, now)

        flags_json = db.minify_json(["-q"])
        cand_id1 = db.upsert_candidate(temp_db, "Read", None, flags_json, sess_id1, now)
        cand_id2 = db.upsert_candidate(temp_db, "Read", None, flags_json, sess_id2, now)

        assert cand_id1 == cand_id2
        row = temp_db.execute(
            "SELECT observations, distinct_sessions FROM permission_candidates WHERE id = ?;",
            (cand_id1,),
        ).fetchone()
        assert row[0] == 2  # both sessions touched it
        assert row[1] == 2  # two distinct sessions


class TestUpsertRuleShape:
    """Tests for upsert_rule_shape()."""

    def test_insert_new_rule_shape(self, temp_db):
        """Insert a new rule shape."""
        now = db._now()
        flags_json = db.minify_json(["-q"])
        shape_id = db.upsert_rule_shape(
            temp_db, "Read", None, flags_json, "$HOME/**", now
        )

        assert shape_id > 0
        row = temp_db.execute(
            "SELECT verb, subcommand, flags, path_spec FROM rule_shapes WHERE id = ?;",
            (shape_id,),
        ).fetchone()
        assert row[0] == "Read"
        assert row[1] is None
        assert row[2] == flags_json
        assert row[3] == "$HOME/**"

    def test_touch_existing_rule_shape(self, temp_db):
        """Touch updates last_seen."""
        now1 = db._now()
        flags_json = db.minify_json(["-q"])
        shape_id1 = db.upsert_rule_shape(
            temp_db, "Read", None, flags_json, "$HOME/**", now1
        )

        now2 = db._now()
        shape_id2 = db.upsert_rule_shape(
            temp_db, "Read", None, flags_json, "$HOME/**", now2
        )

        assert shape_id1 == shape_id2
        row = temp_db.execute(
            "SELECT first_seen, last_seen FROM rule_shapes WHERE id = ?;",
            (shape_id1,),
        ).fetchone()
        assert row[0] == now1
        assert row[1] == now2

    def test_pattern_verb_prefix(self, temp_db):
        """Pattern verbs like '$VAR/...' are stored as-is."""
        now = db._now()
        flags_json = db.minify_json([])
        shape_id = db.upsert_rule_shape(
            temp_db, "$VAR/subcommand", None, flags_json, None, now
        )

        row = temp_db.execute(
            "SELECT verb FROM rule_shapes WHERE id = ?;", (shape_id,)
        ).fetchone()
        assert row[0] == "$VAR/subcommand"

    def test_wildcard_flags(self, temp_db):
        """Wildcard flags='*' are stored as-is."""
        now = db._now()
        shape_id = db.upsert_rule_shape(temp_db, "Read", None, "*", None, now)

        row = temp_db.execute(
            "SELECT flags FROM rule_shapes WHERE id = ?;", (shape_id,)
        ).fetchone()
        assert row[0] == "*"


class TestInsertPermission:
    """Tests for insert_permission()."""

    def test_insert_approved_global(self, temp_db):
        """Insert a global-tier approved permission."""
        now = db._now()
        flags_json = db.minify_json([])
        shape_id = db.upsert_rule_shape(temp_db, "Read", None, flags_json, None, now)

        perm_id = db.insert_permission(
            temp_db,
            shape_id,
            session_id=None,
            project_id=None,
            decision="approved",
            source="seed",
            ts=now,
        )

        assert perm_id > 0
        row = temp_db.execute(
            "SELECT decision, source, session_id, project_id FROM permissions WHERE id = ?;",
            (perm_id,),
        ).fetchone()
        assert row[0] == "approved"
        assert row[1] == "seed"
        assert row[2] is None
        assert row[3] is None

    def test_insert_rejected_with_reason(self, temp_db):
        """Insert a rejected permission with reason."""
        now = db._now()
        flags_json = db.minify_json([])
        shape_id = db.upsert_rule_shape(temp_db, "Bash", None, flags_json, None, now)

        perm_id = db.insert_permission(
            temp_db,
            shape_id,
            session_id=None,
            project_id=None,
            decision="rejected",
            source="learner",
            ts=now,
            reason="dangerous",
        )

        row = temp_db.execute(
            "SELECT decision, reason FROM permissions WHERE id = ?;", (perm_id,)
        ).fetchone()
        assert row[0] == "rejected"
        assert row[1] == "dangerous"

    def test_insert_session_tier(self, temp_db):
        """Insert a session-tier permission."""
        now = db._now()
        sess_id = db.upsert_session(temp_db, "uuid-test", None, now)
        flags_json = db.minify_json([])
        shape_id = db.upsert_rule_shape(temp_db, "Read", None, flags_json, None, now)

        perm_id = db.insert_permission(
            temp_db,
            shape_id,
            session_id=sess_id,
            project_id=None,
            decision="approved",
            source="session-ask",
            ts=now,
        )

        row = temp_db.execute(
            "SELECT session_id, project_id FROM permissions WHERE id = ?;",
            (perm_id,),
        ).fetchone()
        assert row[0] == sess_id
        assert row[1] is None

    def test_invalid_decision_raises(self, temp_db):
        """Invalid decision raises ValueError."""
        now = db._now()
        flags_json = db.minify_json([])
        shape_id = db.upsert_rule_shape(temp_db, "Read", None, flags_json, None, now)

        with pytest.raises(ValueError, match="invalid decision"):
            db.insert_permission(
                temp_db,
                shape_id,
                None,
                None,
                "maybe",  # invalid
                "seed",
                now,
            )


class TestLookupPermissions:
    """Tests for lookup_permissions()."""

    def test_lookup_global_permission(self, temp_db):
        """Look up a global permission."""
        now = db._now()
        flags_json = db.minify_json([])
        shape_id = db.upsert_rule_shape(temp_db, "Read", None, flags_json, None, now)
        db.insert_permission(temp_db, shape_id, None, None, "approved", "seed", now)

        rows = db.lookup_permissions(temp_db, shape_id, None, None)

        assert len(rows) == 1
        assert rows[0]["decision"] == "approved"
        assert rows[0]["source"] == "seed"

    def test_lookup_tier_priority(self, temp_db):
        """Lookup returns rows in tier priority order (session → project → global)."""
        now = db._now()
        sess_id = db.upsert_session(temp_db, "uuid-test", None, now)
        proj_id = db.upsert_project(temp_db, "/work/test", now)

        flags_json = db.minify_json([])
        shape_id = db.upsert_rule_shape(temp_db, "Read", None, flags_json, None, now)

        # Insert all three tiers
        db.insert_permission(temp_db, shape_id, None, None, "rejected", "seed", now)
        db.insert_permission(
            temp_db, shape_id, None, proj_id, "approved", "manual", now
        )
        db.insert_permission(temp_db, shape_id, sess_id, None, "approved", "seed", now)

        rows = db.lookup_permissions(temp_db, shape_id, sess_id, proj_id)

        # Should have all three rows, session first
        assert len(rows) == 3
        assert rows[0]["session_id"] == sess_id  # session-tier first
        assert rows[1]["project_id"] == proj_id  # project-tier second
        # global third (no id fields set)

    def test_lookup_no_match(self, temp_db):
        """Lookup for non-existent shape returns empty list."""
        rows = db.lookup_permissions(temp_db, 99999, None, None)
        assert rows == []


class TestMinifyJson:
    """Tests for minify_json()."""

    def test_minify_removes_whitespace(self):
        """Minification removes all whitespace."""
        obj = {"key": "value", "list": [1, 2, 3]}
        result = db.minify_json(obj)
        assert " " not in result

    def test_minify_preserves_utf8(self):
        """Minification preserves UTF-8 characters."""
        obj = {"emoji": "🎉"}
        result = db.minify_json(obj)
        assert "🎉" in result


class TestLookupHelpers:
    """Tests for lookup_*_id helpers."""

    def test_lookup_permission_mode_id_exists(self, temp_db):
        """Permission mode id lookup for existing mode."""
        # "default" is seeded in the fixture
        mode_id = db.lookup_permission_mode_id(temp_db, "default")
        assert mode_id is not None and mode_id > 0

    def test_lookup_permission_mode_id_none(self, temp_db):
        """Permission mode id lookup for None returns None."""
        mode_id = db.lookup_permission_mode_id(temp_db, None)
        assert mode_id is None

    def test_lookup_permission_mode_id_unknown(self, temp_db):
        """Permission mode id lookup for unknown mode returns None."""
        mode_id = db.lookup_permission_mode_id(temp_db, "unknown-mode")
        assert mode_id is None

    def test_lookup_status_id_exists(self, temp_db):
        """Status id lookup for existing status."""
        # "ok" is seeded in the fixture
        status_id = db.lookup_status_id(temp_db, "ok")
        assert status_id is not None and status_id > 0

    def test_lookup_status_id_unknown_raises(self, temp_db):
        """Status id lookup for unknown status raises ValueError."""
        with pytest.raises(ValueError, match="unknown call status"):
            db.lookup_status_id(temp_db, "unknown-status")

    def test_lookup_or_insert_tool_id_inserts_new(self, temp_db):
        """Tool id lookup inserts new tool on first sight."""
        tool_id = db.lookup_or_insert_tool_id(temp_db, "NewTool")
        assert tool_id > 0

        # Second lookup should return same id
        tool_id2 = db.lookup_or_insert_tool_id(temp_db, "NewTool")
        assert tool_id == tool_id2

    def test_lookup_or_insert_subagent_type_id_none(self, temp_db):
        """Subagent type id lookup for None returns None."""
        sa_id = db.lookup_or_insert_subagent_type_id(temp_db, None)
        assert sa_id is None

    def test_lookup_or_insert_subagent_type_id_inserts_new(self, temp_db):
        """Subagent type id lookup inserts new type on first sight."""
        sa_id = db.lookup_or_insert_subagent_type_id(temp_db, "researcher")
        assert sa_id is not None and sa_id > 0

        sa_id2 = db.lookup_or_insert_subagent_type_id(temp_db, "researcher")
        assert sa_id == sa_id2

    def test_lookup_or_insert_file_path_id_none(self, temp_db):
        """File path id lookup for None returns None."""
        now = db._now()
        path_id = db.lookup_or_insert_file_path_id(temp_db, None, now)
        assert path_id is None

    def test_lookup_or_insert_file_path_id_inserts_new(self, temp_db):
        """File path id lookup inserts new path on first sight."""
        now = db._now()
        path_id = db.lookup_or_insert_file_path_id(temp_db, "/home/user/file.txt", now)
        assert path_id is not None and path_id > 0

        path_id2 = db.lookup_or_insert_file_path_id(temp_db, "/home/user/file.txt", now)
        assert path_id == path_id2


class TestWriteExtra:
    """Tests for write_extra()."""

    def test_write_extra_inserts_new(self, temp_db):
        """write_extra inserts a new sidecar row."""
        now = db._now()
        # Create a tool_call first
        sess_id = db.upsert_session(temp_db, "uuid-test", None, now)
        tool_id = db.lookup_or_insert_tool_id(temp_db, "Read")
        status_id = db.lookup_status_id(temp_db, "ok")

        # Insert a mock tool_call
        cur = temp_db.execute(
            "INSERT INTO tool_calls(ts, session_id, tool_id, status_id)"
            " VALUES (?, ?, ?, ?);",
            (now, sess_id, tool_id, status_id),
        )
        tool_call_id = cur.lastrowid

        db.write_extra(temp_db, tool_call_id, "key1", "value1")

        row = temp_db.execute(
            "SELECT name, value FROM tool_extras WHERE tool_call_id = ?;",
            (tool_call_id,),
        ).fetchone()
        assert row[0] == "key1"
        assert row[1] == "value1"

    def test_write_extra_replaces_existing(self, temp_db):
        """write_extra replaces existing sidecar row for same key."""
        now = db._now()
        sess_id = db.upsert_session(temp_db, "uuid-test", None, now)
        tool_id = db.lookup_or_insert_tool_id(temp_db, "Read")
        status_id = db.lookup_status_id(temp_db, "ok")

        cur = temp_db.execute(
            "INSERT INTO tool_calls(ts, session_id, tool_id, status_id)"
            " VALUES (?, ?, ?, ?);",
            (now, sess_id, tool_id, status_id),
        )
        tool_call_id = cur.lastrowid

        db.write_extra(temp_db, tool_call_id, "key1", "value1")
        db.write_extra(temp_db, tool_call_id, "key1", "value2")

        rows = temp_db.execute(
            "SELECT COUNT(*) FROM tool_extras WHERE tool_call_id = ? AND name = ?;",
            (tool_call_id, "key1"),
        ).fetchone()
        assert rows[0] == 1  # Only one row (replaced)

        row = temp_db.execute(
            "SELECT value FROM tool_extras WHERE tool_call_id = ? AND name = ?;",
            (tool_call_id, "key1"),
        ).fetchone()
        assert row[0] == "value2"


class TestUpsertProjectCanonicalizes:
    """Integration tests: upsert_project must canonicalize cwd + root at write.

    Two forms of the same logical path (tilde vs absolute, symlink vs realpath)
    must land as one row, not two. Read-side defensive canonicalization is kept
    for belt-and-braces, but storage identity is the point.
    """

    def test_upsert_project_dedups_tilde_vs_absolute(
        self, temp_db, tmp_path, monkeypatch
    ):
        """~/proj and /<fake-home>/proj upserted back-to-back yield one row."""
        monkeypatch.setenv("HOME", str(tmp_path))
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()

        now1 = db._now()
        id1 = db.upsert_project(temp_db, "~/proj", now1)
        now2 = db._now()
        id2 = db.upsert_project(temp_db, str(proj_dir), now2)

        assert id1 == id2, (
            f"tilde ~/proj and absolute {proj_dir} produced different ids "
            f"({id1} vs {id2}) — canonicalize() not applied at write"
        )
        count = temp_db.execute(
            "SELECT COUNT(*) FROM projects WHERE cwd = ?;", (str(proj_dir),)
        ).fetchone()[0]
        assert count == 1, (
            f"expected exactly one row for canonical {proj_dir}, got {count}"
        )

    def test_upsert_project_dedups_symlink_vs_realpath(self, temp_db, tmp_path):
        """Symlink path and its realpath dedupe to one row."""
        real = tmp_path / "real-proj"
        real.mkdir()
        link = tmp_path / "link-proj"
        link.symlink_to(real)

        now1 = db._now()
        id1 = db.upsert_project(temp_db, str(link), now1)
        now2 = db._now()
        id2 = db.upsert_project(temp_db, str(real), now2)

        assert id1 == id2, (
            f"symlink {link} and realpath {real} produced different ids "
            f"({id1} vs {id2}) — canonicalize() not applied at write"
        )
        count = temp_db.execute("SELECT COUNT(*) FROM projects;").fetchone()[0]
        assert count == 1, f"expected exactly one project row, got {count}"

    def test_upsert_project_stores_canonical_cwd(self, temp_db, tmp_path, monkeypatch):
        """The stored cwd column holds the canonical (expanduser+resolve) form."""
        monkeypatch.setenv("HOME", str(tmp_path))
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()

        now = db._now()
        proj_id = db.upsert_project(temp_db, "~/proj", now)

        stored = temp_db.execute(
            "SELECT cwd FROM projects WHERE id = ?;", (proj_id,)
        ).fetchone()[0]
        assert stored == str(proj_dir), (
            f"stored cwd is {stored!r}; expected canonical {str(proj_dir)!r} "
            f"— upsert_project did not canonicalize on insert"
        )
        assert "~" not in stored, (
            f"stored cwd still contains '~': {stored!r} — tilde not expanded"
        )

    def test_upsert_project_stores_canonical_root(self, temp_db, tmp_path, monkeypatch):
        """The stored root column holds a canonical form (no tilde, no symlink)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir()

        now = db._now()
        proj_id = db.upsert_project(temp_db, "~/proj", now)

        stored_root = temp_db.execute(
            "SELECT root FROM projects WHERE id = ?;", (proj_id,)
        ).fetchone()[0]
        assert stored_root is not None, (
            "resolve_project_root falls back to cwd, so stored root must be set"
        )
        assert "~" not in stored_root, (
            f"stored root contains '~': {stored_root!r} — tilde not expanded"
        )
        assert canonicalize(stored_root) == stored_root, (
            f"stored root {stored_root!r} is not canonical "
            f"— canonicalize not applied to root at write"
        )

    def test_upsert_project_idempotent(self, temp_db):
        """Back-to-back upserts of the same cwd return the same id and one row."""
        # Fixed timestamps to sidestep same-millisecond collisions — the
        # assertion below cares that last_seen moved to now2, not that now2
        # was later than now1 in wall-clock terms.
        now1 = "2024-01-01T00:00:00.000Z"
        now2 = "2024-01-01T00:00:00.001Z"
        id1 = db.upsert_project(temp_db, "/work/proj-x", now1)
        id2 = db.upsert_project(temp_db, "/work/proj-x", now2)

        assert id1 == id2
        count = temp_db.execute(
            "SELECT COUNT(*) FROM projects WHERE cwd = ?;", ("/work/proj-x",)
        ).fetchone()[0]
        assert count == 1
        last_seen = temp_db.execute(
            "SELECT last_seen FROM projects WHERE id = ?;", (id1,)
        ).fetchone()[0]
        assert last_seen == now2, "second upsert must bump last_seen"

    def test_upsert_project_two_connections_dedupe(self, two_connections):
        """Two connections upserting the same cwd produce one row, same id.

        With autocommit + WAL, connection A's INSERT commits before B starts;
        B's fast-path SELECT sees the row and takes the UPDATE branch.
        Proves the user-visible race-safety claim ("two writers → one row"),
        but does not exercise the ON CONFLICT DO UPDATE branch — see the
        companion test below for that.
        """
        conn_a, conn_b = two_connections
        id_a = db.upsert_project(conn_a, "/work/race-proj", db._now())
        id_b = db.upsert_project(conn_b, "/work/race-proj", db._now())

        assert id_a == id_b, (
            f"UPSERT returned different ids under two-writer scenario: {id_a} vs {id_b}"
        )
        count = conn_a.execute(
            "SELECT COUNT(*) FROM projects WHERE cwd = ?;", ("/work/race-proj",)
        ).fetchone()[0]
        assert count == 1, (
            f"expected one row after two writers, got {count} — dedup failed"
        )

    def test_upsert_project_on_conflict_branch_returns_existing_id(
        self, two_connections
    ):
        """The ON CONFLICT DO UPDATE branch returns the existing row's id.

        Forces the fast-path SELECT to miss by seeding a row with
        ``root IS NULL`` (fast-path needs both ``row is not None`` and
        ``root is not None``). The second caller falls through to the
        INSERT, hits the UNIQUE constraint, takes DO UPDATE, and must
        return the pre-existing id — not create a duplicate or return 0.
        """
        conn_a, conn_b = two_connections
        # Seed a row with NULL root directly — simulates a caller that
        # wrote the cwd row before root could be resolved.
        conn_a.execute(
            "INSERT INTO projects(cwd, name, root, first_seen, last_seen)"
            " VALUES (?, ?, NULL, ?, ?);",
            ("/work/conflict-proj", "conflict-proj", db._now(), db._now()),
        )
        seed_id = conn_a.execute(
            "SELECT id FROM projects WHERE cwd = ?;", ("/work/conflict-proj",)
        ).fetchone()[0]

        # conn_b's upsert sees row with root IS NULL → fast-path miss →
        # ON CONFLICT branch fires.
        returned_id = db.upsert_project(conn_b, "/work/conflict-proj", db._now())

        assert returned_id == seed_id, (
            f"ON CONFLICT branch returned {returned_id}; expected existing id "
            f"{seed_id}. RETURNING must yield the conflict target's id."
        )
        count = conn_a.execute(
            "SELECT COUNT(*) FROM projects WHERE cwd = ?;", ("/work/conflict-proj",)
        ).fetchone()[0]
        assert count == 1, f"duplicate row created — got {count}"
        # The backfill via COALESCE(projects.root, excluded.root) should have
        # populated root — rule 3 of resolve_project_root returns cwd verbatim
        # when git fails, so `excluded.root` is non-NULL for any non-empty cwd.
        root = conn_a.execute(
            "SELECT root FROM projects WHERE id = ?;", (seed_id,)
        ).fetchone()[0]
        assert root is not None, (
            "ON CONFLICT DO UPDATE should have backfilled root via COALESCE"
        )


class TestLookupOrInsertFilePathCanonicalizes:
    """lookup_or_insert_file_path_id must canonicalize before SELECT/INSERT."""

    def test_file_path_dedups_tilde_vs_absolute(self, temp_db, tmp_path, monkeypatch):
        """~/foo.txt and /<fake-home>/foo.txt are one row."""
        monkeypatch.setenv("HOME", str(tmp_path))
        now = db._now()

        id1 = db.lookup_or_insert_file_path_id(temp_db, "~/foo.txt", now)
        id2 = db.lookup_or_insert_file_path_id(temp_db, str(tmp_path / "foo.txt"), now)

        assert id1 == id2, (
            f"tilde ~/foo.txt and absolute {tmp_path}/foo.txt got different ids "
            f"({id1} vs {id2}) — canonicalize() not applied"
        )
        count = temp_db.execute("SELECT COUNT(*) FROM file_paths;").fetchone()[0]
        assert count == 1, f"expected exactly one file_paths row, got {count}"

    def test_file_path_dedups_symlink_vs_realpath(self, temp_db, tmp_path):
        """Symlink path and realpath dedupe to one file_paths row."""
        real = tmp_path / "real"
        real.mkdir()
        (real / "file.txt").write_text("x")
        link = tmp_path / "link"
        link.symlink_to(real)

        now = db._now()
        id1 = db.lookup_or_insert_file_path_id(temp_db, str(link / "file.txt"), now)
        id2 = db.lookup_or_insert_file_path_id(temp_db, str(real / "file.txt"), now)

        assert id1 == id2, (
            f"symlink and realpath got different ids ({id1} vs {id2}) "
            f"— canonicalize() not applied at write"
        )

    def test_file_path_stores_canonical(self, temp_db, tmp_path, monkeypatch):
        """The stored path column holds the canonical form."""
        monkeypatch.setenv("HOME", str(tmp_path))
        now = db._now()
        path_id = db.lookup_or_insert_file_path_id(temp_db, "~/foo.txt", now)

        stored = temp_db.execute(
            "SELECT path FROM file_paths WHERE id = ?;", (path_id,)
        ).fetchone()[0]
        assert stored == str(tmp_path / "foo.txt"), (
            f"stored path is {stored!r}; expected canonical "
            f"{str(tmp_path / 'foo.txt')!r}"
        )
        assert "~" not in stored, f"stored path still has '~': {stored!r}"

    def test_file_path_none_still_returns_none(self, temp_db):
        """Canonicalization of file_paths does not change the None short-circuit."""
        # Regression guard: the existing None→None contract must survive.
        now = db._now()
        assert db.lookup_or_insert_file_path_id(temp_db, None, now) is None

    def test_file_path_idempotent(self, temp_db):
        """Same path on one connection twice returns the same id, bumps last_seen."""
        now1 = "2024-01-01T00:00:00.000Z"
        now2 = "2024-01-01T00:00:00.001Z"
        id1 = db.lookup_or_insert_file_path_id(temp_db, "/tmp/idem.txt", now1)
        id2 = db.lookup_or_insert_file_path_id(temp_db, "/tmp/idem.txt", now2)

        assert id1 == id2
        count = temp_db.execute(
            "SELECT COUNT(*) FROM file_paths WHERE path = ?;", ("/tmp/idem.txt",)
        ).fetchone()[0]
        assert count == 1
        last_seen = temp_db.execute(
            "SELECT last_seen FROM file_paths WHERE id = ?;", (id1,)
        ).fetchone()[0]
        assert last_seen == now2

    def test_file_path_concurrent_connections_dedupe(self, two_connections):
        """Two connections inserting the same path produce one row, same id.

        Exercises the ``INSERT ... ON CONFLICT(path) DO UPDATE ... RETURNING``
        path: connection A inserts; connection B hits the UNIQUE conflict and
        takes the UPDATE branch. Both receive the same id.
        """
        conn_a, conn_b = two_connections
        now = db._now()
        id_a = db.lookup_or_insert_file_path_id(conn_a, "/tmp/race.txt", now)
        id_b = db.lookup_or_insert_file_path_id(conn_b, "/tmp/race.txt", now)

        assert id_a == id_b, (
            f"UPSERT returned different ids under concurrent writers: {id_a} vs {id_b}"
        )
        count = conn_a.execute(
            "SELECT COUNT(*) FROM file_paths WHERE path = ?;", ("/tmp/race.txt",)
        ).fetchone()[0]
        assert count == 1, (
            f"expected one row after two writers, got {count} — "
            f"ON CONFLICT did not take effect"
        )


class TestRecorderCanonicalizesTranscriptPath:
    """recorder/run.py must canonicalize transcript_path before UPDATE.

    The recorder's pre-phase writes transcript_path into sessions (set-once).
    If two callers send the same logical transcript with different tilde/symlink
    forms, the set-once semantics only work if the stored form is canonical.
    """

    def test_transcript_path_stored_canonical(self, tmp_db, tmp_path, monkeypatch):
        """transcript_path is canonicalized before being stored on sessions."""
        monkeypatch.setenv("HOME", str(tmp_path))
        transcript_dir = tmp_path / "transcripts"
        transcript_dir.mkdir()

        payload = {
            "session_id": "019673a0-aaaa-7000-8000-000000000099",
            "transcript_path": "~/transcripts/t.jsonl",
            "cwd": str(tmp_path / "proj"),
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "echo x"},
            "tool_use_id": "toolu_canonical_transcript_test",
        }
        (tmp_path / "proj").mkdir()
        recorder._handle("pre", payload)

        stored = tmp_db.execute(
            "SELECT transcript_path FROM sessions WHERE session_uuid = ?;",
            (payload["session_id"],),
        ).fetchone()[0]
        expected = str(transcript_dir / "t.jsonl")
        assert stored == expected, (
            f"stored transcript_path is {stored!r}, expected canonical "
            f"{expected!r} — canonicalize() not applied at recorder UPDATE site"
        )
        assert "~" not in (stored or ""), (
            f"stored transcript_path contains '~': {stored!r}"
        )
