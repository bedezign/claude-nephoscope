"""Tests for dispatch() non_bash_tool_matching config flag.

The flag enables full DB matching for Write/Edit/Read tools without
requiring HOOK_FULL_MATCH=on.

Coverage:
  1. Write returns NoOpinion when flag is off (default)
  2. Write falls through to DB matcher when flag is on
  3. Edit falls through to DB matcher when flag is on
  4. Read falls through to DB matcher when flag is on
  5. Bash is unaffected (always full match regardless of flag)
  6. Non-file-class tools (Grep, Glob) still return NoOpinion when flag is on
  7. MultiEdit and NotebookEdit (file-class but not in _FILE_CLASS_TOOLS) still NoOpinion
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nephoscope.learners.permission.match import Verdict, dispatch


# ---------------------------------------------------------------------------
# Autouse cache-clear fixture — lru_cache must not leak between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_config_cache():
    from nephoscope.config import get_config

    get_config.cache_clear()
    yield
    get_config.cache_clear()


# ---------------------------------------------------------------------------
# Config file helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, *, non_bash_tool_matching: bool) -> Path:
    """Write a minimal TOML config to tmp_path and return its path."""
    cfg = tmp_path / "config.toml"
    value = "true" if non_bash_tool_matching else "false"
    cfg.write_text(f"non_bash_tool_matching = {value}\n")
    return cfg


# ---------------------------------------------------------------------------
# DB helpers (duplicated from test_matcher_dispatch for isolation)
# ---------------------------------------------------------------------------


def _insert_rule_shape(
    conn: sqlite3.Connection,
    verb: str,
    path_spec: str | None = None,
) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO rule_shapes"
        " (verb, subcommand, flags, path_spec, first_seen, last_seen)"
        " VALUES (?, NULL, '[]', ?, '2025-01-01Z', '2025-01-01Z');",
        (verb, path_spec),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM rule_shapes"
        " WHERE verb=? AND subcommand IS NULL AND flags='[]'"
        " AND IFNULL(path_spec,'')=IFNULL(?,'');",
        (verb, path_spec),
    ).fetchone()
    return int(row[0]) if row else 0


def _insert_permission(
    conn: sqlite3.Connection,
    rule_shape_id: int,
    decision: str,
) -> None:
    conn.execute(
        "INSERT INTO permissions"
        " (rule_shape_id, session_id, project_id, decision, source, decided_at)"
        " VALUES (?, NULL, NULL, ?, 'seed', '2025-01-01Z');",
        (rule_shape_id, decision),
    )
    conn.commit()


def _open_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _db_path_from(conn: sqlite3.Connection) -> Path:
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


# ---------------------------------------------------------------------------
# Tests — flag OFF (default behaviour must not regress)
# ---------------------------------------------------------------------------


class TestNonBashMatchingFlagOff:
    """When non_bash_tool_matching=false, file tools return NoOpinion."""

    def test_write_returns_noop_flag_off(self, tmp_db, tmp_path, monkeypatch):
        cfg = _write_config(tmp_path, non_bash_tool_matching=False)
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg))
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)

        shape_id = _insert_rule_shape(tmp_db, "Write", path_spec=None)
        _insert_permission(tmp_db, shape_id, "approved")

        conn = _open_conn(_db_path_from(tmp_db))
        try:
            v = dispatch("Write", {"file_path": "/tmp/test.py"}, conn, None, None)
        finally:
            conn.close()

        assert v == Verdict.NoOpinion

    def test_edit_returns_noop_flag_off(self, tmp_db, tmp_path, monkeypatch):
        cfg = _write_config(tmp_path, non_bash_tool_matching=False)
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg))
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)

        shape_id = _insert_rule_shape(tmp_db, "Edit", path_spec=None)
        _insert_permission(tmp_db, shape_id, "approved")

        conn = _open_conn(_db_path_from(tmp_db))
        try:
            v = dispatch("Edit", {"file_path": "/tmp/test.py"}, conn, None, None)
        finally:
            conn.close()

        assert v == Verdict.NoOpinion

    def test_explicit_false_config_returns_noop(self, tmp_db, tmp_path, monkeypatch):
        """Explicit non_bash_tool_matching=false config returns NoOpinion for file tools."""
        cfg = _write_config(tmp_path, non_bash_tool_matching=False)
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg))
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)

        shape_id = _insert_rule_shape(tmp_db, "Read", path_spec=None)
        _insert_permission(tmp_db, shape_id, "approved")

        conn = _open_conn(_db_path_from(tmp_db))
        try:
            v = dispatch("Read", {"file_path": "/tmp/test.py"}, conn, None, None)
        finally:
            conn.close()

        assert v == Verdict.NoOpinion


# ---------------------------------------------------------------------------
# Tests — flag ON (file tools promoted to full DB matching)
# ---------------------------------------------------------------------------


class TestNonBashMatchingFlagOn:
    """When non_bash_tool_matching=true, Write/Edit/Read run full DB matching."""

    def test_write_falls_through_to_db_allow(self, tmp_db, tmp_path, monkeypatch):
        cfg = _write_config(tmp_path, non_bash_tool_matching=True)
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg))
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)

        shape_id = _insert_rule_shape(tmp_db, "Write", path_spec=None)
        _insert_permission(tmp_db, shape_id, "approved")

        conn = _open_conn(_db_path_from(tmp_db))
        try:
            v = dispatch("Write", {"file_path": "/tmp/test.py"}, conn, None, None)
        finally:
            conn.close()

        assert v == Verdict.Allow

    def test_edit_falls_through_to_db_allow(self, tmp_db, tmp_path, monkeypatch):
        cfg = _write_config(tmp_path, non_bash_tool_matching=True)
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg))
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)

        shape_id = _insert_rule_shape(tmp_db, "Edit", path_spec=None)
        _insert_permission(tmp_db, shape_id, "approved")

        conn = _open_conn(_db_path_from(tmp_db))
        try:
            v = dispatch("Edit", {"file_path": "/tmp/test.py"}, conn, None, None)
        finally:
            conn.close()

        assert v == Verdict.Allow

    def test_read_falls_through_to_db_allow(self, tmp_db, tmp_path, monkeypatch):
        cfg = _write_config(tmp_path, non_bash_tool_matching=True)
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg))
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)

        shape_id = _insert_rule_shape(tmp_db, "Read", path_spec=None)
        _insert_permission(tmp_db, shape_id, "approved")

        conn = _open_conn(_db_path_from(tmp_db))
        try:
            v = dispatch("Read", {"file_path": "/tmp/test.py"}, conn, None, None)
        finally:
            conn.close()

        assert v == Verdict.Allow

    def test_write_falls_through_to_db_deny(self, tmp_db, tmp_path, monkeypatch):
        """Deny verdict is also reachable — proves DB path ran, not just Allow."""
        cfg = _write_config(tmp_path, non_bash_tool_matching=True)
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg))
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)

        shape_id = _insert_rule_shape(tmp_db, "Write", path_spec="/tmp/test.py")
        _insert_permission(tmp_db, shape_id, "rejected")

        conn = _open_conn(_db_path_from(tmp_db))
        try:
            v = dispatch("Write", {"file_path": "/tmp/test.py"}, conn, None, None)
        finally:
            conn.close()

        assert v == Verdict.Deny

    def test_no_matching_rule_still_returns_noop(self, tmp_db, tmp_path, monkeypatch):
        """Flag on, but no DB rule for the tool — falls through entire DB to NoOpinion."""
        cfg = _write_config(tmp_path, non_bash_tool_matching=True)
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg))
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)

        # No permissions seeded for Write.
        conn = _open_conn(_db_path_from(tmp_db))
        try:
            v = dispatch("Write", {"file_path": "/tmp/test.py"}, conn, None, None)
        finally:
            conn.close()

        assert v == Verdict.NoOpinion


# ---------------------------------------------------------------------------
# Tests — Bash unaffected by the flag
# ---------------------------------------------------------------------------


class TestBashUnaffectedByFlag:
    """Bash always runs full matching regardless of non_bash_tool_matching."""

    def test_bash_allow_when_flag_off(self, tmp_db, tmp_path, monkeypatch):
        cfg = _write_config(tmp_path, non_bash_tool_matching=False)
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg))
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)

        _insert_rule_shape(tmp_db, "git")
        # Need subcommand match — insert with the subcommand form
        conn_setup = tmp_db
        conn_setup.execute(
            "INSERT OR IGNORE INTO rule_shapes"
            " (verb, subcommand, flags, path_spec, first_seen, last_seen)"
            " VALUES ('git', 'status', '[]', NULL, '2025-01-01Z', '2025-01-01Z');",
        )
        conn_setup.commit()
        row = conn_setup.execute(
            "SELECT id FROM rule_shapes WHERE verb='git' AND subcommand='status';"
        ).fetchone()
        if row:
            conn_setup.execute(
                "INSERT INTO permissions"
                " (rule_shape_id, session_id, project_id, decision, source, decided_at)"
                " VALUES (?, NULL, NULL, 'approved', 'seed', '2025-01-01Z');",
                (int(row[0]),),
            )
            conn_setup.commit()

        conn = _open_conn(_db_path_from(tmp_db))
        try:
            v = dispatch("Bash", {"command": "git status"}, conn, None, None)
        finally:
            conn.close()

        assert v == Verdict.Allow

    def test_bash_allow_when_flag_on(self, tmp_db, tmp_path, monkeypatch):
        cfg = _write_config(tmp_path, non_bash_tool_matching=True)
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg))
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)

        tmp_db.execute(
            "INSERT OR IGNORE INTO rule_shapes"
            " (verb, subcommand, flags, path_spec, first_seen, last_seen)"
            " VALUES ('git', 'status', '[]', NULL, '2025-01-01Z', '2025-01-01Z');",
        )
        tmp_db.commit()
        row = tmp_db.execute(
            "SELECT id FROM rule_shapes WHERE verb='git' AND subcommand='status';"
        ).fetchone()
        if row:
            tmp_db.execute(
                "INSERT INTO permissions"
                " (rule_shape_id, session_id, project_id, decision, source, decided_at)"
                " VALUES (?, NULL, NULL, 'approved', 'seed', '2025-01-01Z');",
                (int(row[0]),),
            )
            tmp_db.commit()

        conn = _open_conn(_db_path_from(tmp_db))
        try:
            v = dispatch("Bash", {"command": "git status"}, conn, None, None)
        finally:
            conn.close()

        assert v == Verdict.Allow


# ---------------------------------------------------------------------------
# Tests — non-file-class tools stay NoOpinion when flag is on
# ---------------------------------------------------------------------------


class TestNonFileClassToolsUnaffected:
    """Grep, Glob, Agent, mcp__* still short-circuit to NoOpinion when flag on.

    The flag is NOT a general non-Bash full-match switch.  Only Write/Edit/Read
    are promoted.
    """

    def test_grep_still_noop_flag_on(self, tmp_db, tmp_path, monkeypatch):
        cfg = _write_config(tmp_path, non_bash_tool_matching=True)
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg))
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)

        shape_id = _insert_rule_shape(tmp_db, "Grep")
        _insert_permission(tmp_db, shape_id, "approved")

        conn = _open_conn(_db_path_from(tmp_db))
        try:
            v = dispatch("Grep", {}, conn, None, None)
        finally:
            conn.close()

        assert v == Verdict.NoOpinion

    def test_glob_still_noop_flag_on(self, tmp_db, tmp_path, monkeypatch):
        cfg = _write_config(tmp_path, non_bash_tool_matching=True)
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg))
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)

        shape_id = _insert_rule_shape(tmp_db, "Glob")
        _insert_permission(tmp_db, shape_id, "approved")

        conn = _open_conn(_db_path_from(tmp_db))
        try:
            v = dispatch("Glob", {}, conn, None, None)
        finally:
            conn.close()

        assert v == Verdict.NoOpinion

    def test_mcp_tool_still_noop_flag_on(self, tmp_db, tmp_path, monkeypatch):
        cfg = _write_config(tmp_path, non_bash_tool_matching=True)
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg))
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)

        shape_id = _insert_rule_shape(tmp_db, "mcp__ns__tool")
        _insert_permission(tmp_db, shape_id, "approved")

        conn = _open_conn(_db_path_from(tmp_db))
        try:
            v = dispatch("mcp__ns__tool", {}, conn, None, None)
        finally:
            conn.close()

        assert v == Verdict.NoOpinion

    def test_orchestration_tool_still_noop_flag_on(self, tmp_db, tmp_path, monkeypatch):
        cfg = _write_config(tmp_path, non_bash_tool_matching=True)
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg))
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)

        conn = _open_conn(_db_path_from(tmp_db))
        try:
            v = dispatch("Agent", {}, conn, None, None)
        finally:
            conn.close()

        assert v == Verdict.NoOpinion


# ---------------------------------------------------------------------------
# Tests — MultiEdit and NotebookEdit are file-class but NOT promoted
# ---------------------------------------------------------------------------


class TestFileClassNotInPromotedSet:
    """MultiEdit and NotebookEdit classify as 'file' but are not in _FILE_CLASS_TOOLS.

    The non_bash_tool_matching flag promotes only Write/Edit/Read, not the
    full file class.
    """

    def test_multiedit_still_noop_flag_on(self, tmp_db, tmp_path, monkeypatch):
        cfg = _write_config(tmp_path, non_bash_tool_matching=True)
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg))
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)

        shape_id = _insert_rule_shape(tmp_db, "MultiEdit", path_spec=None)
        _insert_permission(tmp_db, shape_id, "approved")

        conn = _open_conn(_db_path_from(tmp_db))
        try:
            v = dispatch("MultiEdit", {"file_path": "/tmp/test.py"}, conn, None, None)
        finally:
            conn.close()

        assert v == Verdict.NoOpinion

    def test_notebookedit_still_noop_flag_on(self, tmp_db, tmp_path, monkeypatch):
        cfg = _write_config(tmp_path, non_bash_tool_matching=True)
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg))
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)

        shape_id = _insert_rule_shape(tmp_db, "NotebookEdit", path_spec=None)
        _insert_permission(tmp_db, shape_id, "approved")

        conn = _open_conn(_db_path_from(tmp_db))
        try:
            v = dispatch(
                "NotebookEdit", {"file_path": "/tmp/test.ipynb"}, conn, None, None
            )
        finally:
            conn.close()

        assert v == Verdict.NoOpinion
