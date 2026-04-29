"""Tests for learners.permission.hook — Phase 8 matching priority.

Tests cover the six-step priority chain:
  1. Procedural deny (deny.yaml denied_verbs / ask_verbs)
  2. Permissions lookup: session → project → global tier
  3. Rejected leaf → deny
  4. All approved → allow
  5. Ask tier: unresolved leaf + deny.yaml ask → pending registration + ask
  6. Fall through

Pattern-variant matching:
  - Literal exact match
  - flags="*" wildcard
  - $VAR verb substitution
  - $VAR path_spec matching

Doom-path cases:
  - Empty / unparseable command → fall through
  - Non-Bash tool → fall through
  - No DB file → graceful degradation
  - Multi-leaf pipeline: one leaf rejected → whole call denied
  - Multi-leaf pipeline: all leaves approved → allow
  - Mixed approved+unresolved → not all approved → ask / fall through
  - No session context (recorder miss) → global-only lookup
  - Ask tier skipped when leaf already has approved permission
  - NULL payload → fall through
"""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Module import — reload ensures monkeypatching takes effect cleanly.
# ---------------------------------------------------------------------------

import nephoscope.learners.permission.hook as hook_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_payload(
    command: str,
    tool_use_id: str = "toolu_test_001",
    cwd: str = "/home/user/project",
    tool: str = "Bash",
) -> dict[str, Any]:
    return {
        "tool_name": tool,
        "tool_input": {"command": command},
        "tool_use_id": tool_use_id,
        "cwd": cwd,
    }


def _run_hook(
    payload: dict[str, Any] | None,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Invoke hook.main() with the given payload; return parsed stdout JSON."""
    raw = json.dumps(payload) if payload is not None else ""
    captured_out = io.StringIO()

    with (
        mock.patch.object(hook_mod, "_db_path", return_value=db_path),
        mock.patch("sys.stdin", io.StringIO(raw)),
        mock.patch("sys.stdout", captured_out),
    ):
        hook_mod.main()

    output = captured_out.getvalue().strip()
    if not output:
        return {}
    return json.loads(output)


def _decision(result: dict[str, Any]) -> str | None:
    """Extract permissionDecision from hook output, or None for fall-through."""
    return result.get("hookSpecificOutput", {}).get("permissionDecision")


def _reason(result: dict[str, Any]) -> str:
    return result.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")


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
    tool_id_val = int(t_row[0]) if t_row else 1

    pending_row = conn.execute(
        "SELECT id FROM call_statuses WHERE name='pending'"
    ).fetchone()
    status_id = int(pending_row[0]) if pending_row else 1

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
    # Fetch the actual id (in case of conflict).
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


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestFallThrough:
    """Step 6: no deny, no permissions, no ask → empty object."""

    def test_empty_payload_falls_through(self, tmp_db, monkeypatch):
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        result = _run_hook(None, db_path, monkeypatch)
        assert result == {}

    def test_non_bash_tool_falls_through(self, tmp_db, monkeypatch):
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        payload = _make_payload("ls -la", tool="Read")
        result = _run_hook(payload, db_path, monkeypatch)
        assert result == {}

    def test_empty_command_falls_through(self, tmp_db, monkeypatch):
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        payload = _make_payload("   ")
        result = _run_hook(payload, db_path, monkeypatch)
        assert result == {}

    def test_unparseable_command_falls_through(self, tmp_db, monkeypatch):
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        payload = _make_payload(")(invalid bash$(")
        result = _run_hook(payload, db_path, monkeypatch)
        assert result == {}

    def test_no_deny_no_permissions_no_ask_falls_through(self, tmp_db, monkeypatch):
        """A plain git-status with no DB rules → fall through."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        payload = _make_payload("git status")
        result = _run_hook(payload, db_path, monkeypatch)
        assert result == {}

    def test_zero_length_command_falls_through(self, tmp_db, monkeypatch):
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        payload = _make_payload("")
        result = _run_hook(payload, db_path, monkeypatch)
        assert result == {}


class TestProceduralDeny:
    """Step 1: deny.yaml ``denied_verbs`` / procedural guard → immediate deny."""

    def test_sudo_is_always_denied(self, tmp_db, monkeypatch):
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        payload = _make_payload("sudo apt-get install vim")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "deny"
        assert "sudo" in _reason(result)

    def test_dd_is_denied(self, tmp_db, monkeypatch):
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        payload = _make_payload("dd if=/dev/zero of=/dev/sda")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "deny"

    def test_shutdown_is_denied(self, tmp_db, monkeypatch):
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        payload = _make_payload("shutdown -h now")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "deny"

    def test_guarded_path_redirection_denied(self, tmp_db, monkeypatch):
        """Redirection into ~/.claude/ is a hard deny regardless of permissions."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        target = Path.home() / ".claude" / "CLAUDE.md"
        payload = _make_payload(f"echo malicious > {target}")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "deny"

    def test_procedural_deny_fires_before_permissions(self, tmp_db, monkeypatch):
        """Even an 'approved' rule_shape cannot override procedural deny."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        # Insert an approved permission for 'sudo'.
        shape_id = _insert_rule_shape(tmp_db, "sudo")
        _insert_permission(tmp_db, shape_id, "approved")
        payload = _make_payload("sudo ls")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "deny"

    def test_deny_fires_without_db(self, tmp_path, monkeypatch):
        """Procedural deny still fires when no DB exists."""
        db_path = tmp_path / "nonexistent.db"
        payload = _make_payload("dd if=/dev/zero of=/dev/sda")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "deny"

    def test_pipeline_with_denied_verb_denies_whole_call(self, tmp_db, monkeypatch):
        """A single denied leaf in a pipeline denies the entire call."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        payload = _make_payload("git log --oneline | sudo tee /tmp/out")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "deny"


class TestPermissionsApproved:
    """Step 4: all leaves approved by permissions → allow."""

    def test_global_approved_emits_allow(self, tmp_db, monkeypatch):
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        shape_id = _insert_rule_shape(tmp_db, "git", "status")
        _insert_permission(tmp_db, shape_id, "approved")  # global tier
        payload = _make_payload("git status")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "allow"

    def test_project_approved_emits_allow(self, tmp_db, monkeypatch):
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        proj_id = _insert_project(tmp_db, "/home/user/project")
        _insert_tool_call(tmp_db, "toolu_proj", project_id=proj_id)
        shape_id = _insert_rule_shape(tmp_db, "git", "diff")
        _insert_permission(tmp_db, shape_id, "approved", project_id=proj_id)
        payload = _make_payload("git diff", tool_use_id="toolu_proj")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "allow"

    def test_session_approved_emits_allow(self, tmp_db, monkeypatch):
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        proj_id = _insert_project(tmp_db, "/home/user/project")
        sess_id = _insert_session(tmp_db, "sess-uuid-001", proj_id)
        _insert_tool_call(tmp_db, "toolu_sess", session_id=sess_id, project_id=proj_id)
        shape_id = _insert_rule_shape(tmp_db, "pytest", None, '["--verbose"]')
        _insert_permission(tmp_db, shape_id, "approved", session_id=sess_id)
        payload = _make_payload("pytest --verbose", tool_use_id="toolu_sess")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "allow"

    def test_multi_leaf_all_approved_emits_allow(self, tmp_db, monkeypatch):
        """Pipeline: every leaf approved → allow."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        shape_git = _insert_rule_shape(tmp_db, "git", "log")
        shape_grep = _insert_rule_shape(tmp_db, "grep", None, '["-E"]')
        _insert_permission(tmp_db, shape_git, "approved")
        _insert_permission(tmp_db, shape_grep, "approved")
        payload = _make_payload("git log | grep -E 'foo'")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "allow"

    def test_allow_reason_contains_verb(self, tmp_db, monkeypatch):
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        shape_id = _insert_rule_shape(tmp_db, "git", "log")
        _insert_permission(tmp_db, shape_id, "approved")
        payload = _make_payload("git log")
        result = _run_hook(payload, db_path, monkeypatch)
        assert "git" in _reason(result)


class TestPermissionsRejected:
    """Step 3: rejected leaf → deny."""

    def test_rejected_emits_deny(self, tmp_db, monkeypatch):
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        shape_id = _insert_rule_shape(tmp_db, "git", "push")
        _insert_permission(tmp_db, shape_id, "rejected")
        payload = _make_payload("git push origin main")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "deny"
        assert "rejected" in _reason(result)

    def test_rejected_reason_names_verb(self, tmp_db, monkeypatch):
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        shape_id = _insert_rule_shape(tmp_db, "rm", None, '["-f","-r"]')
        _insert_permission(tmp_db, shape_id, "rejected")
        payload = _make_payload("rm -rf /tmp/junk")
        result = _run_hook(payload, db_path, monkeypatch)
        # deny.py ask-tier fires before permissions for rm -rf, so we check
        # either deny from procedural or from permissions.
        assert _decision(result) == "deny"

    def test_rejected_beats_global_approved(self, tmp_db, monkeypatch):
        """Session-tier rejection overrides global approval for same shape."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        proj_id = _insert_project(tmp_db, "/home/user/project")
        sess_id = _insert_session(tmp_db, "sess-uuid-002", proj_id)
        _insert_tool_call(tmp_db, "toolu_rej", session_id=sess_id, project_id=proj_id)
        # git fetch has no procedural deny/ask rules → pure permissions test.
        shape_id = _insert_rule_shape(tmp_db, "git", "fetch")
        _insert_permission(tmp_db, shape_id, "approved")  # global approved
        _insert_permission(tmp_db, shape_id, "rejected", session_id=sess_id)
        payload = _make_payload("git fetch", tool_use_id="toolu_rej")
        result = _run_hook(payload, db_path, monkeypatch)
        # Session rejection wins — lookup_permissions returns session row first.
        assert _decision(result) == "deny"

    def test_pipeline_one_leaf_rejected_denies_all(self, tmp_db, monkeypatch):
        """If any leaf in a pipeline is rejected, the whole call is denied."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        shape_log = _insert_rule_shape(tmp_db, "git", "log")
        shape_push = _insert_rule_shape(tmp_db, "git", "push")
        _insert_permission(tmp_db, shape_log, "approved")
        _insert_permission(tmp_db, shape_push, "rejected")
        payload = _make_payload("git log && git push origin main")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "deny"


class TestTierPriority:
    """Session tier beats project; project beats global."""

    def test_session_approved_beats_project_rejected(self, tmp_db, monkeypatch):
        """Session approval takes priority over project rejection."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        proj_id = _insert_project(tmp_db, "/home/user/project")
        sess_id = _insert_session(tmp_db, "sess-uuid-003", proj_id)
        _insert_tool_call(tmp_db, "toolu_tier1", session_id=sess_id, project_id=proj_id)
        shape_id = _insert_rule_shape(tmp_db, "git", "fetch")
        _insert_permission(tmp_db, shape_id, "rejected", project_id=proj_id)
        _insert_permission(tmp_db, shape_id, "approved", session_id=sess_id)
        payload = _make_payload("git fetch", tool_use_id="toolu_tier1")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "allow"

    def test_project_approved_beats_global_rejected(self, tmp_db, monkeypatch):
        """Project approval takes priority over global rejection."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        proj_id = _insert_project(tmp_db, "/home/user/project")
        _insert_tool_call(tmp_db, "toolu_tier2", project_id=proj_id)
        shape_id = _insert_rule_shape(tmp_db, "git", "fetch")
        _insert_permission(tmp_db, shape_id, "rejected")  # global rejected
        _insert_permission(tmp_db, shape_id, "approved", project_id=proj_id)
        payload = _make_payload("git fetch", tool_use_id="toolu_tier2")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "allow"

    def test_no_session_context_falls_to_global(self, tmp_db, monkeypatch):
        """When recorder row is missing, global tier still applies."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        shape_id = _insert_rule_shape(tmp_db, "git", "log")
        _insert_permission(tmp_db, shape_id, "approved")  # global
        # No tool_call row inserted → session_id=None, project_id=None.
        payload = _make_payload("git log", tool_use_id="toolu_no_context")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "allow"


class TestAskTier:
    """Step 5: unresolved leaf with deny.yaml ask rule → pending + ask."""

    def test_ask_verb_emits_ask(self, tmp_db, monkeypatch):
        """rm -r is in ask_flag_patterns; unresolved → ask."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        proj_id = _insert_project(tmp_db, "/home/user/project")
        sess_id = _insert_session(tmp_db, "sess-uuid-ask1", proj_id)
        _insert_tool_call(tmp_db, "toolu_ask1", session_id=sess_id, project_id=proj_id)
        payload = _make_payload("rm -r /tmp/file.txt", tool_use_id="toolu_ask1")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "ask"

    def test_ask_registers_pending_row(self, tmp_db, monkeypatch):
        """Ask writes a permission_ask_pending row with inlined shape."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        proj_id = _insert_project(tmp_db, "/home/user/project")
        sess_id = _insert_session(tmp_db, "sess-uuid-ask2", proj_id)
        _insert_tool_call(tmp_db, "toolu_ask2", session_id=sess_id, project_id=proj_id)
        payload = _make_payload("rm -f /tmp/file.txt", tool_use_id="toolu_ask2")
        _run_hook(payload, db_path, monkeypatch)
        # Re-open DB to check for pending row.
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT verb, session_id FROM permission_ask_pending"
            " WHERE tool_use_id = 'toolu_ask2';"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "rm"
        assert row[1] == sess_id

    def test_ask_skipped_when_leaf_already_approved(self, tmp_db, monkeypatch):
        """An approved permission overrides ask tier — emit allow, not ask."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        # rm with -f flag has an ask rule; but we have a matching approval.
        shape_id = _insert_rule_shape(tmp_db, "rm", None, '["-f"]')
        _insert_permission(tmp_db, shape_id, "approved")
        payload = _make_payload("rm -f /tmp/file.txt")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "allow"

    def test_ask_without_session_no_pending_row(self, tmp_db, monkeypatch):
        """Ask emits ask but does NOT insert pending when session_id is None."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        # No tool_call row → no session_id.
        payload = _make_payload("rm -r /tmp/file.txt", tool_use_id="toolu_norow")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "ask"
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT 1 FROM permission_ask_pending WHERE tool_use_id = 'toolu_norow';"
        ).fetchone()
        conn.close()
        assert row is None

    def test_ask_fires_even_without_db(self, tmp_path, monkeypatch):
        """No-DB path: ask still emits ask for ask_flag_patterns matches."""
        db_path = tmp_path / "nonexistent.db"
        payload = _make_payload("rm -r /tmp/file.txt")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "ask"

    def test_no_db_falls_through_for_plain_command(self, tmp_path, monkeypatch):
        """No-DB path: command with no ask/deny rule → fall through."""
        db_path = tmp_path / "nonexistent.db"
        payload = _make_payload("git status")
        result = _run_hook(payload, db_path, monkeypatch)
        assert result == {}


class TestPatternVariantMatching:
    """Pattern variants (flags=*, $VAR verb, $VAR path_spec) match in DB."""

    def test_flags_wildcard_matches_any_flags(self, tmp_db, monkeypatch):
        """rule_shapes row with flags='*' approves any flag combination."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        # Use literal flags key for the rule_shape — flags="*" wildcard.
        shape_id = _insert_rule_shape(tmp_db, "pytest", None, "*")
        _insert_permission(tmp_db, shape_id, "approved")
        payload = _make_payload("pytest -q --tb=short --no-header")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "allow"

    def test_literal_shape_exact_match(self, tmp_db, monkeypatch):
        """Exact literal rule_shape match (verb+sub+flags) → approved."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        shape_id = _insert_rule_shape(tmp_db, "git", "log", '["-q"]')
        _insert_permission(tmp_db, shape_id, "approved")
        payload = _make_payload("git log -q")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "allow"

    def test_path_spec_glob_match(self, tmp_db, monkeypatch):
        """rule_shape with path_spec='$PROJECT_ROOT/**' matches via variant."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        cwd = "/home/user/myproject/repository"
        proj_root = "/home/user/myproject"
        proj_id = _insert_project(tmp_db, cwd, root=proj_root)
        _insert_tool_call(tmp_db, "toolu_pathspec", project_id=proj_id)

        # Shape with $PROJECT_ROOT/** path spec.
        shape_id = _insert_rule_shape(tmp_db, "cat", None, "[]", "$PROJECT_ROOT/**")
        _insert_permission(tmp_db, shape_id, "approved")

        # cat /home/user/myproject/src/main.py should match via $PROJECT_ROOT/**
        payload = _make_payload(
            f"cat {proj_root}/src/main.py",
            tool_use_id="toolu_pathspec",
            cwd=cwd,
        )
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "allow"

    def test_verb_var_substitution_match(self, tmp_db, monkeypatch):
        """rule_shape with verb='$HOME/...' matches an absolute-path binary."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        home = str(Path.home())
        local_bin = f"{home}/.local/bin/my-tool"

        # The rule_shape uses the $HOME-substituted verb form.
        shape_id = _insert_rule_shape(tmp_db, "$HOME/.local/bin/my-tool")
        _insert_permission(tmp_db, shape_id, "approved")

        payload = _make_payload(local_bin, cwd="/home/user/project")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "allow"

    def test_no_matching_variant_falls_through(self, tmp_db, monkeypatch):
        """If no variant matches rule_shapes, result is fall through."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        # Insert a shape for a DIFFERENT verb — no match.
        shape_id = _insert_rule_shape(tmp_db, "unrelated_verb")
        _insert_permission(tmp_db, shape_id, "approved")
        payload = _make_payload("git status")
        result = _run_hook(payload, db_path, monkeypatch)
        assert result == {}


class TestMixedPipelineLeaves:
    """Multi-leaf edge cases: mixed approved/unresolved → ask or fall through."""

    def test_one_leaf_approved_one_unresolved_falls_through(self, tmp_db, monkeypatch):
        """Not all leaves approved → not allow; unresolved leaf has no ask rule."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        shape_id = _insert_rule_shape(tmp_db, "git", "log")
        _insert_permission(tmp_db, shape_id, "approved")
        # wc has no rule and no ask rule → fall through.
        payload = _make_payload("git log | wc -l")
        result = _run_hook(payload, db_path, monkeypatch)
        # Approved leaf not enough for allow; wc is unresolved and non-ask.
        assert result == {}

    def test_one_leaf_approved_one_ask_emits_ask(self, tmp_db, monkeypatch):
        """Approved leaf + ask-tier unresolved leaf → ask."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        shape_id = _insert_rule_shape(tmp_db, "git", "log")
        _insert_permission(tmp_db, shape_id, "approved")
        # rm -r matches ask_flag_patterns; no approval in DB.
        # Use && to get two separate top-level leaves.
        payload = _make_payload("git log && rm -r /tmp/stale.txt")
        result = _run_hook(payload, db_path, monkeypatch)
        assert _decision(result) == "ask"

    def test_idempotent_pending_row(self, tmp_db, monkeypatch):
        """INSERT OR IGNORE: a second ask for the same tool_use_id is a no-op."""
        db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        proj_id = _insert_project(tmp_db, "/home/user/project")
        sess_id = _insert_session(tmp_db, "sess-idem", proj_id)
        _insert_tool_call(tmp_db, "toolu_idem", session_id=sess_id, project_id=proj_id)
        payload = _make_payload("rm -r /tmp/a", tool_use_id="toolu_idem")
        _run_hook(payload, db_path, monkeypatch)
        _run_hook(payload, db_path, monkeypatch)
        conn = sqlite3.connect(str(db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM permission_ask_pending WHERE tool_use_id='toolu_idem';"
        ).fetchone()[0]
        conn.close()
        assert count == 1
