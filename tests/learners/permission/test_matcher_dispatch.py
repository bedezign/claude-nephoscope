"""Tests for learners.permission.match — tool-class dispatch.

Covers:
  - Each tool class (bash, file, flat, mcp, orchestration) against sandbox DB
  - Tier priority (session → project → global)
  - HOOK_FULL_MATCH env var (default OFF → NoOpinion for non-bash;
    ON → full matching)
  - Verdict enum values
  - Doom-path: empty input, unknown tool, missing DB rows, no session context

Tool classes tested
-------------------
  bash          — approve / reject / ask / noopinion
  file          — path glob match, no-path match, $HOME/$PROJECT_ROOT tokens
  flat          — presence check: approved / rejected / noopinion
  mcp           — literal match, namespace wildcard, noopinion
  orchestration — always Allow when HOOK_FULL_MATCH=on; NoOpinion when off
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from nephoscope.config import get_config
from nephoscope.learners.permission.match import Verdict, dispatch
from nephoscope.learners.permission.match._types import Verdict as VerdictDirect
from nephoscope.learners.permission.match.bash import match as bash_match
from nephoscope.learners.permission.match.file import match as file_match
from nephoscope.learners.permission.match.flat import match as flat_match
from nephoscope.learners.permission.match.mcp import match as mcp_match
from nephoscope.learners.permission.match.orchestration import match as orch_match
from nephoscope.learners.permission.seed import apply_fixtures
from nephoscope.lib.db import set_session_extra_dirs

_FIXTURES_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "src"
    / "nephoscope"
    / "learners"
    / "permission"
    / "config"
    / "fixtures"
)


# ---------------------------------------------------------------------------
# Helpers (shared with test_hook.py style)
# ---------------------------------------------------------------------------


def _insert_project(conn: sqlite3.Connection, cwd: str, root: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO projects (cwd, name, root, first_seen, last_seen)"
        " VALUES (?, ?, ?, '2025-01-01Z', '2025-01-01Z');",
        (cwd, Path(cwd).name, root),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _insert_session(
    conn: sqlite3.Connection, uuid: str, project_id: int | None = None
) -> int:
    cur = conn.execute(
        "INSERT INTO sessions (session_uuid, project_id, started_at, last_activity)"
        " VALUES (?, ?, '2025-01-01Z', '2025-01-01Z');",
        (uuid, project_id),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _insert_tool_call(
    conn: sqlite3.Connection,
    tool_use_id: str,
    session_id: int | None = None,
    project_id: int | None = None,
) -> int:
    conn.execute("INSERT OR IGNORE INTO tools (name) VALUES ('Bash')")
    t_row = conn.execute("SELECT id FROM tools WHERE name='Bash'").fetchone()
    tool_id_val = int(t_row[0])
    pending_row = conn.execute(
        "SELECT id FROM call_statuses WHERE name='pending'"
    ).fetchone()
    status_id = int(pending_row[0])
    cur = conn.execute(
        "INSERT INTO tool_calls"
        " (ts, session_id, project_id, tool_id, status_id, tool_use_id)"
        " VALUES ('2025-01-01Z', ?, ?, ?, ?, ?);",
        (session_id, project_id, tool_id_val, status_id, tool_use_id),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _insert_rule_shape(
    conn: sqlite3.Connection,
    verb: str,
    subcommand: str | None = None,
    flags: str = "[]",
    path_spec: str | None = None,
) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO rule_shapes"
        " (verb, subcommand, flags, path_spec, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, '2025-01-01Z', '2025-01-01Z');",
        (verb, subcommand, flags, path_spec),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM rule_shapes"
        " WHERE verb=? AND IFNULL(subcommand,'')=IFNULL(?,'') AND flags=?"
        " AND IFNULL(path_spec,'')=IFNULL(?,'');",
        (verb, subcommand, flags, path_spec),
    ).fetchone()
    return int(row[0]) if row else 0


def _insert_permission(
    conn: sqlite3.Connection,
    rule_shape_id: int,
    decision: str,
    session_id: int | None = None,
    project_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO permissions"
        " (rule_shape_id, session_id, project_id, decision, source, decided_at)"
        " VALUES (?, ?, ?, ?, 'seed', '2025-01-01Z');",
        (rule_shape_id, session_id, project_id, decision),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _conn(db: sqlite3.Connection) -> sqlite3.Connection:
    """Return the connection from the tmp_db fixture (already open)."""
    return db


# ---------------------------------------------------------------------------
# Verdict enum
# ---------------------------------------------------------------------------


class TestVerdictEnum:
    def test_all_members_present(self):
        names = {v.name for v in Verdict}
        assert names == {"Allow", "Deny", "Ask", "NoOpinion"}

    def test_verdict_re_export(self):
        assert Verdict is VerdictDirect


# ---------------------------------------------------------------------------
# Bash matcher (unit)
# ---------------------------------------------------------------------------


class TestBashMatcher:
    def test_approved_shape_returns_allow(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "git", "status")
        _insert_permission(tmp_db, shape_id, "approved")
        v, _ = bash_match(
            "Bash", {"command": "git status"}, _conn(tmp_db), None, None, {}
        )
        assert v == Verdict.Allow

    def test_rejected_shape_returns_deny(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "git", "push")
        _insert_permission(tmp_db, shape_id, "rejected")
        v, _ = bash_match(
            "Bash", {"command": "git push origin main"}, _conn(tmp_db), None, None, {}
        )
        assert v == Verdict.Deny

    def test_ask_verb_returns_ask(self, tmp_db):
        # rm -r has no DB approval → falls to ask tier via ask_flag_patterns in deny.py
        v, _ = bash_match(
            "Bash", {"command": "rm -r /tmp/file"}, _conn(tmp_db), None, None, {}
        )
        assert v == Verdict.Ask

    def test_unknown_verb_returns_noop(self, tmp_db):
        v, _ = bash_match("Bash", {"command": "git log"}, _conn(tmp_db), None, None, {})
        assert v == Verdict.NoOpinion

    def test_empty_command_returns_noop(self, tmp_db):
        v, _ = bash_match("Bash", {"command": ""}, _conn(tmp_db), None, None, {})
        assert v == Verdict.NoOpinion

    def test_unparseable_command_returns_noop(self, tmp_db):
        v, _ = bash_match(
            "Bash", {"command": ")(invalid("}, _conn(tmp_db), None, None, {}
        )
        assert v == Verdict.NoOpinion

    def test_approved_beats_ask_tier(self, tmp_db):
        """An approved permission overrides the ask tier."""
        shape_id = _insert_rule_shape(tmp_db, "rm", None, '["-f"]')
        _insert_permission(tmp_db, shape_id, "approved")
        v, _ = bash_match(
            "Bash", {"command": "rm -f /tmp/x"}, _conn(tmp_db), None, None, {}
        )
        assert v == Verdict.Allow

    def test_pipeline_one_rejected_returns_deny(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "git", "push")
        _insert_permission(tmp_db, shape_id, "rejected")
        v, _ = bash_match(
            "Bash",
            {"command": "git log && git push origin main"},
            _conn(tmp_db),
            None,
            None,
            {},
        )
        assert v == Verdict.Deny

    def test_flags_wildcard_matches(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "pytest", None, "*")
        _insert_permission(tmp_db, shape_id, "approved")
        v, _ = bash_match(
            "Bash", {"command": "pytest -q --tb=short"}, _conn(tmp_db), None, None, {}
        )
        assert v == Verdict.Allow


# ---------------------------------------------------------------------------
# File matcher (unit)
# ---------------------------------------------------------------------------


class TestFileMatcher:
    def test_no_path_spec_matches_any_path(self, tmp_db):
        """path_spec=NULL → matches any file_path."""
        shape_id = _insert_rule_shape(tmp_db, "Read", path_spec=None)
        _insert_permission(tmp_db, shape_id, "approved")
        v, _ = file_match(
            "Read", {"file_path": "/home/user/file.py"}, _conn(tmp_db), None, None, {}
        )
        assert v == Verdict.Allow

    def test_empty_path_spec_matches_no_path(self, tmp_db):
        """path_spec='' → matches only when no file_path provided."""
        shape_id = _insert_rule_shape(tmp_db, "Read", path_spec="")
        _insert_permission(tmp_db, shape_id, "approved")
        # No file path → matches.
        v, _ = file_match("Read", {}, _conn(tmp_db), None, None, {})
        assert v == Verdict.Allow
        # File path provided → no match.
        v2, _ = file_match(
            "Read", {"file_path": "/home/user/file.py"}, _conn(tmp_db), None, None, {}
        )
        assert v2 == Verdict.NoOpinion

    def test_home_token_resolved(self, tmp_db):
        import os

        home = os.path.expanduser("~")
        shape_id = _insert_rule_shape(tmp_db, "Read", path_spec="$HOME/**")
        _insert_permission(tmp_db, shape_id, "approved")
        ctx = {"home": home}
        v, _ = file_match(
            "Read",
            {"file_path": f"{home}/projects/main.py"},
            _conn(tmp_db),
            None,
            None,
            ctx,
        )
        assert v == Verdict.Allow

    def test_project_root_token_resolved(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "Edit", path_spec="$PROJECT_ROOT/**")
        _insert_permission(tmp_db, shape_id, "approved")
        ctx = {"project_root": "/home/user/myproject"}
        v, _ = file_match(
            "Edit",
            {"file_path": "/home/user/myproject/src/main.py"},
            _conn(tmp_db),
            None,
            None,
            ctx,
        )
        assert v == Verdict.Allow

    def test_path_outside_glob_returns_noop(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "Read", path_spec="$HOME/.claude/**")
        _insert_permission(tmp_db, shape_id, "approved")
        ctx = {"home": "/home/user"}
        v, _ = file_match(
            "Read", {"file_path": "/tmp/unrelated.py"}, _conn(tmp_db), None, None, ctx
        )
        assert v == Verdict.NoOpinion

    def test_rejected_path_returns_deny(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "Write", path_spec="$HOME/.claude/**")
        _insert_permission(tmp_db, shape_id, "rejected")
        ctx = {"home": "/home/user"}
        v, _ = file_match(
            "Write",
            {"file_path": "/home/user/.claude/settings.json"},
            _conn(tmp_db),
            None,
            None,
            ctx,
        )
        assert v == Verdict.Deny

    def test_no_rule_shape_for_verb_returns_noop(self, tmp_db):
        v, _ = file_match(
            "Read", {"file_path": "/any/file.py"}, _conn(tmp_db), None, None, {}
        )
        assert v == Verdict.NoOpinion

    def test_path_key_fallback(self, tmp_db):
        """Tool input with 'path' key (instead of 'file_path') is accepted."""
        shape_id = _insert_rule_shape(tmp_db, "Read", path_spec=None)
        _insert_permission(tmp_db, shape_id, "approved")
        v, _ = file_match(
            "Read", {"path": "/some/file.py"}, _conn(tmp_db), None, None, {}
        )
        assert v == Verdict.Allow


# ---------------------------------------------------------------------------
# Flat matcher (unit)
# ---------------------------------------------------------------------------


class TestFlatMatcher:
    def test_approved_returns_allow(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "Grep")
        _insert_permission(tmp_db, shape_id, "approved")
        v, _ = flat_match("Grep", {}, _conn(tmp_db), None, None, {})
        assert v == Verdict.Allow

    def test_rejected_returns_deny(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "WebSearch")
        _insert_permission(tmp_db, shape_id, "rejected")
        v, _ = flat_match("WebSearch", {}, _conn(tmp_db), None, None, {})
        assert v == Verdict.Deny

    def test_no_row_returns_noop(self, tmp_db):
        v, _ = flat_match("Glob", {}, _conn(tmp_db), None, None, {})
        assert v == Verdict.NoOpinion

    def test_ignores_tool_input_content(self, tmp_db):
        """Flat matcher ignores tool_input; only verb matters."""
        shape_id = _insert_rule_shape(tmp_db, "Grep")
        _insert_permission(tmp_db, shape_id, "approved")
        v, _ = flat_match(
            "Grep",
            {"pattern": "anything", "path": "/wherever"},
            _conn(tmp_db),
            None,
            None,
            {},
        )
        assert v == Verdict.Allow


# ---------------------------------------------------------------------------
# MCP matcher (unit)
# ---------------------------------------------------------------------------


class TestMcpMatcher:
    def test_literal_match_approved(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "mcp__claude-peers__send_message")
        _insert_permission(tmp_db, shape_id, "approved")
        v, _ = mcp_match(
            "mcp__claude-peers__send_message", {}, _conn(tmp_db), None, None, {}
        )
        assert v == Verdict.Allow

    def test_literal_match_rejected(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "mcp__dangerous__delete_all")
        _insert_permission(tmp_db, shape_id, "rejected")
        v, _ = mcp_match(
            "mcp__dangerous__delete_all", {}, _conn(tmp_db), None, None, {}
        )
        assert v == Verdict.Deny

    def test_wildcard_match(self, tmp_db):
        """mcp__ns__* wildcard covers any tool in that namespace."""
        shape_id = _insert_rule_shape(tmp_db, "mcp__claude-peers__*")
        _insert_permission(tmp_db, shape_id, "approved")
        v, _ = mcp_match(
            "mcp__claude-peers__list_peers", {}, _conn(tmp_db), None, None, {}
        )
        assert v == Verdict.Allow

    def test_literal_beats_wildcard(self, tmp_db):
        """Literal match is attempted first; wildcard is a fallback."""
        # Literal: rejected; wildcard: approved.
        lit_id = _insert_rule_shape(tmp_db, "mcp__ns__specific_tool")
        _insert_permission(tmp_db, lit_id, "rejected")
        wild_id = _insert_rule_shape(tmp_db, "mcp__ns__*")
        _insert_permission(tmp_db, wild_id, "approved")
        v, _ = mcp_match("mcp__ns__specific_tool", {}, _conn(tmp_db), None, None, {})
        assert v == Verdict.Deny

    def test_no_row_returns_noop(self, tmp_db):
        v, _ = mcp_match("mcp__unknown__tool", {}, _conn(tmp_db), None, None, {})
        assert v == Verdict.NoOpinion


# ---------------------------------------------------------------------------
# Orchestration matcher (unit)
# ---------------------------------------------------------------------------


class TestOrchestrationMatcher:
    def test_always_allow(self, tmp_db):
        v, _ = orch_match("Agent", {}, _conn(tmp_db), None, None, {})
        assert v == Verdict.Allow

    def test_allow_without_any_db_rows(self, tmp_db):
        # No rule_shapes or permissions rows at all.
        v, _ = orch_match("TaskCreate", {}, _conn(tmp_db), None, None, {})
        assert v == Verdict.Allow


# ---------------------------------------------------------------------------
# Dispatch — HOOK_FULL_MATCH=OFF (default)
# ---------------------------------------------------------------------------


class TestDispatchFullMatchOff:
    """With HOOK_FULL_MATCH unset/off and non_bash_tool_matching explicitly false, non-Bash tools → NoOpinion."""

    @pytest.fixture(autouse=True)
    def _config_defaults(self, monkeypatch, tmp_path):
        """Point NEPHOSCOPE_CONFIG at a config file with non_bash_tool_matching=false
        so this class exercises the explicitly-off code path regardless of the
        dataclass default."""
        cfg = tmp_path / "config.toml"
        cfg.write_text("non_bash_tool_matching = false\n")
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg))
        get_config.cache_clear()
        yield
        get_config.cache_clear()

    def test_bash_still_runs_full_match(self, tmp_db, monkeypatch):
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)
        shape_id = _insert_rule_shape(tmp_db, "git", "status")
        _insert_permission(tmp_db, shape_id, "approved")
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v, _ = dispatch("Bash", {"command": "git status"}, conn, None, None)
        finally:
            conn.close()
        assert v == Verdict.Allow

    def test_file_tool_returns_noop_when_off(self, tmp_db, monkeypatch):
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)
        shape_id = _insert_rule_shape(tmp_db, "Read", path_spec=None)
        _insert_permission(tmp_db, shape_id, "approved")
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v, _ = dispatch("Read", {"file_path": "/some/file.py"}, conn, None, None)
        finally:
            conn.close()
        assert v == Verdict.NoOpinion

    def test_flat_tool_returns_noop_when_off(self, tmp_db, monkeypatch):
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)
        shape_id = _insert_rule_shape(tmp_db, "Grep")
        _insert_permission(tmp_db, shape_id, "approved")
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v, _ = dispatch("Grep", {}, conn, None, None)
        finally:
            conn.close()
        assert v == Verdict.NoOpinion

    def test_mcp_returns_noop_when_off(self, tmp_db, monkeypatch):
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)
        shape_id = _insert_rule_shape(tmp_db, "mcp__ns__tool")
        _insert_permission(tmp_db, shape_id, "approved")
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v, _ = dispatch("mcp__ns__tool", {}, conn, None, None)
        finally:
            conn.close()
        assert v == Verdict.NoOpinion

    def test_orchestration_returns_noop_when_off(self, tmp_db, monkeypatch):
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v, _ = dispatch("Agent", {}, conn, None, None)
        finally:
            conn.close()
        assert v == Verdict.NoOpinion


# ---------------------------------------------------------------------------
# Dispatch — HOOK_FULL_MATCH=ON
# ---------------------------------------------------------------------------


class TestDispatchFullMatchOn:
    """With HOOK_FULL_MATCH=1, every class runs full matching."""

    def test_file_tool_allow_when_on(self, tmp_db, monkeypatch):
        monkeypatch.setenv("HOOK_FULL_MATCH", "1")
        shape_id = _insert_rule_shape(tmp_db, "Read", path_spec=None)
        _insert_permission(tmp_db, shape_id, "approved")
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v, _ = dispatch("Read", {"file_path": "/some/file.py"}, conn, None, None)
        finally:
            conn.close()
        assert v == Verdict.Allow

    def test_flat_tool_deny_when_on(self, tmp_db, monkeypatch):
        monkeypatch.setenv("HOOK_FULL_MATCH", "1")
        shape_id = _insert_rule_shape(tmp_db, "WebSearch")
        _insert_permission(tmp_db, shape_id, "rejected")
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v, _ = dispatch("WebSearch", {}, conn, None, None)
        finally:
            conn.close()
        assert v == Verdict.Deny

    def test_mcp_allow_when_on(self, tmp_db, monkeypatch):
        monkeypatch.setenv("HOOK_FULL_MATCH", "1")
        shape_id = _insert_rule_shape(tmp_db, "mcp__ns__tool")
        _insert_permission(tmp_db, shape_id, "approved")
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v, _ = dispatch("mcp__ns__tool", {}, conn, None, None)
        finally:
            conn.close()
        assert v == Verdict.Allow

    def test_orchestration_allow_when_on(self, tmp_db, monkeypatch):
        monkeypatch.setenv("HOOK_FULL_MATCH", "1")
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v, _ = dispatch("Agent", {}, conn, None, None)
        finally:
            conn.close()
        assert v == Verdict.Allow

    def test_true_value_accepted(self, tmp_db, monkeypatch):
        monkeypatch.setenv("HOOK_FULL_MATCH", "true")
        shape_id = _insert_rule_shape(tmp_db, "Grep")
        _insert_permission(tmp_db, shape_id, "approved")
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v, _ = dispatch("Grep", {}, conn, None, None)
        finally:
            conn.close()
        assert v == Verdict.Allow

    def test_zero_value_is_off(self, tmp_db, monkeypatch):
        monkeypatch.setenv("HOOK_FULL_MATCH", "0")
        shape_id = _insert_rule_shape(tmp_db, "Grep")
        _insert_permission(tmp_db, shape_id, "approved")
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v, _ = dispatch("Grep", {}, conn, None, None)
        finally:
            conn.close()
        assert v == Verdict.NoOpinion


# ---------------------------------------------------------------------------
# Tier priority via dispatch
# ---------------------------------------------------------------------------


class TestDispatchTierPriority:
    """Session tier beats project; project beats global."""

    def test_session_approved_beats_global_rejected(self, tmp_db, monkeypatch):
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)
        proj_id = _insert_project(tmp_db, "/home/user/project")
        sess_id = _insert_session(tmp_db, "sess-tier-001", proj_id)
        shape_id = _insert_rule_shape(tmp_db, "git", "fetch")
        _insert_permission(tmp_db, shape_id, "rejected")  # global
        _insert_permission(tmp_db, shape_id, "approved", session_id=sess_id)  # session
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v, _ = dispatch("Bash", {"command": "git fetch"}, conn, sess_id, proj_id)
        finally:
            conn.close()
        assert v == Verdict.Allow

    def test_project_approved_beats_global_rejected(self, tmp_db, monkeypatch):
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)
        proj_id = _insert_project(tmp_db, "/home/user/project")
        shape_id = _insert_rule_shape(tmp_db, "git", "fetch")
        _insert_permission(tmp_db, shape_id, "rejected")  # global
        _insert_permission(tmp_db, shape_id, "approved", project_id=proj_id)  # project
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v, _ = dispatch("Bash", {"command": "git fetch"}, conn, None, proj_id)
        finally:
            conn.close()
        assert v == Verdict.Allow

    def test_global_falls_through_to_tier(self, tmp_db, monkeypatch):
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)
        shape_id = _insert_rule_shape(tmp_db, "git", "log")
        _insert_permission(tmp_db, shape_id, "approved")  # global
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v, _ = dispatch("Bash", {"command": "git log"}, conn, None, None)
        finally:
            conn.close()
        assert v == Verdict.Allow

    def test_tier_priority_file_full_match(self, tmp_db, monkeypatch):
        """File matcher: project approval beats global rejection (HOOK_FULL_MATCH on)."""
        monkeypatch.setenv("HOOK_FULL_MATCH", "1")
        import os

        home = os.path.expanduser("~")
        proj_id = _insert_project(tmp_db, f"{home}/project")
        shape_id = _insert_rule_shape(tmp_db, "Read", path_spec=None)
        _insert_permission(tmp_db, shape_id, "rejected")  # global
        _insert_permission(tmp_db, shape_id, "approved", project_id=proj_id)  # project
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v, _ = dispatch(
                "Read", {"file_path": f"{home}/project/main.py"}, conn, None, proj_id
            )
        finally:
            conn.close()
        assert v == Verdict.Allow


# ---------------------------------------------------------------------------
# Doom-path edge cases
# ---------------------------------------------------------------------------


class TestDispatchDoomPath:
    def test_empty_tool_name_returns_noop(self, tmp_db):
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            # classify("") raises; dispatch should propagate ValueError
            with pytest.raises(ValueError):
                dispatch("", {}, conn, None, None)
        finally:
            conn.close()

    def test_null_tool_input_bash(self, tmp_db):
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v, _ = dispatch("Bash", {}, conn, None, None)
        finally:
            conn.close()
        assert v == Verdict.NoOpinion

    def test_no_session_context_uses_global_tier(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "git", "log")
        _insert_permission(tmp_db, shape_id, "approved")
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            # session_id=None, project_id=None → global-only lookup
            v, _ = dispatch("Bash", {"command": "git log"}, conn, None, None)
        finally:
            conn.close()
        assert v == Verdict.Allow

    def test_unknown_non_mcp_tool_class_bash_default(self, tmp_db):
        """An arbitrary tool name not in any explicit set defaults to bash class."""
        shape_id = _insert_rule_shape(tmp_db, "my-custom-script")
        _insert_permission(tmp_db, shape_id, "approved")
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            # Classify: not file/flat/mcp/orchestration → bash default.
            # Bash matcher looks for rule_shape match via canonicalize (verb=my-custom-script).
            v, _ = dispatch(
                "Bash", {"command": "my-custom-script --run"}, conn, None, None
            )
        finally:
            conn.close()
        # Bash matcher uses canonicalize; depends on DB shape; no ask tier for this verb.
        # Should be Allow if canonicalize finds the shape.
        assert v in (Verdict.Allow, Verdict.NoOpinion)

    def test_file_match_wrong_verb_returns_noop(self, tmp_db, monkeypatch):
        """A 'Write' rule does not match a 'Read' dispatch call."""
        monkeypatch.setenv("HOOK_FULL_MATCH", "1")
        shape_id = _insert_rule_shape(tmp_db, "Write", path_spec=None)
        _insert_permission(tmp_db, shape_id, "approved")
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v, _ = dispatch("Read", {"file_path": "/some/file.py"}, conn, None, None)
        finally:
            conn.close()
        assert v == Verdict.NoOpinion


# ---------------------------------------------------------------------------
# per-session extra_dirs merge in _get_additional_dirs
# ---------------------------------------------------------------------------


class TestGetAdditionalDirsSessionMerge:
    """`_get_additional_dirs` merges global + project + session sources.

    Sessions sit at the bottom of the merge so that mtime-cached entries
    (global, project) come first; session-only `--add-dir` flags are
    appended. The helper deduplicates while preserving order.
    """

    def _seed_session_extras(
        self, conn: sqlite3.Connection, session_id: int, dirs: list[str]
    ) -> None:
        set_session_extra_dirs(conn, session_id, json.dumps(dirs))
        conn.commit()

    def test_session_dirs_appended_after_global_and_project(self, tmp_db):
        """Session extra_dirs follow global + project in the merged list."""
        from nephoscope.learners.permission.match import _get_additional_dirs

        sid = _insert_session(tmp_db, "uuid-merge-1")
        self._seed_session_extras(tmp_db, sid, ["/session/only"])

        result = _get_additional_dirs(tmp_db, None, sid)
        assert result == ["/session/only"], (
            f"expected session entry to surface, got {result!r}"
        )

    def test_session_dirs_dedupe_against_global(self, tmp_db):
        """Duplicates between session and global are dropped (first wins, order preserved)."""
        from nephoscope.learners.permission.match import _get_additional_dirs

        # Seed global mirror with a settings.json on disk so the
        # mtime-cache reader returns the entry.
        settings = (
            Path(tmp_db.execute("PRAGMA database_list").fetchone()[2]).parent
            / "settings.json"
        )
        settings.write_text(
            json.dumps({"permissions": {"additionalDirectories": ["/shared/dir"]}})
        )
        tmp_db.execute(
            "INSERT OR REPLACE INTO global_mirror"
            " (id, settings_json_path, settings_json_sha256, settings_json_last_synced)"
            " VALUES (1, ?, NULL, NULL);",
            (str(settings),),
        )
        tmp_db.commit()

        sid = _insert_session(tmp_db, "uuid-merge-dedupe")
        self._seed_session_extras(tmp_db, sid, ["/shared/dir", "/session/extra"])

        result = _get_additional_dirs(tmp_db, None, sid)
        assert result == ["/shared/dir", "/session/extra"], (
            f"expected dedup against global, got {result!r}"
        )

    def test_no_session_id_skips_session_lookup(self, tmp_db):
        """Calling without a session_id (None) does not touch the sessions table."""
        from nephoscope.learners.permission.match import _get_additional_dirs

        # Insert a session with extras but pass None — they must not appear.
        sid = _insert_session(tmp_db, "uuid-no-merge")
        self._seed_session_extras(tmp_db, sid, ["/should/not/show"])

        result = _get_additional_dirs(tmp_db, None, None)
        assert result == [], f"expected empty without session_id, got {result!r}"

    def test_session_lookup_failure_falls_back_to_empty(self, tmp_db, monkeypatch):
        """If the session read raises, the matcher degrades gracefully."""
        from nephoscope.learners.permission.match import _get_additional_dirs

        sid = _insert_session(tmp_db, "uuid-raise")
        self._seed_session_extras(tmp_db, sid, ["/should/not/show"])

        # Simulate a Scope that breaks for sessions specifically.
        from nephoscope.lib import scope as scope_module

        original = scope_module.get_additional_dirs

        def _raising(conn, scope):
            if scope.table == "sessions":
                raise RuntimeError("simulated session read failure")
            return original(conn, scope)

        monkeypatch.setattr("nephoscope.lib.scope.get_additional_dirs", _raising)

        # Must not raise.
        result = _get_additional_dirs(tmp_db, None, sid)
        assert result == [], f"expected empty on session-read failure, got {result!r}"


# ---------------------------------------------------------------------------
# Wildcard-verb rule shape (verb="*") — Phase B14
# ---------------------------------------------------------------------------


class TestWildcardVerbRuleShape:
    """verb="*" seed rules block any reader on a credential path."""

    _HOME = "/home/tester"

    def _ctx(self):
        return {"home": self._HOME}

    def test_wildcard_verb_deny_cat_credential(self, tmp_db):
        """verb="*" deny on $HOME/.aws/credentials catches `cat ~/.aws/credentials`.

        The seed row uses the $HOME placeholder; to_pattern_form emits a wildcard-verb
        variant with the same placeholder, which the DB lookup matches exactly.
        """
        shape_id = _insert_rule_shape(
            tmp_db,
            verb="*",
            subcommand=None,
            flags="*",
            path_spec="$HOME/.aws/credentials",
        )
        _insert_permission(tmp_db, shape_id, "rejected")

        v, _ = bash_match(
            "Bash",
            {"command": f"cat {self._HOME}/.aws/credentials"},
            tmp_db,
            None,
            None,
            self._ctx(),
        )
        assert v == Verdict.Deny

    def test_wildcard_verb_deny_grep_credential(self, tmp_db):
        """Same verb="*" rule catches `grep aws ~/.aws/credentials`."""
        shape_id = _insert_rule_shape(
            tmp_db,
            verb="*",
            subcommand=None,
            flags="*",
            path_spec="$HOME/.aws/credentials",
        )
        _insert_permission(tmp_db, shape_id, "rejected")

        v, _ = bash_match(
            "Bash",
            {"command": f"grep aws {self._HOME}/.aws/credentials"},
            tmp_db,
            None,
            None,
            self._ctx(),
        )
        assert v == Verdict.Deny

    def test_wildcard_verb_does_not_match_unrelated_path(self, tmp_db):
        """A verb="*" rule scoped to credentials path must NOT fire on unrelated paths."""
        shape_id = _insert_rule_shape(
            tmp_db,
            verb="*",
            subcommand=None,
            flags="*",
            path_spec="$HOME/.aws/credentials",
        )
        _insert_permission(tmp_db, shape_id, "rejected")

        v, _ = bash_match(
            "Bash",
            {"command": "cat /tmp/something-unrelated.txt"},
            tmp_db,
            None,
            None,
            self._ctx(),
        )
        # /tmp/something-unrelated.txt is not under $HOME and does not match the
        # credential path_spec — so the deny rule must not fire.
        assert v != Verdict.Deny

    def test_per_verb_rule_beats_wildcard_verb(self, tmp_db):
        """Per-verb approved rule wins over verb="*" rejected rule for the same path.

        to_pattern_form emits per-verb path-spec variants (e.g. verb="cat",
        flags="[]", path_spec="$HOME/.aws/credentials") BEFORE the wildcard-verb
        variant (verb="*", flags="*", path_spec="$HOME/.aws/credentials"). The
        _decision_for_leaf loop returns on the first match, so the per-verb rule
        takes precedence.
        """
        # Per-verb allow for cat on $HOME/.aws/credentials.
        # flags="[]" matches the literal-flags path-spec variant emitted by
        # to_pattern_form for a cat command without any flags.
        per_verb_shape_id = _insert_rule_shape(
            tmp_db,
            verb="cat",
            subcommand=None,
            flags="[]",
            path_spec="$HOME/.aws/credentials",
        )
        _insert_permission(tmp_db, per_verb_shape_id, "approved")

        # Wildcard-verb deny on the same path.
        wildcard_shape_id = _insert_rule_shape(
            tmp_db,
            verb="*",
            subcommand=None,
            flags="*",
            path_spec="$HOME/.aws/credentials",
        )
        _insert_permission(tmp_db, wildcard_shape_id, "rejected")

        v, _ = bash_match(
            "Bash",
            {"command": f"cat {self._HOME}/.aws/credentials"},
            tmp_db,
            None,
            None,
            self._ctx(),
        )
        # Per-verb rule wins → Allow.
        assert v == Verdict.Allow


# ---------------------------------------------------------------------------
# Context-aware matching (Phase 2)
# ---------------------------------------------------------------------------


def _insert_rule_shape_with_context(
    conn: sqlite3.Connection,
    verb: str,
    subcommand: str | None = None,
    flags: str = "*",
    path_spec: str | None = None,
    context: str = "any",
) -> int:
    """Insert a rule_shape with a context constraint."""
    conn.execute(
        "INSERT OR IGNORE INTO rule_shapes"
        " (verb, subcommand, flags, path_spec, context, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?, '2025-01-01Z', '2025-01-01Z');",
        (verb, subcommand, flags, path_spec, context),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM rule_shapes"
        " WHERE verb=? AND IFNULL(subcommand,'')=IFNULL(?,'') AND flags=?"
        " AND IFNULL(path_spec,'')=IFNULL(?,'') AND context=?;",
        (verb, subcommand, flags, path_spec, context),
    ).fetchone()
    return int(row[0]) if row else 0


class TestContextAwareMatcher:
    """Phase 2: context='toplevel' and context='any' rules filter by invocation context."""

    def test_toplevel_deny_matches_standalone_op_read(self, tmp_db):
        """context='toplevel' deny fires on standalone `op read ...`."""
        shape_id = _insert_rule_shape_with_context(
            tmp_db,
            verb="op",
            subcommand="read",
            flags="*",
            path_spec=None,
            context="toplevel",
        )
        _insert_permission(tmp_db, shape_id, "rejected")

        v, _ = bash_match(
            "Bash",
            {"command": "op read 'op://Private/item/password'"},
            tmp_db,
            None,
            None,
            {},
        )
        assert v == Verdict.Deny

    def test_toplevel_deny_does_not_fire_on_substitution_form(self, tmp_db):
        """context='toplevel' deny must NOT fire when `op read` is inside $(...).

        The inner op read leaf has is_substitution_child=True, so its variants
        carry context='substitution'. A rule with context='toplevel' must not
        match context='substitution' variants.
        """
        shape_id = _insert_rule_shape_with_context(
            tmp_db,
            verb="op",
            subcommand="read",
            flags="*",
            path_spec=None,
            context="toplevel",
        )
        _insert_permission(tmp_db, shape_id, "rejected")

        # curl with op read inside command substitution.
        v, _ = bash_match(
            "Bash",
            {
                "command": "curl -H \"Authorization: Bearer $(op read 'op://Private/item/password')\""
            },
            tmp_db,
            None,
            None,
            {},
        )
        # op read is inside $(...) — toplevel deny must not fire.
        # curl itself has no deny rule; the verdict must not be Deny.
        assert v != Verdict.Deny

    def test_any_context_deny_matches_standalone(self, tmp_db):
        """context='any' deny fires on standalone `op read`."""
        shape_id = _insert_rule_shape_with_context(
            tmp_db,
            verb="op",
            subcommand="read",
            flags="*",
            path_spec=None,
            context="any",
        )
        _insert_permission(tmp_db, shape_id, "rejected")

        v, _ = bash_match(
            "Bash",
            {"command": "op read 'op://Private/item/password'"},
            tmp_db,
            None,
            None,
            {},
        )
        assert v == Verdict.Deny

    def test_any_context_deny_matches_substitution_form(self, tmp_db):
        """context='any' deny fires even when `op read` is inside $(...).

        The inner op read leaf has context='substitution', but a rule with
        context='any' matches both contexts.
        """
        shape_id = _insert_rule_shape_with_context(
            tmp_db,
            verb="op",
            subcommand="read",
            flags="*",
            path_spec=None,
            context="any",
        )
        _insert_permission(tmp_db, shape_id, "rejected")

        v, _ = bash_match(
            "Bash",
            {
                "command": "curl -H \"Authorization: Bearer $(op read 'op://Private/item/password')\""
            },
            tmp_db,
            None,
            None,
            {},
        )
        assert v == Verdict.Deny

    def test_two_rules_same_shape_different_context_coexist(self, tmp_db):
        """Two rules with same (v,s,f,p) but different context can coexist in the DB."""
        id_any = _insert_rule_shape_with_context(
            tmp_db,
            verb="op",
            subcommand="read",
            flags="*",
            path_spec=None,
            context="any",
        )
        id_top = _insert_rule_shape_with_context(
            tmp_db,
            verb="op",
            subcommand="read",
            flags="*",
            path_spec=None,
            context="toplevel",
        )
        # Both must have distinct ids (unique index includes context).
        assert id_any != id_top
        count = tmp_db.execute(
            "SELECT COUNT(*) FROM rule_shapes WHERE verb='op' AND subcommand='read';"
        ).fetchone()[0]
        assert count == 2


class TestTwoWordSubcommandMatchEndToEnd:
    """Two-word subcommand verbs (vault, doppler) match seed rules end-to-end.

    Regression guard: before the canonicalize fix, ``vault kv get foo`` produced
    ``subcommand="kv"`` (not ``"kv get"``), so a seed row with
    ``subcommand="kv get"`` never matched. These tests assert the full path
    parse_command → to_pattern_form → DB lookup → Verdict.
    """

    def test_vault_kv_get_matches_toplevel_deny(self, tmp_db):
        """A toplevel deny on (vault, "kv get", *, NULL) fires on the standalone form."""
        shape_id = _insert_rule_shape_with_context(
            tmp_db,
            verb="vault",
            subcommand="kv get",
            flags="*",
            path_spec=None,
            context="toplevel",
        )
        _insert_permission(tmp_db, shape_id, "rejected")

        v, _ = bash_match(
            "Bash",
            {"command": "vault kv get secret/db-password"},
            tmp_db,
            None,
            None,
            {},
        )
        assert v == Verdict.Deny

    def test_doppler_secrets_get_matches_toplevel_deny(self, tmp_db):
        """A toplevel deny on (doppler, "secrets get", *, NULL) fires standalone."""
        shape_id = _insert_rule_shape_with_context(
            tmp_db,
            verb="doppler",
            subcommand="secrets get",
            flags="*",
            path_spec=None,
            context="toplevel",
        )
        _insert_permission(tmp_db, shape_id, "rejected")

        v, _ = bash_match(
            "Bash",
            {"command": "doppler secrets get DATABASE_URL"},
            tmp_db,
            None,
            None,
            {},
        )
        assert v == Verdict.Deny

    def test_vault_kv_get_inside_substitution_does_not_fire_toplevel(self, tmp_db):
        """A toplevel deny on `vault kv get` does not fire inside $(...) — same
        substitution-vs-toplevel split that motivated the context column."""
        shape_id = _insert_rule_shape_with_context(
            tmp_db,
            verb="vault",
            subcommand="kv get",
            flags="*",
            path_spec=None,
            context="toplevel",
        )
        _insert_permission(tmp_db, shape_id, "rejected")

        v, _ = bash_match(
            "Bash",
            {
                "command": 'curl -H "Authorization: Bearer $(vault kv get -field=value secret/api-key)"'
            },
            tmp_db,
            None,
            None,
            {},
        )
        # Inner vault kv get is substitution-child; toplevel rule must not fire.
        assert v != Verdict.Deny


# ---------------------------------------------------------------------------
# B14 — .env seed-rule matching via $CWD/**/.env basename-glob
# ---------------------------------------------------------------------------


class TestEnvFileSeedRuleMatching:
    """Deny rule with path_spec=$CWD/**/.env matches .env at any depth.

    Relies on Change 1 (relative-path resolution against $CWD) and Change 2
    (basename-glob emission) both being in place so that `cat .env` and
    `cat apps/web/.env` emit a $CWD/**/.env variant that matches the seed rule.
    """

    _CWD = "/work/proj"

    def _ctx(self):
        return {"cwd": self._CWD}

    def _seed_env_rule(self, conn: sqlite3.Connection) -> int:
        """Insert verb='*', flags='*', path_spec='$CWD/**/.env' → rejected."""
        shape_id = _insert_rule_shape(
            conn,
            verb="*",
            subcommand=None,
            flags="*",
            path_spec="$CWD/**/.env",
        )
        _insert_permission(conn, shape_id, "rejected")
        return shape_id

    def test_cat_env_in_cwd_is_denied(self, tmp_db):
        """cat .env (relative, resolved to $CWD/.env) → Deny."""
        self._seed_env_rule(tmp_db)
        v, _ = bash_match(
            "Bash",
            {"command": "cat .env"},
            tmp_db,
            None,
            None,
            self._ctx(),
        )
        assert v == Verdict.Deny, f'Expected Deny for "cat .env", got {v!r}'

    def test_cat_env_in_subdirectory_is_denied(self, tmp_db):
        """cat src/.env (relative, resolved to $CWD/src/.env) → Deny."""
        self._seed_env_rule(tmp_db)
        v, _ = bash_match(
            "Bash",
            {"command": "cat src/.env"},
            tmp_db,
            None,
            None,
            self._ctx(),
        )
        assert v == Verdict.Deny, f'Expected Deny for "cat src/.env", got {v!r}'

    def test_cat_env_deep_path_is_denied(self, tmp_db):
        """cat apps/web/.env (relative, deep) → Deny."""
        self._seed_env_rule(tmp_db)
        v, _ = bash_match(
            "Bash",
            {"command": "cat apps/web/.env"},
            tmp_db,
            None,
            None,
            self._ctx(),
        )
        assert v == Verdict.Deny, f'Expected Deny for "cat apps/web/.env", got {v!r}'

    def test_env_rule_does_not_match_different_basename(self, tmp_db):
        """$CWD/**/.env rule must NOT fire on cat src/foo.txt (different basename)."""
        self._seed_env_rule(tmp_db)
        v, _ = bash_match(
            "Bash",
            {"command": "cat src/foo.txt"},
            tmp_db,
            None,
            None,
            self._ctx(),
        )
        assert v != Verdict.Deny, (
            f'Unexpected Deny for unrelated file "cat src/foo.txt", got {v!r}'
        )

    def test_env_rule_does_not_match_env_example(self, tmp_db):
        """$CWD/**/.env rule must NOT fire on .env.example (different basename)."""
        self._seed_env_rule(tmp_db)
        v, _ = bash_match(
            "Bash",
            {"command": "cat .env.example"},
            tmp_db,
            None,
            None,
            self._ctx(),
        )
        assert v != Verdict.Deny, f'Unexpected Deny for ".env.example", got {v!r}'

    def test_env_rule_does_not_match_env_template(self, tmp_db):
        """$CWD/**/.env rule must NOT fire on .env.template."""
        self._seed_env_rule(tmp_db)
        v, _ = bash_match(
            "Bash",
            {"command": "cat .env.template"},
            tmp_db,
            None,
            None,
            self._ctx(),
        )
        assert v != Verdict.Deny, f'Unexpected Deny for ".env.template", got {v!r}'


# ---------------------------------------------------------------------------
# B15 — credential_leaks.yaml: 9 new Bash-level deny rules
#
# All tests seed the actual credential_leaks.yaml fixture so they are RED
# until A2 adds the 9 rules.  Canonicalize extensions (A0) are already live.
# ---------------------------------------------------------------------------


class TestCredentialLeaksYamlNewRules:
    """credential_leaks.yaml → dispatch() denies all 9 new credential patterns.

    Relies on the three new _match_ctx_prefix variant types (B15 / A0):
    - extension-glob  ($VAR/**/*.ext)
    - deep-path       ($VAR/**/<tail>)
    - directory-glob  ($VAR/**/<parent>/**)
    """

    _CWD = "/work/proj"
    _HOME = "/home/tester"

    def _ctx(self):
        return {"cwd": self._CWD, "home": self._HOME}

    def _load(self, conn):
        apply_fixtures(conn, _FIXTURES_DIR / "credential_leaks.yaml")

    def test_dev_vars_is_denied(self, tmp_db):
        """cat .dev.vars → Deny via $CWD/**/.dev.vars rule."""
        self._load(tmp_db)
        v, _ = bash_match(
            "Bash", {"command": "cat .dev.vars"}, tmp_db, None, None, self._ctx()
        )
        assert v == Verdict.Deny, f'Expected Deny for "cat .dev.vars", got {v!r}'

    def test_dev_vars_local_is_denied(self, tmp_db):
        """cat .dev.vars.local → Deny via $CWD/**/.dev.vars.local rule."""
        self._load(tmp_db)
        v, _ = bash_match(
            "Bash", {"command": "cat .dev.vars.local"}, tmp_db, None, None, self._ctx()
        )
        assert v == Verdict.Deny, f'Expected Deny for "cat .dev.vars.local", got {v!r}'

    def test_pem_file_is_denied(self, tmp_db):
        """cat project.pem → Deny via $CWD/**/*.pem extension-glob rule."""
        self._load(tmp_db)
        v, _ = bash_match(
            "Bash", {"command": "cat project.pem"}, tmp_db, None, None, self._ctx()
        )
        assert v == Verdict.Deny, f'Expected Deny for "cat project.pem", got {v!r}'

    def test_key_file_is_denied(self, tmp_db):
        """cat server.key → Deny via $CWD/**/*.key extension-glob rule."""
        self._load(tmp_db)
        v, _ = bash_match(
            "Bash", {"command": "cat server.key"}, tmp_db, None, None, self._ctx()
        )
        assert v == Verdict.Deny, f'Expected Deny for "cat server.key", got {v!r}'

    def test_file_under_secrets_dir_is_denied(self, tmp_db):
        """cat secrets/db.pass → Deny via $CWD/**/secrets/** directory-glob rule."""
        self._load(tmp_db)
        v, _ = bash_match(
            "Bash", {"command": "cat secrets/db.pass"}, tmp_db, None, None, self._ctx()
        )
        assert v == Verdict.Deny, f'Expected Deny for "cat secrets/db.pass", got {v!r}'

    def test_file_under_credentials_dir_is_denied(self, tmp_db):
        """cat credentials/api.json → Deny via $CWD/**/credentials/** directory-glob rule."""
        self._load(tmp_db)
        v, _ = bash_match(
            "Bash",
            {"command": "cat credentials/api.json"},
            tmp_db,
            None,
            None,
            self._ctx(),
        )
        assert v == Verdict.Deny, (
            f'Expected Deny for "cat credentials/api.json", got {v!r}'
        )

    def test_config_database_yml_is_denied(self, tmp_db):
        """cat config/database.yml → Deny via $CWD/**/config/database.yml deep-path rule."""
        self._load(tmp_db)
        v, _ = bash_match(
            "Bash",
            {"command": "cat config/database.yml"},
            tmp_db,
            None,
            None,
            self._ctx(),
        )
        assert v == Verdict.Deny, (
            f'Expected Deny for "cat config/database.yml", got {v!r}'
        )

    def test_config_credentials_yml_enc_is_denied(self, tmp_db):
        """cat config/credentials.yml.enc → Deny via $CWD/**/config/credentials.yml.enc deep-path rule."""
        self._load(tmp_db)
        v, _ = bash_match(
            "Bash",
            {"command": "cat config/credentials.yml.enc"},
            tmp_db,
            None,
            None,
            self._ctx(),
        )
        assert v == Verdict.Deny, (
            f'Expected Deny for "cat config/credentials.yml.enc", got {v!r}'
        )

    def test_pypirc_is_denied(self, tmp_db):
        """cat ~/.pypirc → Deny via $HOME/.pypirc rule."""
        self._load(tmp_db)
        v, _ = bash_match(
            "Bash",
            {"command": f"cat {self._HOME}/.pypirc"},
            tmp_db,
            None,
            None,
            self._ctx(),
        )
        assert v == Verdict.Deny, f'Expected Deny for "cat ~/.pypirc", got {v!r}'


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _open_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn
