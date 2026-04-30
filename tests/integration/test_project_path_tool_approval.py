"""Integration tests: $TRUSTED_DIR path-spec resolution in match/file.py.

Verifies that file-tool calls are matched against DB rules containing
$TRUSTED_DIR path-specs, and that the trusted-dir scope qualifier resolves
correctly at match time.  Rules must exist in the DB — there is no code-path
bypass that grants Allow without a DB rule.

Also covers verb-group expansion: rules stored with a group name (e.g.
"Reading", "Full Access") match multiple tool names.
"""

from __future__ import annotations

import sqlite3
import textwrap
from collections.abc import Generator
from pathlib import Path

import pytest

from nephoscope.config import get_config
from nephoscope.learners.permission.match._types import Verdict
from nephoscope.learners.permission.match.file import match
from nephoscope.lib.db import insert_permission, upsert_rule_shape


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_CTX: dict[str, str] = {
    "home": "/home/testuser",
    "cwd": "/home/testuser/project",
    "project_root": "/home/testuser/project",
}

_TS = "2024-01-01T00:00:00Z"


def _write_config(tmp_path: Path, workspace_roots: list[str]) -> Path:
    """Write a minimal TOML config with the given workspace_roots."""
    roots_toml = "[" + ", ".join(f'"{r}"' for r in workspace_roots) + "]"
    content = textwrap.dedent(f"""\
        trusted_dirs = {roots_toml}
        non_bash_tool_matching = true
    """)
    cfg_path = tmp_path / "nephoscope-config.toml"
    cfg_path.write_text(content)
    return cfg_path


@pytest.fixture(autouse=True)
def _config_isolation(monkeypatch, tmp_path):
    """Isolate get_config() cache between tests."""
    get_config.cache_clear()
    yield
    get_config.cache_clear()


@pytest.fixture()
def empty_db(tmp_path, monkeypatch) -> Generator[sqlite3.Connection, None, None]:
    """Isolated DB with schema applied, no rule_shapes or permissions rows."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

    src = Path(__file__).resolve().parent.parent.parent
    schema_sql = (src / "src" / "nephoscope" / "lib" / "schema.sql").read_text()
    conn = sqlite3.connect(str(db_path))
    conn.executescript(schema_sql)
    conn.execute(
        "INSERT OR IGNORE INTO permission_modes (name)"
        " VALUES ('default'),('acceptEdits'),('bypassPermissions'),('plan'),('auto')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO call_statuses (name)"
        " VALUES ('pending'),('ok'),('err'),('denied'),('orphan')"
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helper: set config + seed a $TRUSTED_DIR/** allow rule
# ---------------------------------------------------------------------------


def _configure(monkeypatch, tmp_path: Path, workspace_roots: list[str]) -> None:
    """Point NEPHOSCOPE_CONFIG at a fresh TOML and clear the cache."""
    cfg_path = _write_config(tmp_path, workspace_roots)
    monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg_path))
    get_config.cache_clear()


def _seed_allow(conn: sqlite3.Connection, verb: str) -> None:
    """Seed a global Allow rule for $TRUSTED_DIR/** with the given verb."""
    shape_id = upsert_rule_shape(conn, verb, None, "[]", "$TRUSTED_DIR/**", _TS)
    insert_permission(conn, shape_id, None, None, "approved", "seed", _TS)
    conn.commit()


# ---------------------------------------------------------------------------
# Tests: Literal-verb rules for trusted_dir paths
# ---------------------------------------------------------------------------


class TestLiteralVerbTrustedDirRules:
    def test_write_inside_workspace_root_returns_allow(
        self, monkeypatch, tmp_path, empty_db
    ):
        """Write tool with path inside workspace root returns Allow (DB rule present)."""
        _configure(monkeypatch, tmp_path, ["/tmp/wsroot"])
        _seed_allow(empty_db, "Write")

        result, _ = match(
            tool_name="Write",
            tool_input={"path": "/tmp/wsroot/src/main.py"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        assert result == Verdict.Allow

    def test_edit_inside_workspace_root_returns_allow(
        self, monkeypatch, tmp_path, empty_db
    ):
        """Edit tool with path inside workspace root returns Allow."""
        _configure(monkeypatch, tmp_path, ["/tmp/wsroot"])
        _seed_allow(empty_db, "Edit")

        result, _ = match(
            tool_name="Edit",
            tool_input={"path": "/tmp/wsroot/src/app.py"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        assert result == Verdict.Allow

    def test_read_inside_workspace_root_returns_allow(
        self, monkeypatch, tmp_path, empty_db
    ):
        """Read tool with file_path inside workspace root returns Allow."""
        _configure(monkeypatch, tmp_path, ["/tmp/wsroot"])
        _seed_allow(empty_db, "Read")

        result, _ = match(
            tool_name="Read",
            tool_input={"file_path": "/tmp/wsroot/docs/README.md"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        assert result == Verdict.Allow


# ---------------------------------------------------------------------------
# Tests: Path outside workspace root falls through to DB
# ---------------------------------------------------------------------------


class TestPathOutsideWorkspaceRoot:
    def test_path_outside_workspace_root_falls_through_to_db(
        self, monkeypatch, tmp_path, empty_db
    ):
        """Path outside workspace root falls through to DB (NoOpinion when no DB rule)."""
        _configure(monkeypatch, tmp_path, ["/tmp/wsroot"])
        _seed_allow(empty_db, "Write")

        result, _ = match(
            tool_name="Write",
            tool_input={"path": "/tmp/otherplace/file.py"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        # Path outside trusted dir → $TRUSTED_DIR/** rule does not match
        assert result == Verdict.NoOpinion


# ---------------------------------------------------------------------------
# Tests: Nested path inside workspace root
# ---------------------------------------------------------------------------


class TestNestedPathInsideWorkspaceRoot:
    def test_deeply_nested_path_inside_workspace_root_returns_allow(
        self, monkeypatch, tmp_path, empty_db
    ):
        """Deeply nested path inside workspace root returns Allow."""
        _configure(monkeypatch, tmp_path, ["/tmp/wsroot"])
        _seed_allow(empty_db, "Write")

        result, _ = match(
            tool_name="Write",
            tool_input={"path": "/tmp/wsroot/a/b/c/deeply/nested/file.py"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        assert result == Verdict.Allow


# ---------------------------------------------------------------------------
# Tests: Empty workspace_roots — no $TRUSTED_DIR rule fires
# ---------------------------------------------------------------------------


class TestEmptyWorkspaceRoots:
    def test_empty_workspace_roots_falls_through_to_db(
        self, monkeypatch, tmp_path, empty_db
    ):
        """Empty trusted_dirs means $TRUSTED_DIR path-specs match nothing."""
        _configure(monkeypatch, tmp_path, [])
        _seed_allow(empty_db, "Write")

        result, _ = match(
            tool_name="Write",
            tool_input={"path": "/tmp/wsroot/src/main.py"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        # $TRUSTED_DIR/** rule present but trusted_dirs=[] → no match
        assert result == Verdict.NoOpinion


# ---------------------------------------------------------------------------
# Tests: Multiple workspace roots
# ---------------------------------------------------------------------------


class TestMultipleWorkspaceRoots:
    def test_second_workspace_root_matches(self, monkeypatch, tmp_path, empty_db):
        """When multiple workspace roots configured, a path in the second root returns Allow."""
        _configure(monkeypatch, tmp_path, ["/tmp/ws1", "/tmp/ws2"])
        _seed_allow(empty_db, "Write")

        result, _ = match(
            tool_name="Write",
            tool_input={"path": "/tmp/ws2/project/file.py"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        assert result == Verdict.Allow

    def test_first_workspace_root_matches(self, monkeypatch, tmp_path, empty_db):
        """When multiple workspace roots configured, a path in the first root returns Allow."""
        _configure(monkeypatch, tmp_path, ["/tmp/ws1", "/tmp/ws2"])
        _seed_allow(empty_db, "Write")

        result, _ = match(
            tool_name="Write",
            tool_input={"path": "/tmp/ws1/project/file.py"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        assert result == Verdict.Allow


# ---------------------------------------------------------------------------
# Tests: Missing target path key
# ---------------------------------------------------------------------------


class TestMissingTargetPath:
    def test_tool_input_without_path_key_does_not_allow(
        self, monkeypatch, tmp_path, empty_db
    ):
        """tool_input with no path/file_path key must not return Allow."""
        _configure(monkeypatch, tmp_path, ["/tmp/wsroot"])
        _seed_allow(empty_db, "Write")

        # No 'path' or 'file_path' key — empty target must not match any root
        result, _ = match(
            tool_name="Write",
            tool_input={"content": "hello"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        assert result != Verdict.Allow


# ---------------------------------------------------------------------------
# Tests: Verb-group expansion (Task 4)
# ---------------------------------------------------------------------------


class TestVerbGroupExpansion:
    def test_reading_group_rule_matches_read_tool(
        self, monkeypatch, tmp_path, empty_db
    ):
        """A rule stored with verb='Reading' matches a Read tool call."""
        _configure(monkeypatch, tmp_path, ["/tmp/wsroot"])
        _seed_allow(empty_db, "Reading")

        result, _ = match(
            tool_name="Read",
            tool_input={"file_path": "/tmp/wsroot/file.py"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        assert result == Verdict.Allow

    def test_full_access_group_rule_matches_read(self, monkeypatch, tmp_path, empty_db):
        """A rule stored with verb='Full Access' matches a Read tool call."""
        _configure(monkeypatch, tmp_path, ["/tmp/wsroot"])
        _seed_allow(empty_db, "Full Access")

        result, _ = match(
            tool_name="Read",
            tool_input={"file_path": "/tmp/wsroot/file.py"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        assert result == Verdict.Allow

    def test_full_access_group_rule_matches_write(
        self, monkeypatch, tmp_path, empty_db
    ):
        """A rule stored with verb='Full Access' matches a Write tool call."""
        _configure(monkeypatch, tmp_path, ["/tmp/wsroot"])
        _seed_allow(empty_db, "Full Access")

        result, _ = match(
            tool_name="Write",
            tool_input={"path": "/tmp/wsroot/file.py"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        assert result == Verdict.Allow

    def test_full_access_group_rule_matches_edit(self, monkeypatch, tmp_path, empty_db):
        """A rule stored with verb='Full Access' matches an Edit tool call."""
        _configure(monkeypatch, tmp_path, ["/tmp/wsroot"])
        _seed_allow(empty_db, "Full Access")

        result, _ = match(
            tool_name="Edit",
            tool_input={"path": "/tmp/wsroot/file.py"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        assert result == Verdict.Allow

    def test_literal_verb_rule_still_matches_after_group_expansion(
        self, monkeypatch, tmp_path, empty_db
    ):
        """A rule with verb='Read' (literal) still matches a Read tool call."""
        _configure(monkeypatch, tmp_path, ["/tmp/wsroot"])
        _seed_allow(empty_db, "Read")

        result, _ = match(
            tool_name="Read",
            tool_input={"file_path": "/tmp/wsroot/file.py"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        assert result == Verdict.Allow
