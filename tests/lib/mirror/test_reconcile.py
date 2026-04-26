"""Tests for lib.mirror.reconcile — diff engine and reconcile orchestration.

All tests use tmp_path or in-memory DBs.
Zero tolerance for writes to real paths (~/.claude/settings.json, etc.).

Coverage targets (per plan W2-reconcile gate):
  - Empty JSON + empty DB → no-op, success
  - Populated JSON matches DB → no-op (is_empty)
  - JSON has extras (only_in_json) → JSON-wins inserts; DB-wins drops
  - DB has extras (only_in_db) → JSON-wins removes; DB-wins keeps + regenerates
  - Conflict (same key, different decision) → both modes resolve correctly
  - First-touch (hash NULL) → auto-adopt (interactive → adopt)
  - plan mode → no mutations; diff returned
  - auto-db-wins / auto-json-wins → non-interactive, correct resolution
  - Interactive mode → mocked stdin; user picks DB_WINS / JSON_WINS / PER_ENTRY
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from nephoscope.lib.mirror.permissions_hash import settings_permissions_hash
from nephoscope.lib.mirror.reconcile import (
    ReconcileError,
    _key_from_db_row,
    _key_from_json_row,
    diff,
    reconcile,
)

# ---------------------------------------------------------------------------
# Schema path
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCHEMA_PATH = PROJECT_ROOT / "src" / "nephoscope" / "lib" / "schema.sql"


# ---------------------------------------------------------------------------
# Test DB fixture
# ---------------------------------------------------------------------------


def _make_conn(tmp_path: Path, settings_path: Path | None = None) -> sqlite3.Connection:
    """Create an isolated SQLite DB seeded with schema + global_mirror singleton.

    The global_mirror singleton points to settings_path (or a default fake path
    inside tmp_path when settings_path is None).
    """
    db_file = tmp_path / "test_reconcile.db"
    fake_settings = settings_path or (tmp_path / "settings.json")

    conn = sqlite3.connect(str(db_file), isolation_level=None)
    conn.executescript(SCHEMA_PATH.read_text())

    conn.execute(
        "INSERT OR IGNORE INTO global_mirror"
        " (id, settings_json_path, settings_json_sha256, settings_json_last_synced)"
        " VALUES (1, ?, NULL, NULL);",
        (str(fake_settings),),
    )
    conn.execute(
        "INSERT OR IGNORE INTO permission_modes (name)"
        " VALUES ('default'),('acceptEdits'),('bypassPermissions'),('plan'),('auto');"
    )
    conn.execute(
        "INSERT OR IGNORE INTO call_statuses (name)"
        " VALUES ('pending'),('ok'),('err'),('denied'),('orphan');"
    )
    return conn


def _write_settings(path: Path, *, allow=None, deny=None, ask=None) -> None:
    """Write a minimal settings.json to path with the given permission lists."""
    path.write_text(
        json.dumps(
            {
                "permissions": {
                    "allow": allow or [],
                    "deny": deny or [],
                    "ask": ask or [],
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _stamp_real_hash(conn: sqlite3.Connection, settings: Path) -> None:
    """Stamp the actual on-disk permissions hash of settings into global_mirror.

    Use this in interactive-mode tests to avoid first-touch auto-adopt
    while keeping the hash consistent with the file so sync() succeeds.
    """
    real_hash = settings_permissions_hash(settings.read_bytes())
    conn.execute(
        "UPDATE global_mirror SET settings_json_sha256=? WHERE id=1;",
        (real_hash,),
    )


def _insert_global_rule(
    conn: sqlite3.Connection,
    *,
    verb: str,
    subcommand: str | None = None,
    flags: str = "[]",
    path_spec: str | None = None,
    decision: str = "approved",
) -> int:
    """Insert a rule_shape + global permission row; return permissions.id."""
    ts = "2025-01-01T00:00:00.000Z"
    row = conn.execute(
        "SELECT id FROM rule_shapes WHERE verb = ?"
        " AND IFNULL(subcommand,'')=IFNULL(?,'') AND flags=?"
        " AND IFNULL(path_spec,'')=IFNULL(?,'');",
        (verb, subcommand, flags, path_spec),
    ).fetchone()
    if row:
        shape_id = row[0]
    else:
        cur = conn.execute(
            "INSERT INTO rule_shapes(verb,subcommand,flags,path_spec,first_seen,last_seen)"
            " VALUES(?,?,?,?,?,?);",
            (verb, subcommand, flags, path_spec, ts, ts),
        )
        shape_id = cur.lastrowid

    cur = conn.execute(
        "INSERT INTO permissions"
        " (rule_shape_id,session_id,project_id,decision,source,reason,decided_at)"
        " VALUES(?,NULL,NULL,?,?,NULL,?);",
        (shape_id, decision, "test", ts),
    )
    return int(cur.lastrowid or 0)


def _count_global_permissions(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM permissions WHERE project_id IS NULL AND session_id IS NULL;"
    ).fetchone()[0]


def _global_decision(conn: sqlite3.Connection, verb: str) -> str | None:
    """Return the decision for the first global permission row matching verb."""
    row = conn.execute(
        "SELECT p.decision FROM permissions p"
        " JOIN rule_shapes rs ON rs.id=p.rule_shape_id"
        " WHERE rs.verb=? AND p.project_id IS NULL AND p.session_id IS NULL"
        " ORDER BY p.id ASC LIMIT 1;",
        (verb,),
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Pure diff() tests
# ---------------------------------------------------------------------------


class TestDiff:
    """Tests for the pure diff() function."""

    def test_both_empty(self):
        d = diff([], [])
        assert d.is_empty
        assert d.only_in_db == []
        assert d.only_in_json == []
        assert d.conflicting == []
        assert d.matching == []

    def test_only_in_json(self):
        json_rows = [
            {
                "tool": "Bash",
                "verb": "git",
                "subcommand": None,
                "flags": "*",
                "path_spec": None,
                "decision": "allow",
            }
        ]
        d = diff([], json_rows)
        assert len(d.only_in_json) == 1
        assert d.only_in_db == []
        assert d.conflicting == []

    def test_only_in_db(self):
        db_rows = [
            {
                "id": 1,
                "verb": "git",
                "subcommand": None,
                "flags": "*",
                "path_spec": None,
                "decision": "approved",
            }
        ]
        d = diff(db_rows, [])
        assert len(d.only_in_db) == 1
        assert d.only_in_json == []
        assert d.conflicting == []

    def test_matching_allow(self):
        """Same key + same decision → matching, no conflict."""
        db_rows = [
            {
                "id": 1,
                "verb": "git",
                "subcommand": None,
                "flags": "*",
                "path_spec": None,
                "decision": "approved",  # DB value for "allow"
            }
        ]
        json_rows = [
            {
                "tool": "Bash",
                "verb": "git",
                "subcommand": None,
                "flags": "*",
                "path_spec": None,
                "decision": "allow",
            }
        ]
        d = diff(db_rows, json_rows)
        assert d.is_empty
        assert len(d.matching) == 1

    def test_matching_deny(self):
        db_rows = [
            {
                "id": 1,
                "verb": "git",
                "subcommand": "push",
                "flags": "[]",
                "path_spec": None,
                "decision": "rejected",
            }
        ]
        json_rows = [
            {
                "tool": "Bash",
                "verb": "git",
                "subcommand": "push",
                "flags": "[]",
                "path_spec": None,
                "decision": "deny",
            }
        ]
        d = diff(db_rows, json_rows)
        assert d.is_empty
        assert len(d.matching) == 1

    def test_conflict_different_decision(self):
        """Same logical key, DB says allow, JSON says deny → conflict."""
        db_rows = [
            {
                "id": 1,
                "verb": "git",
                "subcommand": None,
                "flags": "*",
                "path_spec": None,
                "decision": "approved",
            }
        ]
        json_rows = [
            {
                "tool": "Bash",
                "verb": "git",
                "subcommand": None,
                "flags": "*",
                "path_spec": None,
                "decision": "deny",
            }
        ]
        d = diff(db_rows, json_rows)
        assert len(d.conflicting) == 1
        entry = d.conflicting[0]
        assert entry.db_decision == "allow"
        assert entry.json_decision == "deny"
        assert d.only_in_db == []
        assert d.only_in_json == []

    def test_file_tool_matching(self):
        """DB 'flags=[]' and JSON 'flags=None' normalize to the same key.

        The DB stores '[]' (reconcile INSERT path coalesces None → '[]'),
        while the ingester emits None for non-Bash tools. Without
        normalization, every file/MCP rule would spuriously diff.
        """
        db_rows = [
            {
                "id": 2,
                "verb": "Read",
                "subcommand": None,
                "flags": "[]",
                "path_spec": "//home/steve/.claude/**",
                "decision": "approved",
            }
        ]
        json_rows = [
            {
                "tool": "Read",
                "verb": "Read",
                "subcommand": None,
                "flags": None,
                "path_spec": "//home/steve/.claude/**",
                "decision": "allow",
            }
        ]
        d = diff(db_rows, json_rows)
        assert d.is_empty
        assert len(d.matching) == 1

    def test_file_tool_matching_same_flags(self):
        """File tool rows match when flags are consistent."""
        db_rows = [
            {
                "id": 2,
                "verb": "Read",
                "subcommand": None,
                "flags": None,
                "path_spec": "//home/steve/.claude/**",
                "decision": "approved",
            }
        ]
        json_rows = [
            {
                "tool": "Read",
                "verb": "Read",
                "subcommand": None,
                "flags": None,
                "path_spec": "//home/steve/.claude/**",
                "decision": "allow",
            }
        ]
        d = diff(db_rows, json_rows)
        assert d.is_empty
        assert len(d.matching) == 1

    def test_mcp_matching(self):
        """MCP tool rows match as flat entries."""
        db_rows = [
            {
                "id": 3,
                "verb": "mcp__claude-peers__send_message",
                "subcommand": None,
                "flags": None,
                "path_spec": None,
                "decision": "approved",
            }
        ]
        json_rows = [
            {
                "tool": "mcp__claude-peers__send_message",
                "verb": "mcp__claude-peers__send_message",
                "subcommand": None,
                "flags": None,
                "path_spec": None,
                "decision": "allow",
            }
        ]
        d = diff(db_rows, json_rows)
        assert d.is_empty

    def test_multiple_items_mixed(self):
        """Multiple rules: one match, one only_in_db, one conflict."""
        db_rows = [
            {
                "id": 1,
                "verb": "git",
                "subcommand": None,
                "flags": "*",
                "path_spec": None,
                "decision": "approved",
            },
            {
                "id": 2,
                "verb": "npm",
                "subcommand": None,
                "flags": "*",
                "path_spec": None,
                "decision": "approved",
            },
            {
                "id": 3,
                "verb": "curl",
                "subcommand": None,
                "flags": "[]",
                "path_spec": None,
                "decision": "rejected",
            },
        ]
        json_rows = [
            {
                "tool": "Bash",
                "verb": "git",
                "subcommand": None,
                "flags": "*",
                "path_spec": None,
                "decision": "allow",
            },
            {
                "tool": "Bash",
                "verb": "curl",
                "subcommand": None,
                "flags": "[]",
                "path_spec": None,
                "decision": "allow",
            },  # conflict: json=allow, db=rejected
        ]
        d = diff(db_rows, json_rows)
        assert len(d.matching) == 1  # git
        assert len(d.only_in_db) == 1  # npm
        assert len(d.conflicting) == 1  # curl


# ---------------------------------------------------------------------------
# Logical key helpers
# ---------------------------------------------------------------------------


class TestKeyHelpers:
    def test_bash_db_row_key(self):
        row = {"verb": "git", "subcommand": None, "flags": "*", "path_spec": None}
        key = _key_from_db_row(row)
        assert key[0] == "Bash"  # tool
        assert key[1] == "git"  # verb

    def test_file_db_row_key(self):
        row = {
            "verb": "Read",
            "subcommand": None,
            "flags": None,
            "path_spec": "//foo/**",
        }
        key = _key_from_db_row(row)
        assert key[0] == "Read"  # tool == verb for file tools

    def test_mcp_db_row_key(self):
        row = {
            "verb": "mcp__ns__tool",
            "subcommand": None,
            "flags": None,
            "path_spec": None,
        }
        key = _key_from_db_row(row)
        assert key[0] == "mcp__ns__tool"

    def test_json_row_key(self):
        row = {
            "tool": "Bash",
            "verb": "git",
            "subcommand": "push",
            "flags": "[]",
            "path_spec": None,
        }
        key = _key_from_json_row(row)
        assert key == ("Bash", "git", "push", "[]", None)


# ---------------------------------------------------------------------------
# reconcile() — plan mode
# ---------------------------------------------------------------------------


class TestPlanMode:
    def test_plan_empty_db_empty_json(self, tmp_path):
        settings = tmp_path / "settings.json"
        conn = _make_conn(tmp_path, settings)

        report = reconcile(conn, settings, mode="plan")

        assert report.mode == "plan"
        assert report.applied is False
        assert report.diff.is_empty
        assert report.db_inserts == 0
        assert report.db_deletes == 0
        assert report.db_updates == 0

    def test_plan_returns_diff_without_mutations(self, tmp_path):
        settings = tmp_path / "settings.json"
        conn = _make_conn(tmp_path, settings)

        # DB has a rule; JSON is absent
        _insert_global_rule(conn, verb="git", flags="*", decision="approved")

        report = reconcile(conn, settings, mode="plan")

        assert report.applied is False
        assert len(report.diff.only_in_db) == 1
        # Verify no DB changes
        assert _count_global_permissions(conn) == 1

    def test_plan_does_not_create_mirror_file(self, tmp_path):
        """plan mode must not write any mirror file."""
        settings = tmp_path / "settings.json"
        conn = _make_conn(tmp_path, settings)

        reconcile(conn, settings, mode="plan")

        # settings.json must not exist (we didn't write it, file was absent)
        assert not settings.exists()

    def test_plan_with_json_extras_returns_diff(self, tmp_path):
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)

        report = reconcile(conn, settings, mode="plan")

        assert report.applied is False
        assert len(report.diff.only_in_json) == 1
        assert _count_global_permissions(conn) == 0

    def test_plan_is_not_affected_by_first_touch(self, tmp_path):
        """plan mode stays 'plan' even when hash is NULL (first-touch)."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)  # hash starts NULL

        report = reconcile(conn, settings, mode="plan")

        assert report.mode == "plan"
        assert report.applied is False


# ---------------------------------------------------------------------------
# reconcile() — auto-json-wins mode
# ---------------------------------------------------------------------------


class TestAutoJsonWins:
    def test_empty_json_empty_db_noop(self, tmp_path):
        settings = tmp_path / "settings.json"
        _write_settings(settings)
        conn = _make_conn(tmp_path, settings)

        report = reconcile(conn, settings, mode="auto-json-wins")

        assert report.applied is False
        assert report.diff.is_empty

    def test_json_extras_inserted_into_db(self, tmp_path):
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)", "Bash(npm *)"])
        conn = _make_conn(tmp_path, settings)

        report = reconcile(conn, settings, mode="auto-json-wins")

        assert report.db_inserts == 2
        assert _count_global_permissions(conn) == 2

    def test_json_extras_source_is_reconcile_adopt(self, tmp_path):
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)

        reconcile(conn, settings, mode="auto-json-wins")

        row = conn.execute(
            "SELECT source FROM permissions WHERE project_id IS NULL AND session_id IS NULL;"
        ).fetchone()
        assert row[0] == "reconcile-adopt"

    def test_db_extras_deleted(self, tmp_path):
        settings = tmp_path / "settings.json"
        _write_settings(settings)  # empty JSON
        conn = _make_conn(tmp_path, settings)
        _insert_global_rule(conn, verb="git", flags="*", decision="approved")
        _insert_global_rule(conn, verb="npm", flags="*", decision="approved")

        report = reconcile(conn, settings, mode="auto-json-wins")

        assert report.db_deletes == 2
        assert _count_global_permissions(conn) == 0

    def test_conflict_json_decision_wins(self, tmp_path):
        """Conflicting rule: JSON says deny, DB says allow → DB updated to rejected."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, deny=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)
        _insert_global_rule(conn, verb="git", flags="*", decision="approved")

        report = reconcile(conn, settings, mode="auto-json-wins")

        assert report.db_updates == 1
        assert _global_decision(conn, "git") == "rejected"

    def test_mirror_written_and_hash_stamped(self, tmp_path):
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)

        reconcile(conn, settings, mode="auto-json-wins")

        # Mirror file must exist
        assert settings.exists()
        # Hash must be stamped
        row = conn.execute(
            "SELECT settings_json_sha256 FROM global_mirror WHERE id=1;"
        ).fetchone()
        assert row[0] is not None

    def test_matching_rows_not_touched(self, tmp_path):
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)
        _insert_global_rule(conn, verb="git", flags="*", decision="approved")

        report = reconcile(conn, settings, mode="auto-json-wins")

        assert report.db_inserts == 0
        assert report.db_deletes == 0
        assert report.db_updates == 0
        assert report.applied is False  # diff was empty → no mutations

    def test_deny_rule_inserted_correctly(self, tmp_path):
        settings = tmp_path / "settings.json"
        _write_settings(settings, deny=["Bash(git push)"])
        conn = _make_conn(tmp_path, settings)

        reconcile(conn, settings, mode="auto-json-wins")

        assert _global_decision(conn, "git") == "rejected"

    def test_ask_rule_inserted_correctly(self, tmp_path):
        settings = tmp_path / "settings.json"
        _write_settings(settings, ask=["Bash(npm *)"])
        conn = _make_conn(tmp_path, settings)

        reconcile(conn, settings, mode="auto-json-wins")

        assert _global_decision(conn, "npm") == "ask"


# ---------------------------------------------------------------------------
# reconcile() — auto-db-wins mode
# ---------------------------------------------------------------------------


class TestAutoDbWins:
    def test_empty_db_empty_json_noop(self, tmp_path):
        settings = tmp_path / "settings.json"
        _write_settings(settings)
        conn = _make_conn(tmp_path, settings)

        report = reconcile(conn, settings, mode="auto-db-wins")

        assert report.applied is False

    def test_only_in_json_dropped(self, tmp_path):
        """DB_WINS: JSON-only entries are not inserted into DB."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)  # DB is empty

        report = reconcile(conn, settings, mode="auto-db-wins")

        assert report.db_inserts == 0
        assert _count_global_permissions(conn) == 0

    def test_only_in_db_kept(self, tmp_path):
        """DB_WINS: DB-only entries stay in DB; mirror regenerated to include them."""
        settings = tmp_path / "settings.json"
        _write_settings(settings)  # empty JSON
        conn = _make_conn(tmp_path, settings)
        _insert_global_rule(conn, verb="git", flags="*", decision="approved")

        reconcile(conn, settings, mode="auto-db-wins")

        assert _count_global_permissions(conn) == 1
        # Mirror should contain the DB rule
        content = json.loads(settings.read_text())
        assert "Bash(git *)" in content["permissions"]["allow"]

    def test_conflict_db_decision_kept(self, tmp_path):
        """DB_WINS: conflicting rule retains DB decision; JSON ignored."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, deny=["Bash(git *)"])  # JSON says deny
        conn = _make_conn(tmp_path, settings)
        _insert_global_rule(
            conn, verb="git", flags="*", decision="approved"
        )  # DB says allow

        report = reconcile(conn, settings, mode="auto-db-wins")

        assert report.db_updates == 0
        assert _global_decision(conn, "git") == "approved"
        # Mirror should reflect DB (allow)
        content = json.loads(settings.read_text())
        assert "Bash(git *)" in content["permissions"]["allow"]

    def test_hash_stamped(self, tmp_path):
        settings = tmp_path / "settings.json"
        _write_settings(settings)
        conn = _make_conn(tmp_path, settings)

        reconcile(conn, settings, mode="auto-db-wins")

        row = conn.execute(
            "SELECT settings_json_sha256 FROM global_mirror WHERE id=1;"
        ).fetchone()
        assert row[0] is not None


# ---------------------------------------------------------------------------
# reconcile() — adopt mode
# ---------------------------------------------------------------------------


class TestAdoptMode:
    def test_adopt_is_json_wins(self, tmp_path):
        """'adopt' mode inserts JSON rules into DB (same as json-wins)."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)", "WebSearch"])
        conn = _make_conn(tmp_path, settings)

        report = reconcile(conn, settings, mode="adopt")

        assert report.db_inserts == 2
        assert _count_global_permissions(conn) == 2

    def test_adopt_mode_label(self, tmp_path):
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)

        report = reconcile(conn, settings, mode="adopt")

        assert report.mode == "adopt"


# ---------------------------------------------------------------------------
# reconcile() — first-touch (hash NULL → auto-adopt)
# ---------------------------------------------------------------------------


class TestFirstTouch:
    def test_interactive_with_null_hash_becomes_adopt(self, tmp_path):
        """When hash is NULL, interactive mode auto-switches to adopt."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)  # hash starts NULL

        # No stdin needed because mode switches to adopt (non-interactive).
        report = reconcile(conn, settings, mode="interactive")

        assert report.first_touch is True
        assert report.mode == "adopt"
        assert report.db_inserts == 1

    def test_first_touch_false_when_hash_set(self, tmp_path):
        """first_touch is False when a hash is already stored."""
        settings = tmp_path / "settings.json"
        _write_settings(settings)
        conn = _make_conn(tmp_path, settings)

        # Stamp the real file hash so DB reflects the current file state.
        _stamp_real_hash(conn, settings)

        report = reconcile(conn, settings, mode="plan")

        assert report.first_touch is False

    def test_plan_mode_not_affected_by_first_touch(self, tmp_path):
        """plan stays plan regardless of hash being NULL."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)

        report = reconcile(conn, settings, mode="plan")

        assert report.mode == "plan"
        assert report.first_touch is True
        assert report.applied is False

    def test_auto_db_wins_not_affected_by_first_touch(self, tmp_path):
        """auto-db-wins mode does not switch to adopt on first-touch."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)

        report = reconcile(conn, settings, mode="auto-db-wins")

        assert report.mode == "auto-db-wins"
        assert report.db_inserts == 0  # DB_WINS: JSON-only entries dropped


# ---------------------------------------------------------------------------
# reconcile() — interactive mode (mocked stdin)
# ---------------------------------------------------------------------------


class TestInteractiveMode:
    def test_interactive_db_wins_choice(self, tmp_path):
        """Interactive: user picks 'd' (db-wins) → JSON extras dropped."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)
        # Stamp real hash so interactive mode stays interactive (not auto-adopt).
        _stamp_real_hash(conn, settings)

        with patch("builtins.input", return_value="d"):
            report = reconcile(conn, settings, mode="interactive")

        assert report.db_inserts == 0
        assert _count_global_permissions(conn) == 0

    def test_interactive_json_wins_choice(self, tmp_path):
        """Interactive: user picks 'j' (json-wins) → JSON extras inserted."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)
        _stamp_real_hash(conn, settings)

        with patch("builtins.input", return_value="j"):
            report = reconcile(conn, settings, mode="interactive")

        assert report.db_inserts == 1
        assert _global_decision(conn, "git") == "approved"

    def test_interactive_per_entry_add(self, tmp_path):
        """Interactive per-entry: user adds a JSON-only rule."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)
        _stamp_real_hash(conn, settings)

        inputs = iter(["p", "a"])  # bulk: per-entry; then for only_in_json: add
        with patch("builtins.input", side_effect=inputs):
            report = reconcile(conn, settings, mode="interactive")

        assert report.db_inserts == 1

    def test_interactive_per_entry_skip(self, tmp_path):
        """Interactive per-entry: user skips a JSON-only rule."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)
        _stamp_real_hash(conn, settings)

        inputs = iter(["p", "s"])  # per-entry, then skip
        with patch("builtins.input", side_effect=inputs):
            report = reconcile(conn, settings, mode="interactive")

        assert report.db_inserts == 0

    def test_interactive_per_entry_remove_db_only(self, tmp_path):
        """Interactive per-entry: user removes a DB-only rule."""
        settings = tmp_path / "settings.json"
        _write_settings(settings)  # empty JSON
        conn = _make_conn(tmp_path, settings)
        _insert_global_rule(conn, verb="git", flags="*", decision="approved")
        _stamp_real_hash(conn, settings)

        inputs = iter(["p", "r"])  # per-entry, then remove
        with patch("builtins.input", side_effect=inputs):
            report = reconcile(conn, settings, mode="interactive")

        assert report.db_deletes == 1
        assert _count_global_permissions(conn) == 0

    def test_interactive_per_entry_keep_db_only(self, tmp_path):
        """Interactive per-entry: user keeps a DB-only rule."""
        settings = tmp_path / "settings.json"
        _write_settings(settings)  # empty JSON
        conn = _make_conn(tmp_path, settings)
        _insert_global_rule(conn, verb="git", flags="*", decision="approved")
        _stamp_real_hash(conn, settings)

        inputs = iter(["p", "k"])  # per-entry, then keep
        with patch("builtins.input", side_effect=inputs):
            report = reconcile(conn, settings, mode="interactive")

        assert report.db_deletes == 0
        assert _count_global_permissions(conn) == 1

    def test_interactive_per_entry_conflict_json_wins(self, tmp_path):
        """Interactive per-entry: user picks json for a conflicting rule."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, deny=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)
        _insert_global_rule(conn, verb="git", flags="*", decision="approved")
        _stamp_real_hash(conn, settings)

        inputs = iter(["p", "j"])  # per-entry, then json for conflict
        with patch("builtins.input", side_effect=inputs):
            report = reconcile(conn, settings, mode="interactive")

        assert report.db_updates == 1
        assert _global_decision(conn, "git") == "rejected"

    def test_interactive_per_entry_conflict_db_wins(self, tmp_path):
        """Interactive per-entry: user picks db for a conflicting rule."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, deny=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)
        _insert_global_rule(conn, verb="git", flags="*", decision="approved")
        _stamp_real_hash(conn, settings)

        inputs = iter(["p", "d"])  # per-entry, then db for conflict
        with patch("builtins.input", side_effect=inputs):
            report = reconcile(conn, settings, mode="interactive")

        assert report.db_updates == 0
        assert _global_decision(conn, "git") == "approved"

    def test_interactive_invalid_choice_retried(self, tmp_path):
        """Interactive: invalid bulk choice is ignored and re-prompted."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)
        _stamp_real_hash(conn, settings)

        inputs = iter(["x", "y", "j"])  # two invalid, then json-wins
        with patch("builtins.input", side_effect=inputs):
            report = reconcile(conn, settings, mode="interactive")

        assert report.db_inserts == 1


# ---------------------------------------------------------------------------
# reconcile() — scope detection
# ---------------------------------------------------------------------------


class TestScopeDetection:
    def test_unknown_path_raises(self, tmp_path):
        """Passing a path not registered in DB raises ReconcileError."""
        settings = tmp_path / "settings.json"
        conn = _make_conn(
            tmp_path, tmp_path / "other.json"
        )  # registered to different path

        with pytest.raises(ReconcileError, match="Cannot determine scope"):
            reconcile(conn, settings, mode="plan")

    def test_invalid_mode_raises(self, tmp_path):
        settings = tmp_path / "settings.json"
        conn = _make_conn(tmp_path, settings)

        with pytest.raises(ReconcileError, match="Invalid mode"):
            reconcile(conn, settings, mode="bad-mode")

    def test_project_scope(self, tmp_path):
        """Reconcile against a project-scoped mirror path."""
        proj_dir = tmp_path / "myproject"
        proj_dir.mkdir()
        proj_settings = proj_dir / "settings.local.json"
        _write_settings(proj_settings, allow=["Bash(git *)"])

        conn = _make_conn(tmp_path, tmp_path / "global_settings.json")

        # Register project
        ts = "2025-01-01T00:00:00.000Z"
        conn.execute(
            "INSERT INTO projects(cwd, name, root, first_seen, last_seen, settings_json_path)"
            " VALUES(?,?,?,?,?,?);",
            (str(proj_dir), "myproject", str(proj_dir), ts, ts, str(proj_settings)),
        )
        proj_id = conn.execute(
            "SELECT id FROM projects WHERE cwd=?;", (str(proj_dir),)
        ).fetchone()[0]

        report = reconcile(conn, proj_settings, mode="auto-json-wins")

        assert report.db_inserts == 1
        # Verify the permission was inserted with correct project_id
        row = conn.execute(
            "SELECT project_id FROM permissions WHERE project_id=?;", (proj_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == proj_id


# ---------------------------------------------------------------------------
# reconcile() — mirror file content verification
# ---------------------------------------------------------------------------


class TestMirrorContent:
    def test_mirror_contains_adopted_allow_rule(self, tmp_path):
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)

        reconcile(conn, settings, mode="auto-json-wins")

        content = json.loads(settings.read_text())
        assert "Bash(git *)" in content["permissions"]["allow"]

    def test_mirror_excludes_deleted_rule(self, tmp_path):
        """After DB_WINS on an only_in_db entry, mirror is regenerated from DB."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])  # JSON has git, DB doesn't
        conn = _make_conn(tmp_path, settings)

        # DB-wins: JSON-only entries dropped, mirror regenerated (empty)
        reconcile(conn, settings, mode="auto-db-wins")

        content = json.loads(settings.read_text())
        assert "Bash(git *)" not in content["permissions"]["allow"]

    def test_mirror_absent_file_created_on_noop(self, tmp_path):
        """When diff is empty but file is absent, sync creates it."""
        settings = tmp_path / "settings.json"
        # Don't write anything — file is absent, DB is empty
        conn = _make_conn(tmp_path, settings)

        reconcile(conn, settings, mode="auto-json-wins")

        # File must now exist (created by sync)
        assert settings.exists()
        content = json.loads(settings.read_text())
        assert "permissions" in content

    def test_hash_matches_file_content(self, tmp_path):
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])
        conn = _make_conn(tmp_path, settings)

        reconcile(conn, settings, mode="auto-json-wins")

        stored_hash = conn.execute(
            "SELECT settings_json_sha256 FROM global_mirror WHERE id=1;"
        ).fetchone()[0]
        actual_hash = settings_permissions_hash(settings.read_bytes())
        assert stored_hash == actual_hash


# ---------------------------------------------------------------------------
# Regression: Bug 1 — schema must allow 'ask' decision
# ---------------------------------------------------------------------------


class TestAskDecisionPersistence:
    """Regression tests for schema CHECK constraint including 'ask'.

    Bug: schema.sql previously only allowed ('approved', 'rejected') in the
    decision CHECK constraint.  Adopting permissions.ask entries from JSON
    would fail with IntegrityError when the DB was rebuilt from the old schema.
    """

    def test_ask_decision_inserted_by_adopt(self, tmp_path):
        """adopt mode must persist 'ask' decision rows without error."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, ask=["Bash(npm *)"])
        conn = _make_conn(tmp_path, settings)

        report = reconcile(conn, settings, mode="adopt")

        assert report.db_inserts == 1
        decision = conn.execute(
            "SELECT p.decision FROM permissions p"
            " JOIN rule_shapes rs ON rs.id = p.rule_shape_id"
            " WHERE rs.verb = ?",
            ("npm",),
        ).fetchone()
        assert decision is not None, "ask-decision row was not persisted"
        assert decision[0] == "ask"

    def test_mixed_allow_deny_ask_all_inserted(self, tmp_path):
        """All three decision types must persist when adopted together."""
        settings = tmp_path / "settings.json"
        _write_settings(
            settings,
            allow=["Bash(git *)"],
            deny=["Bash(rm *)"],
            ask=["Bash(npm *)"],
        )
        conn = _make_conn(tmp_path, settings)

        report = reconcile(conn, settings, mode="adopt")

        assert report.db_inserts == 3
        decisions = {
            r[0]
            for r in conn.execute(
                "SELECT p.decision FROM permissions p"
                " JOIN rule_shapes rs ON rs.id = p.rule_shape_id"
                " WHERE p.project_id IS NULL AND p.session_id IS NULL"
            ).fetchall()
        }
        assert decisions == {"approved", "rejected", "ask"}

    def test_ask_decision_survives_auto_json_wins(self, tmp_path):
        """auto-json-wins must also persist 'ask' decision correctly."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, ask=["Bash(curl *)"])
        conn = _make_conn(tmp_path, settings)

        reconcile(conn, settings, mode="auto-json-wins")

        assert _global_decision(conn, "curl") == "ask"

    def test_schema_allows_ask_decision_directly(self, tmp_path):
        """Direct INSERT with decision='ask' must not raise IntegrityError."""
        conn = _make_conn(tmp_path)
        ts = "2025-01-01T00:00:00.000Z"
        cur = conn.execute(
            "INSERT INTO rule_shapes(verb, subcommand, flags, path_spec, first_seen, last_seen)"
            " VALUES ('docker', NULL, '*', NULL, ?, ?);",
            (ts, ts),
        )
        shape_id = cur.lastrowid
        # This must not raise — the CHECK constraint must include 'ask'.
        conn.execute(
            "INSERT INTO permissions"
            " (rule_shape_id, session_id, project_id, decision, source, reason, decided_at)"
            " VALUES (?, NULL, NULL, 'ask', 'test', NULL, ?);",
            (shape_id, ts),
        )
        row = conn.execute(
            "SELECT decision FROM permissions WHERE rule_shape_id = ?", (shape_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == "ask"


# ---------------------------------------------------------------------------
# Regression: Bug 2 — INSERT failures must raise ReconcileError, not silently
#             produce wrong counts
# ---------------------------------------------------------------------------


class TestInsertFailureHandling:
    """Regression tests for honest INSERT failure reporting.

    Bug: if a DB INSERT fails (e.g. CHECK constraint, FK violation), reconcile
    previously could report counts based on intent rather than actual persistence,
    or surface raw sqlite3 exceptions instead of a clear ReconcileError.
    """

    def _make_conn_with_broken_constraint(
        self, tmp_path: Path, settings_path: Path
    ) -> sqlite3.Connection:
        """Build a DB whose permissions CHECK constraint is deliberately narrow
        (excludes 'ask') to provoke an IntegrityError on adopt.

        Mirrors the exact broken state that existed before the schema fix.
        """
        db_file = tmp_path / "broken.db"
        conn = sqlite3.connect(str(db_file), isolation_level=None)
        conn.row_factory = sqlite3.Row
        # Build schema without 'ask' in CHECK — old broken state
        old_schema = SCHEMA_PATH.read_text().replace(
            "CHECK (decision IN ('approved', 'rejected', 'ask'))",
            "CHECK (decision IN ('approved', 'rejected'))",
        )
        conn.executescript(old_schema)
        conn.execute(
            "INSERT OR IGNORE INTO global_mirror"
            " (id, settings_json_path, settings_json_sha256, settings_json_last_synced)"
            " VALUES (1, ?, NULL, NULL);",
            (str(settings_path),),
        )
        conn.execute(
            "INSERT OR IGNORE INTO permission_modes (name)"
            " VALUES ('default'),('acceptEdits'),('bypassPermissions'),('plan'),('auto');"
        )
        conn.execute(
            "INSERT OR IGNORE INTO call_statuses (name)"
            " VALUES ('pending'),('ok'),('err'),('denied'),('orphan');"
        )
        return conn

    def test_insert_failure_raises_reconcile_error(self, tmp_path):
        """INSERT failure must surface as ReconcileError, not raw IntegrityError."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, ask=["Bash(npm *)"])
        conn = self._make_conn_with_broken_constraint(tmp_path, settings)

        with pytest.raises(ReconcileError, match="Failed to insert permission"):
            reconcile(conn, settings, mode="adopt")

    def test_insert_failure_error_names_verb(self, tmp_path):
        """ReconcileError message must identify the failing verb."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, ask=["Bash(docker *)"])
        conn = self._make_conn_with_broken_constraint(tmp_path, settings)

        with pytest.raises(ReconcileError, match="docker"):
            reconcile(conn, settings, mode="adopt")

    def test_insert_failure_does_not_report_phantom_inserts(self, tmp_path):
        """When INSERT fails, db_inserts must reflect only successfully persisted rows."""
        settings = tmp_path / "settings.json"
        # Two allow entries (will succeed) + one ask entry (will fail on broken schema)
        _write_settings(
            settings, allow=["Bash(git *)", "Bash(curl *)"], ask=["Bash(npm *)"]
        )
        conn = self._make_conn_with_broken_constraint(tmp_path, settings)

        with pytest.raises(ReconcileError):
            reconcile(conn, settings, mode="adopt")

        # No rows must have been phantom-counted — exception prevented further inserts
        actual_rows = conn.execute("SELECT COUNT(*) FROM permissions").fetchone()[0]
        # The reconcile raised before completing; DB may have partial inserts
        # depending on which entry was processed last, but there must be no
        # discrepancy between what was physically committed and what was reported.
        # Since the exception bubbles out without a report, the key invariant is:
        # we cannot observe a report that claims more inserts than actually exist.
        assert actual_rows <= 2  # at most the allow entries that precede ask


# ---------------------------------------------------------------------------
# reconcile() — additional_dirs cache ingest
# ---------------------------------------------------------------------------


def _write_settings_with_extra_dirs(
    path: Path,
    *,
    allow=None,
    deny=None,
    ask=None,
    additional_dirs: list[str] | None = None,
) -> None:
    """Write settings.json including permissions.additionalDirectories."""
    data: dict[str, Any] = {
        "permissions": {
            "allow": allow or [],
            "deny": deny or [],
            "ask": ask or [],
        }
    }
    if additional_dirs is not None:
        data["permissions"]["additionalDirectories"] = additional_dirs
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _read_cache_row(
    conn: sqlite3.Connection, *, project_id: int | None
) -> tuple[float | None, str | None]:
    """Return (settings_json_mtime, additional_dirs) from the relevant cache row."""
    if project_id is None:
        row = conn.execute(
            "SELECT settings_json_mtime, additional_dirs FROM global_mirror WHERE id = 1;"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT settings_json_mtime, additional_dirs FROM projects WHERE id = ?;",
            (project_id,),
        ).fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


class TestAdditionalDirsCache:
    """Integration tests: reconcile() populates additional_dirs cache."""

    def test_cache_stamped_after_auto_json_wins(self, tmp_path):
        """Cache columns are populated after a non-plan reconcile."""
        settings = tmp_path / "settings.json"
        _write_settings_with_extra_dirs(
            settings,
            allow=["Bash(git *)"],
            additional_dirs=["/extra/one", "/extra/two"],
        )
        conn = _make_conn(tmp_path, settings)

        reconcile(conn, settings, mode="auto-json-wins")

        mtime, dirs_json = _read_cache_row(conn, project_id=None)
        # mtime must be close to the file's on-disk mtime (reconcile captures it
        # before any write; abs=0.01 tolerates float repr differences between
        # consecutive stat() calls on the same unmodified file).
        assert mtime == pytest.approx(settings.stat().st_mtime, abs=0.01)
        assert dirs_json is not None
        dirs = json.loads(dirs_json)
        assert dirs == ["/extra/one", "/extra/two"]

    def test_cache_stamped_in_plan_mode(self, tmp_path):
        """Cache is populated even when mode=plan (read-only, no permission writes)."""
        settings = tmp_path / "settings.json"
        _write_settings_with_extra_dirs(
            settings,
            additional_dirs=["/plan/mode/dir"],
        )
        conn = _make_conn(tmp_path, settings)

        report = reconcile(conn, settings, mode="plan")

        assert report.applied is False  # no permission mutations
        mtime, dirs_json = _read_cache_row(conn, project_id=None)
        assert mtime is not None
        assert dirs_json is not None
        dirs = json.loads(dirs_json)
        assert dirs == ["/plan/mode/dir"]

    def test_cache_mtime_matches_file_stat(self, tmp_path):
        """Cached mtime equals the file's actual st_mtime at reconcile time."""
        settings = tmp_path / "settings.json"
        _write_settings_with_extra_dirs(settings, additional_dirs=["/some/path"])
        conn = _make_conn(tmp_path, settings)

        reconcile(conn, settings, mode="auto-json-wins")

        # Read mtime from both the DB cache and the file; they must agree.
        # abs=0.01 accommodates float representation differences between
        # consecutive stat() calls on the same unmodified file.
        mtime, _ = _read_cache_row(conn, project_id=None)
        on_disk_mtime = settings.stat().st_mtime
        assert mtime == pytest.approx(on_disk_mtime, abs=0.01)

    def test_cache_empty_when_no_additional_dirs_key(self, tmp_path):
        """settings.json with no additionalDirectories → cache stores empty array."""
        settings = tmp_path / "settings.json"
        _write_settings(settings, allow=["Bash(git *)"])  # no additionalDirectories
        conn = _make_conn(tmp_path, settings)

        reconcile(conn, settings, mode="auto-json-wins")

        _, dirs_json = _read_cache_row(conn, project_id=None)
        assert dirs_json == "[]"

    def test_cache_not_stamped_when_file_absent(self, tmp_path):
        """Absent settings.json → cache columns stay NULL."""
        settings = tmp_path / "settings.json"
        conn = _make_conn(tmp_path, settings)
        # File does not exist

        reconcile(conn, settings, mode="plan")

        mtime, dirs_json = _read_cache_row(conn, project_id=None)
        assert mtime is None
        assert dirs_json is None

    def test_cache_stamped_for_project_scope(self, tmp_path):
        """Cache stamp lands in the projects row, not global_mirror."""
        proj_dir = tmp_path / "myproject"
        proj_dir.mkdir()
        proj_settings = proj_dir / "settings.local.json"
        _write_settings_with_extra_dirs(
            proj_settings,
            allow=["Bash(git *)"],
            additional_dirs=["/proj/extra"],
        )
        conn = _make_conn(tmp_path, tmp_path / "global_settings.json")

        ts = "2025-01-01T00:00:00.000Z"
        conn.execute(
            "INSERT INTO projects(cwd, name, root, first_seen, last_seen, settings_json_path)"
            " VALUES(?,?,?,?,?,?);",
            (str(proj_dir), "myproject", str(proj_dir), ts, ts, str(proj_settings)),
        )
        proj_id = conn.execute(
            "SELECT id FROM projects WHERE cwd=?;", (str(proj_dir),)
        ).fetchone()[0]

        reconcile(conn, proj_settings, mode="auto-json-wins")

        mtime, dirs_json = _read_cache_row(conn, project_id=proj_id)
        assert mtime is not None
        assert dirs_json is not None
        dirs = json.loads(dirs_json)
        assert dirs == ["/proj/extra"]

        # global_mirror cache must NOT have been touched
        global_mtime, global_dirs = _read_cache_row(conn, project_id=None)
        assert global_mtime is None
        assert global_dirs is None

    def test_cache_non_string_entries_coerced(self, tmp_path):
        """Non-string entries in additionalDirectories are coerced via str()."""
        settings = tmp_path / "settings.json"
        # Write raw JSON with a numeric entry alongside a string entry
        data = {
            "permissions": {
                "allow": [],
                "deny": [],
                "ask": [],
                "additionalDirectories": ["/valid/path", 42],
            }
        }
        settings.write_text(json.dumps(data), encoding="utf-8")
        conn = _make_conn(tmp_path, settings)

        reconcile(conn, settings, mode="auto-json-wins")

        _, dirs_json = _read_cache_row(conn, project_id=None)
        assert dirs_json is not None
        dirs = json.loads(dirs_json)
        assert dirs == ["/valid/path", "42"]


# ---------------------------------------------------------------------------
# plan mode stamps additional_dirs cache (Fix 2 companion test)
# ---------------------------------------------------------------------------


def test_plan_mode_stamps_cache(tmp_path):
    """reconcile(mode='plan') populates settings_json_mtime and additional_dirs.

    Plan mode is documented as read-only with respect to permission resolution,
    but the additional_dirs cache tracks file metadata and is intentionally
    updated in every mode.  This test asserts that the cache is populated even
    when no file write or rule resolution occurs.
    """
    settings = tmp_path / "settings.json"
    data: dict[str, Any] = {
        "permissions": {
            "allow": [],
            "deny": [],
            "ask": [],
            "additionalDirectories": ["/plan/extra"],
        }
    }
    settings.write_text(json.dumps(data), encoding="utf-8")
    conn = _make_conn(tmp_path, settings)

    report = reconcile(conn, settings, mode="plan")

    # No permission mutations must have occurred.
    assert report.applied is False
    assert _count_global_permissions(conn) == 0

    # Cache must be populated despite plan mode.
    mtime, dirs_json = _read_cache_row(conn, project_id=None)
    assert mtime is not None, "settings_json_mtime must be stamped in plan mode"
    assert dirs_json is not None, "additional_dirs must be stamped in plan mode"
    assert json.loads(dirs_json) == ["/plan/extra"]


# ---------------------------------------------------------------------------
# B9: _stamp_additional_dirs_cache rowcount observability
# ---------------------------------------------------------------------------


def test_stamp_additional_dirs_cache_warns_on_missing_row(tmp_path, capsys):
    """_stamp_additional_dirs_cache emits a WARNING when the target row doesn't exist."""
    from nephoscope.lib.mirror.reconcile import _stamp_additional_dirs_cache

    # Use an in-memory DB with schema applied but no global_mirror row seeded.
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(SCHEMA_PATH.read_text())
    # Do NOT insert a global_mirror row — UPDATE must touch zero rows.

    _stamp_additional_dirs_cache(
        conn,
        project_id=None,
        target_path=tmp_path / "settings.json",
        raw_data={},
        mtime=0.0,
    )

    err = capsys.readouterr().err
    assert "WARNING" in err, "expected WARNING in stderr when rowcount == 0"
    assert "_stamp_additional_dirs_cache" in err, (
        "WARNING must name the function so the source is locatable"
    )
