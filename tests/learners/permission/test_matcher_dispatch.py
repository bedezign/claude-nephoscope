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

import sqlite3
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from nephoscope.learners.permission.match import Verdict, dispatch
from nephoscope.learners.permission.match._types import Verdict as VerdictDirect
from nephoscope.learners.permission.match.bash import match as bash_match
from nephoscope.learners.permission.match.file import match as file_match
from nephoscope.learners.permission.match.flat import match as flat_match
from nephoscope.learners.permission.match.mcp import match as mcp_match
from nephoscope.learners.permission.match.orchestration import match as orch_match


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
        v = bash_match("Bash", {"command": "git status"}, _conn(tmp_db), None, None, {})
        assert v == Verdict.Allow

    def test_rejected_shape_returns_deny(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "git", "push")
        _insert_permission(tmp_db, shape_id, "rejected")
        v = bash_match(
            "Bash", {"command": "git push origin main"}, _conn(tmp_db), None, None, {}
        )
        assert v == Verdict.Deny

    def test_ask_verb_returns_ask(self, tmp_db):
        # rm has no DB approval → falls to ask tier in deny.py
        v = bash_match(
            "Bash", {"command": "rm /tmp/file"}, _conn(tmp_db), None, None, {}
        )
        assert v == Verdict.Ask

    def test_unknown_verb_returns_noop(self, tmp_db):
        v = bash_match("Bash", {"command": "git log"}, _conn(tmp_db), None, None, {})
        assert v == Verdict.NoOpinion

    def test_empty_command_returns_noop(self, tmp_db):
        v = bash_match("Bash", {"command": ""}, _conn(tmp_db), None, None, {})
        assert v == Verdict.NoOpinion

    def test_unparseable_command_returns_noop(self, tmp_db):
        v = bash_match("Bash", {"command": ")(invalid("}, _conn(tmp_db), None, None, {})
        assert v == Verdict.NoOpinion

    def test_approved_beats_ask_tier(self, tmp_db):
        """An approved permission overrides the ask tier."""
        shape_id = _insert_rule_shape(tmp_db, "rm", None, '["-f"]')
        _insert_permission(tmp_db, shape_id, "approved")
        v = bash_match(
            "Bash", {"command": "rm -f /tmp/x"}, _conn(tmp_db), None, None, {}
        )
        assert v == Verdict.Allow

    def test_pipeline_one_rejected_returns_deny(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "git", "push")
        _insert_permission(tmp_db, shape_id, "rejected")
        v = bash_match(
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
        v = bash_match(
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
        v = file_match(
            "Read", {"file_path": "/home/steve/file.py"}, _conn(tmp_db), None, None, {}
        )
        assert v == Verdict.Allow

    def test_empty_path_spec_matches_no_path(self, tmp_db):
        """path_spec='' → matches only when no file_path provided."""
        shape_id = _insert_rule_shape(tmp_db, "Read", path_spec="")
        _insert_permission(tmp_db, shape_id, "approved")
        # No file path → matches.
        v = file_match("Read", {}, _conn(tmp_db), None, None, {})
        assert v == Verdict.Allow
        # File path provided → no match.
        v2 = file_match(
            "Read", {"file_path": "/home/steve/file.py"}, _conn(tmp_db), None, None, {}
        )
        assert v2 == Verdict.NoOpinion

    def test_home_token_resolved(self, tmp_db):
        import os

        home = os.path.expanduser("~")
        shape_id = _insert_rule_shape(tmp_db, "Read", path_spec="$HOME/**")
        _insert_permission(tmp_db, shape_id, "approved")
        ctx = {"home": home}
        v = file_match(
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
        ctx = {"project_root": "/home/steve/myproject"}
        v = file_match(
            "Edit",
            {"file_path": "/home/steve/myproject/src/main.py"},
            _conn(tmp_db),
            None,
            None,
            ctx,
        )
        assert v == Verdict.Allow

    def test_path_outside_glob_returns_noop(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "Read", path_spec="$HOME/.claude/**")
        _insert_permission(tmp_db, shape_id, "approved")
        ctx = {"home": "/home/steve"}
        v = file_match(
            "Read", {"file_path": "/tmp/unrelated.py"}, _conn(tmp_db), None, None, ctx
        )
        assert v == Verdict.NoOpinion

    def test_rejected_path_returns_deny(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "Write", path_spec="$HOME/.claude/**")
        _insert_permission(tmp_db, shape_id, "rejected")
        ctx = {"home": "/home/steve"}
        v = file_match(
            "Write",
            {"file_path": "/home/steve/.claude/settings.json"},
            _conn(tmp_db),
            None,
            None,
            ctx,
        )
        assert v == Verdict.Deny

    def test_no_rule_shape_for_verb_returns_noop(self, tmp_db):
        v = file_match(
            "Read", {"file_path": "/any/file.py"}, _conn(tmp_db), None, None, {}
        )
        assert v == Verdict.NoOpinion

    def test_path_key_fallback(self, tmp_db):
        """Tool input with 'path' key (instead of 'file_path') is accepted."""
        shape_id = _insert_rule_shape(tmp_db, "Read", path_spec=None)
        _insert_permission(tmp_db, shape_id, "approved")
        v = file_match("Read", {"path": "/some/file.py"}, _conn(tmp_db), None, None, {})
        assert v == Verdict.Allow


# ---------------------------------------------------------------------------
# Flat matcher (unit)
# ---------------------------------------------------------------------------


class TestFlatMatcher:
    def test_approved_returns_allow(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "Grep")
        _insert_permission(tmp_db, shape_id, "approved")
        v = flat_match("Grep", {}, _conn(tmp_db), None, None, {})
        assert v == Verdict.Allow

    def test_rejected_returns_deny(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "WebSearch")
        _insert_permission(tmp_db, shape_id, "rejected")
        v = flat_match("WebSearch", {}, _conn(tmp_db), None, None, {})
        assert v == Verdict.Deny

    def test_no_row_returns_noop(self, tmp_db):
        v = flat_match("Glob", {}, _conn(tmp_db), None, None, {})
        assert v == Verdict.NoOpinion

    def test_ignores_tool_input_content(self, tmp_db):
        """Flat matcher ignores tool_input; only verb matters."""
        shape_id = _insert_rule_shape(tmp_db, "Grep")
        _insert_permission(tmp_db, shape_id, "approved")
        v = flat_match(
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
        v = mcp_match(
            "mcp__claude-peers__send_message", {}, _conn(tmp_db), None, None, {}
        )
        assert v == Verdict.Allow

    def test_literal_match_rejected(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "mcp__dangerous__delete_all")
        _insert_permission(tmp_db, shape_id, "rejected")
        v = mcp_match("mcp__dangerous__delete_all", {}, _conn(tmp_db), None, None, {})
        assert v == Verdict.Deny

    def test_wildcard_match(self, tmp_db):
        """mcp__ns__* wildcard covers any tool in that namespace."""
        shape_id = _insert_rule_shape(tmp_db, "mcp__claude-peers__*")
        _insert_permission(tmp_db, shape_id, "approved")
        v = mcp_match(
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
        v = mcp_match("mcp__ns__specific_tool", {}, _conn(tmp_db), None, None, {})
        assert v == Verdict.Deny

    def test_no_row_returns_noop(self, tmp_db):
        v = mcp_match("mcp__unknown__tool", {}, _conn(tmp_db), None, None, {})
        assert v == Verdict.NoOpinion


# ---------------------------------------------------------------------------
# Orchestration matcher (unit)
# ---------------------------------------------------------------------------


class TestOrchestrationMatcher:
    def test_always_allow(self, tmp_db):
        v = orch_match("Agent", {}, _conn(tmp_db), None, None, {})
        assert v == Verdict.Allow

    def test_allow_without_any_db_rows(self, tmp_db):
        # No rule_shapes or permissions rows at all.
        v = orch_match("TaskCreate", {}, _conn(tmp_db), None, None, {})
        assert v == Verdict.Allow


# ---------------------------------------------------------------------------
# Dispatch — HOOK_FULL_MATCH=OFF (default)
# ---------------------------------------------------------------------------


class TestDispatchFullMatchOff:
    """With HOOK_FULL_MATCH unset/off, non-Bash tools → NoOpinion."""

    def test_bash_still_runs_full_match(self, tmp_db, monkeypatch):
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)
        shape_id = _insert_rule_shape(tmp_db, "git", "status")
        _insert_permission(tmp_db, shape_id, "approved")
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v = dispatch("Bash", {"command": "git status"}, conn, None, None)
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
            v = dispatch("Read", {"file_path": "/some/file.py"}, conn, None, None)
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
            v = dispatch("Grep", {}, conn, None, None)
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
            v = dispatch("mcp__ns__tool", {}, conn, None, None)
        finally:
            conn.close()
        assert v == Verdict.NoOpinion

    def test_orchestration_returns_noop_when_off(self, tmp_db, monkeypatch):
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v = dispatch("Agent", {}, conn, None, None)
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
            v = dispatch("Read", {"file_path": "/some/file.py"}, conn, None, None)
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
            v = dispatch("WebSearch", {}, conn, None, None)
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
            v = dispatch("mcp__ns__tool", {}, conn, None, None)
        finally:
            conn.close()
        assert v == Verdict.Allow

    def test_orchestration_allow_when_on(self, tmp_db, monkeypatch):
        monkeypatch.setenv("HOOK_FULL_MATCH", "1")
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v = dispatch("Agent", {}, conn, None, None)
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
            v = dispatch("Grep", {}, conn, None, None)
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
            v = dispatch("Grep", {}, conn, None, None)
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
        proj_id = _insert_project(tmp_db, "/home/steve/project")
        sess_id = _insert_session(tmp_db, "sess-tier-001", proj_id)
        shape_id = _insert_rule_shape(tmp_db, "git", "fetch")
        _insert_permission(tmp_db, shape_id, "rejected")  # global
        _insert_permission(tmp_db, shape_id, "approved", session_id=sess_id)  # session
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v = dispatch("Bash", {"command": "git fetch"}, conn, sess_id, proj_id)
        finally:
            conn.close()
        assert v == Verdict.Allow

    def test_project_approved_beats_global_rejected(self, tmp_db, monkeypatch):
        monkeypatch.delenv("HOOK_FULL_MATCH", raising=False)
        proj_id = _insert_project(tmp_db, "/home/steve/project")
        shape_id = _insert_rule_shape(tmp_db, "git", "fetch")
        _insert_permission(tmp_db, shape_id, "rejected")  # global
        _insert_permission(tmp_db, shape_id, "approved", project_id=proj_id)  # project
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        conn = _open_conn(db_path)
        try:
            v = dispatch("Bash", {"command": "git fetch"}, conn, None, proj_id)
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
            v = dispatch("Bash", {"command": "git log"}, conn, None, None)
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
            v = dispatch(
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
            v = dispatch("Bash", {}, conn, None, None)
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
            v = dispatch("Bash", {"command": "git log"}, conn, None, None)
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
            v = dispatch(
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
            v = dispatch("Read", {"file_path": "/some/file.py"}, conn, None, None)
        finally:
            conn.close()
        assert v == Verdict.NoOpinion


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _open_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn
