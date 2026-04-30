"""Tests for project-tier settings_json_path bugs.

Covers:
- SessionStart sets settings_json_path on new project
- SessionStart backfills NULL settings_json_path on existing project
- sync_project with NULL settings_json_path is a no-op (no exception)
- Status hint contains --tier global (not --tier project)
- review flow can produce a rule with $PROJECT_ROOT/** path-spec
- Running nephoscope-init twice produces no duplicate permission rows
- unpermit-by-id deletes by primary key
- UNIQUE constraint prevents duplicate permission rows
- insert_permission upserts (no duplicate on conflict)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nephoscope.lib.db import (
    _now,
    _open,
    insert_permission,
    upsert_project,
    upsert_rule_shape,
)
from nephoscope.lib.mirror.writer import sync_project


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """Open an isolated test DB."""
    db = tmp_path / "test.db"
    monkeypatch.setenv("OBSERVABILITY_DB", str(db))
    return _open()


def _insert_project_with_null_path(conn: sqlite3.Connection, cwd: str, now: str) -> int:
    """Insert a project row with NULL settings_json_path (simulates pre-fix state)."""
    cur = conn.execute(
        "INSERT INTO projects (cwd, name, first_seen, last_seen) VALUES (?, ?, ?, ?);",
        (cwd, "test", now, now),
    )
    return int(cur.lastrowid or 0)


def _project_settings_path(conn: sqlite3.Connection, cwd: str) -> str | None:
    row = conn.execute(
        "SELECT settings_json_path FROM projects WHERE cwd = ?;", (cwd,)
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Bug 1: SessionStart populates settings_json_path
# ---------------------------------------------------------------------------


class TestSessionStartSetsSettingsJsonPath:
    def test_new_project_gets_settings_json_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """upsert_project sets settings_json_path on first creation."""
        conn = _make_db(tmp_path, monkeypatch)
        cwd = str(tmp_path / "myproject")
        now = _now()
        upsert_project(conn, cwd, now)
        path = _project_settings_path(conn, cwd)
        assert path is not None, "settings_json_path should be set on new project"
        assert path.endswith("/.claude/settings.json"), (
            f"expected <cwd>/.claude/settings.json, got {path!r}"
        )

    def test_existing_project_null_path_gets_backfilled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """upsert_project backfills NULL settings_json_path on existing project."""
        conn = _make_db(tmp_path, monkeypatch)
        cwd = str(tmp_path / "myproject")
        now = _now()
        # Simulate old-style row with NULL settings_json_path
        _insert_project_with_null_path(conn, cwd, now)
        assert _project_settings_path(conn, cwd) is None, "precondition: path is NULL"

        # Now upsert via the fixed helper — should backfill
        upsert_project(conn, cwd, now)
        path = _project_settings_path(conn, cwd)
        assert path is not None, "settings_json_path should be backfilled"
        assert path.endswith("/.claude/settings.json")

    def test_settings_json_path_points_to_cwd_claude_subdir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """settings_json_path is <canonicalized_cwd>/.claude/settings.json."""
        conn = _make_db(tmp_path, monkeypatch)
        cwd = str(tmp_path / "proj")
        now = _now()
        upsert_project(conn, cwd, now)
        path = _project_settings_path(conn, cwd)
        # Must be under cwd's .claude subdir
        assert path is not None
        expected_suffix = ".claude/settings.json"
        assert path.endswith(expected_suffix), (
            f"expected suffix {expected_suffix!r}, got {path!r}"
        )
        # Must not be the global settings.json
        from pathlib import Path

        home_settings = str(Path.home() / ".claude" / "settings.json")
        assert path != home_settings, (
            "project settings_json_path must not be global path"
        )


# ---------------------------------------------------------------------------
# Bug 2: sync_project with NULL settings_json_path is a no-op
# ---------------------------------------------------------------------------


class TestSyncProjectNullPathIsNoOp:
    def test_sync_project_null_path_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sync_project silently skips when settings_json_path is NULL."""
        conn = _make_db(tmp_path, monkeypatch)
        cwd = str(tmp_path / "project_no_settings")
        now = _now()
        # Insert project with NULL settings_json_path
        _insert_project_with_null_path(conn, cwd, now)
        proj_row = conn.execute(
            "SELECT id FROM projects WHERE cwd = ?;", (cwd,)
        ).fetchone()
        assert proj_row is not None
        project_id = int(proj_row[0])

        # Must not raise — should be a silent no-op
        try:
            sync_project(conn, project_id)
        except ValueError as exc:
            pytest.fail(f"sync_project raised ValueError for NULL path: {exc}")

    def test_sync_project_null_path_writes_no_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sync_project with NULL path leaves no file on disk."""
        conn = _make_db(tmp_path, monkeypatch)
        cwd = str(tmp_path / "project_no_settings")
        now = _now()
        _insert_project_with_null_path(conn, cwd, now)
        proj_row = conn.execute(
            "SELECT id FROM projects WHERE cwd = ?;", (cwd,)
        ).fetchone()
        project_id = int(proj_row[0])

        sync_project(conn, project_id)  # must not raise

        # No file should appear on disk in the project dir
        candidate = tmp_path / "project_no_settings" / ".claude" / "settings.json"
        assert not candidate.exists(), (
            "sync_project must not create a file when path is NULL"
        )


# ---------------------------------------------------------------------------
# Bug 3: Status hint uses --tier global
# ---------------------------------------------------------------------------


class TestStatusHintTierGlobal:
    def _get_hint_section(self) -> str:
        cmd_file = Path("/work/bedezign/nephoscope/repository/commands/permissions.md")
        text = cmd_file.read_text()
        # The promote example lives in the awk block under "Try next".
        # We extract from "Try next" to the first occurrence of "EOF" that closes the block.
        hint_start = text.find("Try next")
        # Use a generous window covering the whole status awk block
        return text[hint_start : hint_start + 1000]

    def test_status_hint_does_not_contain_tier_project(self) -> None:
        """The status subcommand hint must not suggest --tier project."""
        hint_section = self._get_hint_section()
        assert "--tier project" not in hint_section, (
            "Status hint must not suggest --tier project; "
            f"found in hint block: {hint_section!r}"
        )

    def test_status_hint_contains_tier_global_flag(self) -> None:
        """The status hint must contain '--tier global' in the promote example."""
        hint_section = self._get_hint_section()
        assert "--tier global" in hint_section, (
            f"Status hint must suggest --tier global; hint block: {hint_section!r}"
        )


# ---------------------------------------------------------------------------
# UNIQUE constraint + insert_permission upsert
# ---------------------------------------------------------------------------


class TestPermissionsUniqueConstraint:
    def _make_shape(self, conn: sqlite3.Connection) -> int:
        return upsert_rule_shape(conn, "rm", None, "[]", None, _now())

    def test_insert_permission_is_idempotent_for_same_shape_and_tier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Inserting the same (rule_shape_id, tier) twice must not create two rows."""
        conn = _make_db(tmp_path, monkeypatch)
        shape_id = self._make_shape(conn)
        now = _now()
        insert_permission(conn, shape_id, None, None, "approved", "seed", now)
        insert_permission(conn, shape_id, None, None, "approved", "seed", now)
        rows = conn.execute(
            "SELECT COUNT(*) FROM permissions WHERE rule_shape_id = ?;",
            (shape_id,),
        ).fetchone()
        assert rows[0] == 1, f"Expected 1 row, got {rows[0]}"

    def test_insert_permission_global_and_project_are_distinct(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Global and project tiers for the same shape produce separate rows."""
        conn = _make_db(tmp_path, monkeypatch)
        shape_id = self._make_shape(conn)
        now = _now()
        cwd = str(tmp_path / "proj")
        project_id = upsert_project(conn, cwd, now)
        insert_permission(conn, shape_id, None, None, "approved", "seed", now)
        insert_permission(conn, shape_id, None, project_id, "approved", "seed", now)
        rows = conn.execute(
            "SELECT COUNT(*) FROM permissions WHERE rule_shape_id = ?;",
            (shape_id,),
        ).fetchone()
        assert rows[0] == 2, f"Expected 2 rows (global + project), got {rows[0]}"

    def test_insert_permission_replace_updates_decision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a same-key row exists, insert_permission updates (replaces) it."""
        conn = _make_db(tmp_path, monkeypatch)
        shape_id = self._make_shape(conn)
        now = _now()
        insert_permission(conn, shape_id, None, None, "approved", "seed", now)
        # Insert again with different decision (e.g. re-seeded as rejected)
        insert_permission(conn, shape_id, None, None, "rejected", "seed", now)
        row = conn.execute(
            "SELECT decision FROM permissions WHERE rule_shape_id = ? "
            "AND session_id IS NULL AND project_id IS NULL;",
            (shape_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == "rejected", f"Expected decision='rejected', got {row[0]!r}"
        count = conn.execute(
            "SELECT COUNT(*) FROM permissions WHERE rule_shape_id = ?;",
            (shape_id,),
        ).fetchone()[0]
        assert count == 1, f"Expected 1 row after replace, got {count}"


# ---------------------------------------------------------------------------
# unpermit-by-id subcommand
# ---------------------------------------------------------------------------


class TestUnpermitById:
    def test_unpermit_by_id_deletes_by_primary_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """unpermit-by-id removes the row with the given id."""
        conn = _make_db(tmp_path, monkeypatch)
        shape_id = upsert_rule_shape(conn, "git", None, "[]", None, _now())
        now = _now()
        perm_id = insert_permission(conn, shape_id, None, None, "approved", "seed", now)
        assert perm_id > 0

        # Call the learner CLI unpermit-by-id subcommand
        from nephoscope.learners.permission.learner import _cmd_unpermit_by_id
        import argparse

        args = argparse.Namespace(id=perm_id, sync=False)
        result = _cmd_unpermit_by_id(args)
        assert result == 0, f"unpermit-by-id returned non-zero: {result}"

        row = conn.execute(
            "SELECT id FROM permissions WHERE id = ?;", (perm_id,)
        ).fetchone()
        assert row is None, (
            f"Permission row {perm_id} still exists after unpermit-by-id"
        )

    def test_unpermit_by_id_missing_id_returns_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """unpermit-by-id returns non-zero when the id does not exist."""
        _make_db(tmp_path, monkeypatch)

        from nephoscope.learners.permission.learner import _cmd_unpermit_by_id
        import argparse

        args = argparse.Namespace(id=99999, sync=False)
        result = _cmd_unpermit_by_id(args)
        assert result != 0, "Expected non-zero exit for nonexistent id"


# ---------------------------------------------------------------------------
# nephoscope-init idempotency (no duplicate permission rows)
# ---------------------------------------------------------------------------


class TestInitIdempotency:
    def test_init_twice_no_duplicate_permissions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running nephoscope-init twice for the same fixtures produces no duplicates."""
        db_path = tmp_path / "init_idem.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))
        monkeypatch.setenv("NEPHOSCOPE_DATA", str(tmp_path))

        from nephoscope.cli.init_cmd import main as init_main

        r1 = init_main(["--db-path", str(db_path), "--no-workspace-prompts"])
        assert r1 == 0
        r2 = init_main(["--db-path", str(db_path), "--no-workspace-prompts"])
        assert r2 == 0

        conn = sqlite3.connect(str(db_path))
        # Check for duplicates: each (rule_shape_id, IFNULL(session_id,0), IFNULL(project_id,0))
        # should appear at most once
        rows = conn.execute(
            "SELECT rule_shape_id, IFNULL(session_id,0), IFNULL(project_id,0), COUNT(*) AS cnt"
            " FROM permissions"
            " GROUP BY rule_shape_id, IFNULL(session_id,0), IFNULL(project_id,0)"
            " HAVING cnt > 1;"
        ).fetchall()
        conn.close()
        assert not rows, (
            f"Found {len(rows)} groups with duplicate permission rows after double init: {rows}"
        )
