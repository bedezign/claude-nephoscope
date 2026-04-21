"""End-to-end tests for per-session auto-allow (v12).

Scenarios covered:
- First ask of a shape → hook emits ``ask`` + writes a pending row.
- Recorder Post (ok) → pending row is promoted into session_approvals.
- Second call of same shape+scope → hook emits ``allow`` (session-approved).
- Different scope (within vs outside project) → still asks, not auto-allowed.
- Status=err on Post → pending row dropped, NOT promoted.
- ask_pending survives idempotent re-asks.
"""

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
    """Invoke the permission hook's main() and return its emitted JSON."""
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


def _project_paths(tmp_path):
    """Set up a project root inside tmp_path and return (proj, inside_path)."""
    proj = tmp_path / "proj"
    _init_repo(proj)
    inside = proj / "file.py"
    inside.touch()
    return proj, inside


def _payload(tmp_path, command, proj, tool_use_id, session="sess-approve-1"):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(proj),
        "session_id": session,
        "tool_use_id": tool_use_id,
        "permission_mode": "default",
    }


def test_first_ask_writes_pending_row(tmp_db, recorder, tmp_path, monkeypatch, capsys):
    proj, inside = _project_paths(tmp_path)
    payload = _payload(tmp_path, f"rm {inside}", proj, "toolu_ask_1")

    # Recorder runs first (records the call + scope_id).
    recorder._handle("pre", payload)
    # Hook fires second; emits ask and writes pending row.
    result = _run_hook(payload, monkeypatch, capsys)

    assert result["hookSpecificOutput"]["permissionDecision"] == "ask"
    rows = tmp_db.execute(
        "SELECT session_id, command_shape_id, scope_id FROM permission_ask_pending"
        " WHERE tool_use_id = ?;",
        (payload["tool_use_id"],),
    ).fetchall()
    assert len(rows) == 1


def test_ok_promotes_pending_to_session_approval(
    tmp_db, recorder, tmp_path, monkeypatch, capsys
):
    proj, inside = _project_paths(tmp_path)
    payload = _payload(tmp_path, f"rm {inside}", proj, "toolu_ok_1")

    recorder._handle("pre", payload)
    _run_hook(payload, monkeypatch, capsys)

    # Simulate successful tool execution.
    recorder._handle(
        "post",
        {
            "tool_name": "Bash",
            "tool_input": payload["tool_input"],
            "cwd": payload["cwd"],
            "session_id": payload["session_id"],
            "tool_use_id": payload["tool_use_id"],
            "tool_response": {"stdout": "ok"},
        },
    )

    pending = tmp_db.execute(
        "SELECT 1 FROM permission_ask_pending WHERE tool_use_id = ?;",
        (payload["tool_use_id"],),
    ).fetchall()
    assert pending == []

    approved = tmp_db.execute(
        "SELECT 1 FROM permission_session_approvals psa"
        " JOIN sessions s ON s.id = psa.session_id"
        " WHERE s.session_uuid = ?;",
        (payload["session_id"],),
    ).fetchall()
    assert len(approved) == 1


def test_second_call_after_approval_auto_allows(
    tmp_db, recorder, tmp_path, monkeypatch, capsys
):
    proj, inside = _project_paths(tmp_path)
    first = _payload(tmp_path, f"rm {inside}", proj, "toolu_first_1")
    recorder._handle("pre", first)
    _run_hook(first, monkeypatch, capsys)
    recorder._handle(
        "post",
        {**first, "tool_response": {"stdout": "done"}},
    )

    # Second call — different path but same shape (rm, None, {}) and same
    # scope (within_project). Should auto-allow.
    inside2 = proj / "other.py"
    inside2.touch()
    second = _payload(tmp_path, f"rm {inside2}", proj, "toolu_second_1")
    recorder._handle("pre", second)
    result = _run_hook(second, monkeypatch, capsys)

    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_different_scope_still_asks(tmp_db, recorder, tmp_path, monkeypatch, capsys):
    proj, inside = _project_paths(tmp_path)
    # Approve rm within_project first.
    first = _payload(tmp_path, f"rm {inside}", proj, "toolu_scope_1")
    recorder._handle("pre", first)
    _run_hook(first, monkeypatch, capsys)
    recorder._handle(
        "post",
        {**first, "tool_response": {"stdout": "ok"}},
    )

    # Now try rm with an outside-project path. Scope changes to outside;
    # approval doesn't transfer. Should still emit ask.
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    outside = outside_dir / "f.txt"
    outside.touch()
    second = _payload(tmp_path, f"rm {outside}", proj, "toolu_scope_2")
    recorder._handle("pre", second)
    result = _run_hook(second, monkeypatch, capsys)

    assert result["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_err_drops_pending_without_promotion(
    tmp_db, recorder, tmp_path, monkeypatch, capsys
):
    proj, inside = _project_paths(tmp_path)
    payload = _payload(tmp_path, f"rm {inside}", proj, "toolu_err_1")
    recorder._handle("pre", payload)
    _run_hook(payload, monkeypatch, capsys)

    # Post with is_error=True → status=err; pending should be dropped, no
    # promotion.
    recorder._handle(
        "post",
        {
            **payload,
            "tool_response": {"is_error": True, "stderr": "no such file"},
        },
    )

    pending = tmp_db.execute(
        "SELECT 1 FROM permission_ask_pending WHERE tool_use_id = ?;",
        (payload["tool_use_id"],),
    ).fetchall()
    assert pending == []

    approved = tmp_db.execute(
        "SELECT 1 FROM permission_session_approvals"
        " JOIN sessions ON sessions.id = permission_session_approvals.session_id"
        " WHERE sessions.session_uuid = ?;",
        (payload["session_id"],),
    ).fetchall()
    assert approved == []


def test_compound_command_requires_all_leaves_approved(
    tmp_db, recorder, tmp_path, monkeypatch, capsys
):
    proj, inside = _project_paths(tmp_path)
    inside2 = proj / "b.py"
    inside2.touch()

    # First: rm + mv together — both ask-tier.
    first = _payload(
        tmp_path, f"rm {inside} && mv {inside2} {proj}/c.py", proj, "toolu_compound_1"
    )
    recorder._handle("pre", first)
    result1 = _run_hook(first, monkeypatch, capsys)
    assert result1["hookSpecificOutput"]["permissionDecision"] == "ask"

    # Approving (status=ok) promotes BOTH leaves into session_approvals.
    recorder._handle(
        "post",
        {**first, "tool_response": {"stdout": "ok"}},
    )

    # Second identical-shape compound — both shapes approved → allow.
    inside3 = proj / "d.py"
    inside3.touch()
    inside4 = proj / "e.py"
    inside4.touch()
    second = _payload(
        tmp_path, f"rm {inside3} && mv {inside4} {proj}/f.py", proj, "toolu_compound_2"
    )
    recorder._handle("pre", second)
    result2 = _run_hook(second, monkeypatch, capsys)
    assert result2["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_gc_drops_stale_sessions(tmp_db, recorder, tmp_path, monkeypatch, capsys):
    from lib import gc_sessions

    proj, inside = _project_paths(tmp_path)
    payload = _payload(tmp_path, f"rm {inside}", proj, "toolu_gc_1")
    recorder._handle("pre", payload)
    _run_hook(payload, monkeypatch, capsys)
    recorder._handle(
        "post",
        {**payload, "tool_response": {"stdout": "ok"}},
    )

    # Backdate the session's last_activity by 10 days.
    tmp_db.execute(
        "UPDATE sessions SET last_activity = '2020-01-01T00:00:00.000Z'"
        " WHERE session_uuid = ?;",
        (payload["session_id"],),
    )
    tmp_db.commit()

    counts = gc_sessions.sweep(tmp_db, session_idle_days=7, ask_pending_hours=1)
    assert counts["permission_session_approvals"] >= 1

    remaining = tmp_db.execute("SELECT 1 FROM permission_session_approvals;").fetchall()
    assert remaining == []


def test_gc_drops_orphan_pending(tmp_db, recorder, tmp_path, monkeypatch, capsys):
    from lib import gc_sessions

    proj, inside = _project_paths(tmp_path)
    payload = _payload(tmp_path, f"rm {inside}", proj, "toolu_orphan_pending_1")
    recorder._handle("pre", payload)
    _run_hook(payload, monkeypatch, capsys)
    # Intentionally skip Post — simulates a user-denied ask.

    # Backdate the pending row by 2 hours.
    tmp_db.execute(
        "UPDATE permission_ask_pending SET asked_at = '2020-01-01T00:00:00.000Z'"
        " WHERE tool_use_id = ?;",
        (payload["tool_use_id"],),
    )
    tmp_db.commit()

    counts = gc_sessions.sweep(tmp_db, session_idle_days=7, ask_pending_hours=1)
    assert counts["permission_ask_pending"] >= 1


def test_approval_scoped_to_session(tmp_db, recorder, tmp_path, monkeypatch, capsys):
    # Session A approves rm; session B in same cwd should still ask.
    proj, inside = _project_paths(tmp_path)
    a = _payload(tmp_path, f"rm {inside}", proj, "toolu_sess_a", session="sess-A")
    recorder._handle("pre", a)
    _run_hook(a, monkeypatch, capsys)
    recorder._handle(
        "post",
        {**a, "tool_response": {"stdout": "ok"}},
    )

    inside_b = proj / "b.py"
    inside_b.touch()
    b = _payload(tmp_path, f"rm {inside_b}", proj, "toolu_sess_b", session="sess-B")
    recorder._handle("pre", b)
    result = _run_hook(b, monkeypatch, capsys)
    assert result["hookSpecificOutput"]["permissionDecision"] == "ask"
