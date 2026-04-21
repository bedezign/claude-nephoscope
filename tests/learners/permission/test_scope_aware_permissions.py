"""End-to-end Wave 3 tests: scope-aware permission_active + permission_rejected."""

from __future__ import annotations

import importlib
import io
import json
import subprocess

import pytest


@pytest.fixture
def recorder(tmp_db):
    import recorder.run as run_module

    importlib.reload(run_module)
    return run_module


def _run_hook(payload: dict, monkeypatch, capsys) -> dict:
    import learners.permission.hook as hook

    hook = importlib.reload(hook)
    raw = json.dumps(payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    rc = hook.main()
    out = capsys.readouterr().out
    assert rc == 0
    return json.loads(out)


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)


def _payload(proj, command, tool_use_id, session="sess-scope-1"):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(proj),
        "session_id": session,
        "tool_use_id": tool_use_id,
        "permission_mode": "default",
    }


def _upsert_shape(conn, verb: str, subcommand: str | None, flags: list[str]) -> int:
    flags_json = json.dumps(sorted(flags), ensure_ascii=False, separators=(",", ":"))
    cur = conn.execute(
        """
        INSERT INTO command_shapes(verb, subcommand, flags, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?);
        """,
        (
            verb,
            subcommand,
            flags_json,
            "2026-04-20T00:00:00.000Z",
            "2026-04-20T00:00:00.000Z",
        ),
    )
    return int(cur.lastrowid or 0)


def _scope_id(conn, name: str) -> int:
    return int(
        conn.execute(
            "SELECT id FROM tool_call_scopes WHERE name = ?;", (name,)
        ).fetchone()[0]
    )


# ---------------------------------------------------------------------------
# permission_active scope matching
# ---------------------------------------------------------------------------


def test_active_any_scope_matches_within(
    tmp_db, recorder, tmp_path, monkeypatch, capsys
):
    proj = tmp_path / "proj"
    _init_repo(proj)
    inside = proj / "f.py"
    inside.touch()

    # Promote rm for scope=any.
    shape_id = _upsert_shape(tmp_db, "rm", None, [])
    tmp_db.execute(
        "INSERT INTO permission_active(command_shape_id, scope_id, promoted_at, source)"
        " VALUES (?, ?, ?, 'manual');",
        (shape_id, _scope_id(tmp_db, "any"), "2026-04-20T00:00:00.000Z"),
    )
    tmp_db.commit()

    payload = _payload(proj, f"rm {inside}", "toolu_active_any_1")
    recorder._handle("pre", payload)
    result = _run_hook(payload, monkeypatch, capsys)
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_active_within_only_matches_within_not_outside(
    tmp_db, recorder, tmp_path, monkeypatch, capsys
):
    proj = tmp_path / "proj"
    _init_repo(proj)
    inside = proj / "f.py"
    inside.touch()
    outside_dir = tmp_path / "else"
    outside_dir.mkdir()
    outside = outside_dir / "f.py"
    outside.touch()

    # Promote rm ONLY for within_project.
    shape_id = _upsert_shape(tmp_db, "rm", None, [])
    tmp_db.execute(
        "INSERT INTO permission_active(command_shape_id, scope_id, promoted_at, source)"
        " VALUES (?, ?, ?, 'manual');",
        (shape_id, _scope_id(tmp_db, "within_project"), "2026-04-20T00:00:00.000Z"),
    )
    tmp_db.commit()

    # Within → allow.
    p1 = _payload(proj, f"rm {inside}", "toolu_within_1")
    recorder._handle("pre", p1)
    r1 = _run_hook(p1, monkeypatch, capsys)
    assert r1["hookSpecificOutput"]["permissionDecision"] == "allow"

    # Outside → still ask (no matching scope).
    p2 = _payload(proj, f"rm {outside}", "toolu_outside_1")
    recorder._handle("pre", p2)
    r2 = _run_hook(p2, monkeypatch, capsys)
    assert r2["hookSpecificOutput"]["permissionDecision"] == "ask"


# ---------------------------------------------------------------------------
# permission_rejected as runtime deny
# ---------------------------------------------------------------------------


def test_rejected_any_is_hard_deny(tmp_db, recorder, tmp_path, monkeypatch, capsys):
    proj = tmp_path / "proj"
    _init_repo(proj)
    inside = proj / "f.py"
    inside.touch()

    shape_id = _upsert_shape(tmp_db, "rm", None, [])
    tmp_db.execute(
        "INSERT INTO permission_rejected(command_shape_id, scope_id, rejected_at, reason)"
        " VALUES (?, ?, ?, 'user said no');",
        (shape_id, _scope_id(tmp_db, "any"), "2026-04-20T00:00:00.000Z"),
    )
    tmp_db.commit()

    payload = _payload(proj, f"rm {inside}", "toolu_rejected_any_1")
    recorder._handle("pre", payload)
    result = _run_hook(payload, monkeypatch, capsys)

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "rejected" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_rejected_outside_only_blocks_outside(
    tmp_db, recorder, tmp_path, monkeypatch, capsys
):
    proj = tmp_path / "proj"
    _init_repo(proj)
    inside = proj / "f.py"
    inside.touch()
    outside_dir = tmp_path / "else"
    outside_dir.mkdir()
    outside = outside_dir / "f.py"
    outside.touch()

    shape_id = _upsert_shape(tmp_db, "rm", None, [])
    tmp_db.execute(
        "INSERT INTO permission_rejected(command_shape_id, scope_id, rejected_at, reason)"
        " VALUES (?, ?, ?, 'never outside');",
        (shape_id, _scope_id(tmp_db, "outside_project"), "2026-04-20T00:00:00.000Z"),
    )
    tmp_db.commit()

    # Outside → deny.
    p_out = _payload(proj, f"rm {outside}", "toolu_rej_out_1")
    recorder._handle("pre", p_out)
    r_out = _run_hook(p_out, monkeypatch, capsys)
    assert r_out["hookSpecificOutput"]["permissionDecision"] == "deny"

    # Within → falls through to ask (scope doesn't match rejection).
    p_in = _payload(proj, f"rm {inside}", "toolu_rej_in_1")
    recorder._handle("pre", p_in)
    r_in = _run_hook(p_in, monkeypatch, capsys)
    assert r_in["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_rejected_beats_active(tmp_db, recorder, tmp_path, monkeypatch, capsys):
    # If a shape is both actively promoted AND rejected for the same call's
    # scope, rejected must win — user rejection is authoritative.
    proj = tmp_path / "proj"
    _init_repo(proj)
    inside = proj / "f.py"
    inside.touch()

    shape_id = _upsert_shape(tmp_db, "rm", None, [])
    tmp_db.execute(
        "INSERT INTO permission_active(command_shape_id, scope_id, promoted_at, source)"
        " VALUES (?, ?, ?, 'manual');",
        (shape_id, _scope_id(tmp_db, "any"), "2026-04-20T00:00:00.000Z"),
    )
    tmp_db.execute(
        "INSERT INTO permission_rejected(command_shape_id, scope_id, rejected_at, reason)"
        " VALUES (?, ?, ?, NULL);",
        (shape_id, _scope_id(tmp_db, "any"), "2026-04-20T00:00:00.000Z"),
    )
    tmp_db.commit()

    payload = _payload(proj, f"rm {inside}", "toolu_rej_beats_active_1")
    recorder._handle("pre", payload)
    result = _run_hook(payload, monkeypatch, capsys)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_rejected_beats_session_approval(
    tmp_db, recorder, tmp_path, monkeypatch, capsys
):
    # Session approvals also can't override a runtime rejection.
    proj = tmp_path / "proj"
    _init_repo(proj)
    inside = proj / "f.py"
    inside.touch()

    # Seed: session-approve rm within_project for this session.
    shape_id = _upsert_shape(tmp_db, "rm", None, [])
    # Need a session row to FK into.
    recorder._handle("pre", _payload(proj, f"rm {inside}", "toolu_seed_1"))
    session_row = tmp_db.execute(
        "SELECT id FROM sessions WHERE session_uuid = 'sess-scope-1';"
    ).fetchone()
    assert session_row is not None
    within_id = _scope_id(tmp_db, "within_project")
    tmp_db.execute(
        "INSERT INTO permission_session_approvals"
        " (session_id, command_shape_id, scope_id, approved_at)"
        " VALUES (?, ?, ?, ?);",
        (session_row[0], shape_id, within_id, "2026-04-20T00:00:00.000Z"),
    )
    # Now reject within_project.
    tmp_db.execute(
        "INSERT INTO permission_rejected(command_shape_id, scope_id, rejected_at, reason)"
        " VALUES (?, ?, ?, NULL);",
        (shape_id, within_id, "2026-04-20T00:00:00.000Z"),
    )
    tmp_db.commit()

    payload = _payload(proj, f"rm {inside}", "toolu_rej_beats_session_1")
    recorder._handle("pre", payload)
    result = _run_hook(payload, monkeypatch, capsys)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# Learner candidacy with scope-qualified rejection
# ---------------------------------------------------------------------------


def test_scope_qualified_rejection_does_not_kill_candidacy(tmp_db):
    """A rejection with scope=outside_project should NOT block candidacy; the
    shape can still be proposed for other scopes. Only scope=any rejections
    kill candidacy outright (_is_rejected check).
    """
    from learners.permission import learner as learner_module

    # Seed a candidate via direct DB insert (faster than going through scan).
    shape_id = _upsert_shape(tmp_db, "someverb", None, [])
    tmp_db.execute(
        """
        INSERT INTO permission_candidates
          (command_shape_id, observations, distinct_sessions, first_seen, last_seen)
        VALUES (?, 10, 5, ?, ?);
        """,
        (shape_id, "2026-04-20T00:00:00.000Z", "2026-04-20T00:00:00.000Z"),
    )
    # Scope-qualified rejection — should NOT disqualify from propose.
    tmp_db.execute(
        "INSERT INTO permission_rejected(command_shape_id, scope_id, rejected_at, reason)"
        " VALUES (?, ?, ?, NULL);",
        (shape_id, _scope_id(tmp_db, "outside_project"), "2026-04-20T00:00:00.000Z"),
    )
    tmp_db.commit()

    proposals = learner_module.propose_promotions(tmp_db)
    assert any(c.verb == "someverb" for c in proposals)


def test_any_rejection_kills_candidacy(tmp_db):
    from learners.permission import learner as learner_module

    shape_id = _upsert_shape(tmp_db, "otherverb", None, [])
    tmp_db.execute(
        """
        INSERT INTO permission_candidates
          (command_shape_id, observations, distinct_sessions, first_seen, last_seen)
        VALUES (?, 10, 5, ?, ?);
        """,
        (shape_id, "2026-04-20T00:00:00.000Z", "2026-04-20T00:00:00.000Z"),
    )
    tmp_db.execute(
        "INSERT INTO permission_rejected(command_shape_id, scope_id, rejected_at, reason)"
        " VALUES (?, ?, ?, NULL);",
        (shape_id, _scope_id(tmp_db, "any"), "2026-04-20T00:00:00.000Z"),
    )
    tmp_db.commit()

    proposals = learner_module.propose_promotions(tmp_db)
    assert not any(c.verb == "otherverb" for c in proposals)
