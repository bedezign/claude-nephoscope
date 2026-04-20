"""Tests for the runtime PreToolUse gate (``learners.permission.hook``).

The hook is a pure stdin→stdout script: feed a JSON payload, capture the
emitted JSON, assert the decision. We drive it by calling ``main()`` with
stdin monkey-patched to an ``io.StringIO`` and capture stdout via pytest's
``capsys`` — faster than spawning a subprocess per case and keeps the v5
schema fixtures (the ``tmp_db`` environment) in scope.

The fixture sets ``OBSERVABILITY_DB`` to a fresh migrated DB; the hook
resolves its DB path through the same env var via a module-local helper,
so every test sees its own isolated state.

Several tests pre-insert into ``command_shapes`` + ``permission_active``
so the active-allowlist branch can be exercised without running the full
learner scan. Shape fields must match the canonical form the learner would
produce (minified flags JSON, sorted) — see ``_flags_json``.
"""
from __future__ import annotations

import importlib
import io
import json

from lib.db import minify_json


def _flags_json(flags: list[str]) -> str:
    """Stored flags are sorted + minified; tests mirror that convention."""
    return minify_json(sorted(flags))


def _insert_active(conn, verb: str, subcommand: str | None, flags: list[str]) -> int:
    """Seed an entry in ``command_shapes`` + ``permission_active``.

    Returns the ``command_shape_id`` so tests can reference it directly.
    Uses a fixed timestamp — the hook doesn't read them, just the join
    predicate.
    """
    ts = "2026-04-20T00:00:00.000Z"
    cur = conn.execute(
        """
        INSERT INTO command_shapes(verb, subcommand, flags, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?);
        """,
        (verb, subcommand, _flags_json(flags), ts, ts),
    )
    shape_id = int(cur.lastrowid or 0)
    conn.execute(
        """
        INSERT INTO permission_active(command_shape_id, promoted_at, source)
        VALUES (?, ?, 'manual');
        """,
        (shape_id, ts),
    )
    return shape_id


def _run_hook(payload: dict, monkeypatch, capsys) -> dict:
    """Invoke the hook's ``main()`` with a stdin payload and return its JSON."""
    # Reload the hook module so the DB-path helper resolves the freshly
    # patched OBSERVABILITY_DB (fixtures set the env var each test).
    import learners.permission.hook as hook

    hook = importlib.reload(hook)

    raw = json.dumps(payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    rc = hook.main()
    out = capsys.readouterr().out
    assert rc == 0
    return json.loads(out)


def test_non_bash_tool_falls_through(tmp_db, monkeypatch, capsys):
    result = _run_hook(
        {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}},
        monkeypatch,
        capsys,
    )
    assert result == {}


def test_empty_command_falls_through(tmp_db, monkeypatch, capsys):
    # Missing command key — tool_input is the empty dict.
    result = _run_hook(
        {"tool_name": "Bash", "tool_input": {}}, monkeypatch, capsys
    )
    assert result == {}
    # Whitespace-only command.
    result2 = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "   "}},
        monkeypatch,
        capsys,
    )
    assert result2 == {}


def test_unparseable_command_falls_through(tmp_db, monkeypatch, capsys):
    # A dangling heredoc token bashlex cannot parse; canonicalize returns [].
    result = _run_hook(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "cat <<EOF\nunclosed"},
        },
        monkeypatch,
        capsys,
    )
    assert result == {}


def test_deny_fires_on_verb(tmp_db, monkeypatch, capsys):
    result = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "dd if=/dev/zero of=/dev/sda"}},
        monkeypatch,
        capsys,
    )
    spec = result.get("hookSpecificOutput") or {}
    assert spec.get("permissionDecision") == "deny"
    assert spec.get("hookEventName") == "PreToolUse"
    assert "dd" in (spec.get("permissionDecisionReason") or "")


def test_deny_fires_on_subcommand(tmp_db, monkeypatch, capsys):
    result = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "systemctl reboot"}},
        monkeypatch,
        capsys,
    )
    spec = result.get("hookSpecificOutput") or {}
    assert spec.get("permissionDecision") == "deny"
    reason = (spec.get("permissionDecisionReason") or "").lower()
    assert "reboot" in reason


def test_ask_fires_on_rm(tmp_db, monkeypatch, capsys):
    result = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "rm /tmp/scratch"}},
        monkeypatch,
        capsys,
    )
    spec = result.get("hookSpecificOutput") or {}
    assert spec.get("permissionDecision") == "ask"
    assert "rm" in (spec.get("permissionDecisionReason") or "")


def test_ask_fires_on_git_push(tmp_db, monkeypatch, capsys):
    result = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}},
        monkeypatch,
        capsys,
    )
    spec = result.get("hookSpecificOutput") or {}
    assert spec.get("permissionDecision") == "ask"
    assert "push" in (spec.get("permissionDecisionReason") or "").lower()


def test_deny_beats_ask_on_compound_command(tmp_db, monkeypatch, capsys):
    # Mix an ask-tier leaf with a deny-tier leaf. Deny must win.
    result = _run_hook(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "rm /tmp/x; dd if=/dev/zero of=/dev/sda"},
        },
        monkeypatch,
        capsys,
    )
    spec = result.get("hookSpecificOutput") or {}
    assert spec.get("permissionDecision") == "deny"


def test_allow_fires_on_single_active_leaf(tmp_db, monkeypatch, capsys):
    _insert_active(tmp_db, "git", "status", [])
    result = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "git status"}},
        monkeypatch,
        capsys,
    )
    spec = result.get("hookSpecificOutput") or {}
    assert spec.get("permissionDecision") == "allow"


def test_allow_requires_all_leaves_active(tmp_db, monkeypatch, capsys):
    # Only `git status` is active — `ls` has no active row, so the compound
    # command must fall through (not allow).
    _insert_active(tmp_db, "git", "status", [])
    result = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "git status; ls"}},
        monkeypatch,
        capsys,
    )
    assert result == {}


def test_allow_fires_when_all_multi_leaves_active(tmp_db, monkeypatch, capsys):
    _insert_active(tmp_db, "git", "status", [])
    _insert_active(tmp_db, "ls", None, [])
    result = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "git status; ls"}},
        monkeypatch,
        capsys,
    )
    spec = result.get("hookSpecificOutput") or {}
    assert spec.get("permissionDecision") == "allow"


def test_flags_key_matches_minified_learner_format(tmp_db, monkeypatch, capsys):
    # This is the regression for the original `json.dumps` default-separators
    # bug: the learner stores `["-a","-l"]` (minified) and the hook must
    # produce the same byte string when it queries. `ls -la` canonicalizes
    # to `(ls, None, {-a, -l})`.
    _insert_active(tmp_db, "ls", None, ["-a", "-l"])
    result = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "ls -la"}},
        monkeypatch,
        capsys,
    )
    spec = result.get("hookSpecificOutput") or {}
    assert spec.get("permissionDecision") == "allow"


def test_deny_marks_pending_row_as_denied(tmp_db, monkeypatch, capsys):
    tool_use_id = "probe-deny-use-id-001"
    pending_id = tmp_db.execute(
        "SELECT id FROM call_statuses WHERE name='pending';"
    ).fetchone()[0]
    cur = tmp_db.execute(
        "INSERT INTO tools (name) VALUES ('Bash') RETURNING id;"
    )
    tool_id = int(cur.fetchone()[0])
    tmp_db.execute(
        """
        INSERT INTO tool_calls
          (ts, tool_use_id, status_id, tool_id, completed_ts)
        VALUES (?, ?, ?, ?, NULL);
        """,
        ("2026-04-20T12:00:00.000Z", tool_use_id, pending_id, tool_id),
    )

    result = _run_hook(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "dd if=/dev/zero of=/dev/sda"},
            "tool_use_id": tool_use_id,
        },
        monkeypatch,
        capsys,
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    row = tmp_db.execute(
        """
        SELECT cs.name, tc.completed_ts
          FROM tool_calls tc
          JOIN call_statuses cs ON cs.id = tc.status_id
         WHERE tc.tool_use_id = ?;
        """,
        (tool_use_id,),
    ).fetchone()
    assert row[0] == "denied"
    assert row[1] is not None


def test_deny_without_tool_use_id_still_denies(tmp_db, monkeypatch, capsys):
    result = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "dd if=/dev/zero of=/dev/sda"}},
        monkeypatch,
        capsys,
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_missing_db_falls_through(tmp_path, monkeypatch, capsys):
    # Bypass the tmp_db fixture (which creates a DB) — point at a path
    # that does not exist so the hook hits its `db.is_file()` guard.
    missing = tmp_path / "does-not-exist.db"
    monkeypatch.setenv("OBSERVABILITY_DB", str(missing))

    result = _run_hook(
        {"tool_name": "Bash", "tool_input": {"command": "git status"}},
        monkeypatch,
        capsys,
    )
    assert result == {}
