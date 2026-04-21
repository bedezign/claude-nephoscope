"""Recorder writes tool_calls.scope_id correctly per path inputs."""

from __future__ import annotations

import importlib
import subprocess

import pytest


@pytest.fixture
def recorder(tmp_db):
    import recorder.run as run_module

    importlib.reload(run_module)
    return run_module


def _pre_payload(tmp_path, cwd=None, **overrides):
    base = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "cwd": cwd or str(tmp_path / "proj"),
        "session_id": "sess-scope-1",
        "tool_use_id": f"toolu_{overrides.get('tool_use_id', 'scope_1')}",
        "permission_mode": "default",
    }
    base.update(overrides)
    return base


def _scope_name_for_last_row(conn, scope_id):
    if scope_id is None:
        return None
    row = conn.execute(
        "SELECT name FROM tool_call_scopes WHERE id = ?;", (scope_id,)
    ).fetchone()
    return row[0] if row is not None else None


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)


def test_bash_no_positional_paths_is_no_path(tmp_db, recorder, tmp_path):
    _init_repo(tmp_path / "proj")
    payload = _pre_payload(tmp_path, tool_input={"command": "ls -la"})
    recorder._handle("pre", payload)

    row = tmp_db.execute(
        "SELECT scope_id FROM tool_calls WHERE tool_use_id = ?;",
        (payload["tool_use_id"],),
    ).fetchone()
    assert _scope_name_for_last_row(tmp_db, row[0]) == "no_path"


def test_bash_with_path_inside_project_is_within(tmp_db, recorder, tmp_path):
    proj = tmp_path / "proj"
    _init_repo(proj)
    inside = proj / "src" / "file.py"
    inside.parent.mkdir(parents=True)
    inside.touch()
    payload = _pre_payload(
        tmp_path,
        tool_input={"command": f"rm {inside}"},
    )
    recorder._handle("pre", payload)

    row = tmp_db.execute(
        "SELECT scope_id FROM tool_calls WHERE tool_use_id = ?;",
        (payload["tool_use_id"],),
    ).fetchone()
    assert _scope_name_for_last_row(tmp_db, row[0]) == "within_project"


def test_bash_with_path_outside_project_is_outside(tmp_db, recorder, tmp_path):
    proj = tmp_path / "proj"
    _init_repo(proj)
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    outside = outside_dir / "file.py"
    outside.touch()
    payload = _pre_payload(
        tmp_path,
        tool_input={"command": f"rm {outside}"},
    )
    recorder._handle("pre", payload)

    row = tmp_db.execute(
        "SELECT scope_id FROM tool_calls WHERE tool_use_id = ?;",
        (payload["tool_use_id"],),
    ).fetchone()
    assert _scope_name_for_last_row(tmp_db, row[0]) == "outside_project"


def test_bash_with_mixed_paths_is_mixed(tmp_db, recorder, tmp_path):
    proj = tmp_path / "proj"
    _init_repo(proj)
    inside = proj / "in.py"
    outside = tmp_path / "out.py"
    inside.touch()
    outside.touch()
    payload = _pre_payload(
        tmp_path,
        tool_input={"command": f"mv {inside} {outside}"},
    )
    recorder._handle("pre", payload)

    row = tmp_db.execute(
        "SELECT scope_id FROM tool_calls WHERE tool_use_id = ?;",
        (payload["tool_use_id"],),
    ).fetchone()
    assert _scope_name_for_last_row(tmp_db, row[0]) == "mixed"


def test_read_tool_uses_file_path_field(tmp_db, recorder, tmp_path):
    proj = tmp_path / "proj"
    _init_repo(proj)
    inside = proj / "a.py"
    inside.touch()
    payload = _pre_payload(
        tmp_path,
        tool_name="Read",
        tool_input={"file_path": str(inside)},
    )
    recorder._handle("pre", payload)

    row = tmp_db.execute(
        "SELECT scope_id FROM tool_calls WHERE tool_use_id = ?;",
        (payload["tool_use_id"],),
    ).fetchone()
    assert _scope_name_for_last_row(tmp_db, row[0]) == "within_project"


def test_project_root_stored_on_projects_row(tmp_db, recorder, tmp_path):
    # Use the /repository convention — project root should land on parent.
    workspace = tmp_path / "myproj"
    repo = workspace / "repository"
    repo.mkdir(parents=True)
    payload = _pre_payload(tmp_path, cwd=str(repo))
    recorder._handle("pre", payload)

    row = tmp_db.execute(
        "SELECT root FROM projects WHERE cwd = ?;", (str(repo),)
    ).fetchone()
    assert row is not None
    assert row[0] == str(workspace)


def test_no_cwd_gets_null_scope(tmp_db, recorder):
    # Project upsert is skipped entirely when cwd is empty; scope_id must be NULL.
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "rm /tmp/x"},
        "cwd": "",
        "session_id": "sess-noroot",
        "tool_use_id": "toolu_noroot_1",
    }
    recorder._handle("pre", payload)

    row = tmp_db.execute(
        "SELECT scope_id FROM tool_calls WHERE tool_use_id = ?;",
        (payload["tool_use_id"],),
    ).fetchone()
    assert row is not None
    assert row[0] is None
