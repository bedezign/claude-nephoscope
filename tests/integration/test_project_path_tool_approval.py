"""Integration tests: workspace-root containment check in match/file.py.

Verifies that a tool's target path falling under any configured workspace_root
returns Verdict.Allow without needing a DB rule entry.  The workspace-root check
runs before the DB lookup, so an empty permissions DB is sufficient to prove
the shortcut is taken.
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


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_CTX: dict[str, str] = {
    "home": "/home/testuser",
    "cwd": "/home/testuser/project",
    "project_root": "/home/testuser/project",
}


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
# Helper: set config + empty DB fixture together
# ---------------------------------------------------------------------------


def _configure(monkeypatch, tmp_path: Path, workspace_roots: list[str]) -> None:
    """Point NEPHOSCOPE_CONFIG at a fresh TOML and clear the cache."""
    cfg_path = _write_config(tmp_path, workspace_roots)
    monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg_path))
    get_config.cache_clear()


# ---------------------------------------------------------------------------
# Tests: Write inside workspace root
# ---------------------------------------------------------------------------


class TestWriteInsideWorkspaceRoot:
    def test_write_inside_workspace_root_returns_allow(
        self, monkeypatch, tmp_path, empty_db
    ):
        """Write tool with path inside workspace root returns Allow (no DB rule needed)."""
        _configure(monkeypatch, tmp_path, ["/tmp/wsroot"])

        result = match(
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

        result = match(
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

        result = match(
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

        result = match(
            tool_name="Write",
            tool_input={"path": "/tmp/otherplace/file.py"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        # No DB rule → NoOpinion; workspace root not involved
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

        result = match(
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
# Tests: Realpath traversal is blocked
# ---------------------------------------------------------------------------


class TestRealpathTraversal:
    def test_dotdot_traversal_resolves_outside_and_does_not_allow(
        self, monkeypatch, tmp_path, empty_db
    ):
        """Path using .. that resolves outside workspace root is not workspace-allowed."""
        _configure(monkeypatch, tmp_path, ["/tmp/wsroot"])

        # /tmp/wsroot/../otherplace/file.py resolves to /tmp/otherplace/file.py
        result = match(
            tool_name="Write",
            tool_input={"path": "/tmp/wsroot/../otherplace/file.py"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        # realpath resolves to /tmp/otherplace/file.py — not under /tmp/wsroot
        assert result != Verdict.Allow


# ---------------------------------------------------------------------------
# Tests: Exact workspace root path is allowed
# ---------------------------------------------------------------------------


class TestExactWorkspaceRootPath:
    def test_exact_workspace_root_path_returns_allow(
        self, monkeypatch, tmp_path, empty_db
    ):
        """A path equal to the workspace root itself (no trailing slash) returns Allow."""
        _configure(monkeypatch, tmp_path, ["/tmp/wsroot"])

        result = match(
            tool_name="Read",
            tool_input={"file_path": "/tmp/wsroot"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        assert result == Verdict.Allow


# ---------------------------------------------------------------------------
# Tests: Empty workspace_roots skips the check
# ---------------------------------------------------------------------------


class TestEmptyWorkspaceRoots:
    def test_empty_workspace_roots_falls_through_to_db(
        self, monkeypatch, tmp_path, empty_db
    ):
        """Empty workspace_roots list skips the workspace check; falls through to DB."""
        _configure(monkeypatch, tmp_path, [])

        result = match(
            tool_name="Write",
            tool_input={"path": "/tmp/wsroot/src/main.py"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        # No DB rule, workspace check skipped → NoOpinion
        assert result == Verdict.NoOpinion


# ---------------------------------------------------------------------------
# Tests: Multiple workspace roots
# ---------------------------------------------------------------------------


class TestMultipleWorkspaceRoots:
    def test_second_workspace_root_matches(self, monkeypatch, tmp_path, empty_db):
        """When multiple workspace roots configured, a path in the second root returns Allow."""
        _configure(monkeypatch, tmp_path, ["/tmp/ws1", "/tmp/ws2"])

        result = match(
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

        result = match(
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
    def test_tool_input_without_path_key_does_not_workspace_allow(
        self, monkeypatch, tmp_path, empty_db
    ):
        """tool_input with no path/file_path key must not trigger workspace-root Allow."""
        _configure(monkeypatch, tmp_path, ["/tmp/wsroot"])

        # No 'path' or 'file_path' key — empty target must not match any root
        result = match(
            tool_name="Write",
            tool_input={"content": "hello"},
            conn=empty_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
            trusted_dirs=get_config().trusted_dirs,
        )
        # Empty target realpath resolves to cwd, which may or may not be
        # under /tmp/wsroot; the guard must ensure empty target → no workspace Allow.
        assert result != Verdict.Allow
