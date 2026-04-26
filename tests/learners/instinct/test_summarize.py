"""Tests for learners.instinct.summarize — cursor management, row formatting,
Markdown snippet generation, and the optional ``claude`` CLI spawn path.

Uses the ``tmp_db`` fixture from conftest.py for an isolated, schema-seeded
SQLite database.  The ``v_tool_calls`` view joins tool_calls with sessions,
projects, and tools — so those supporting rows need to be seeded too.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Generator
from unittest import mock

import pytest

from nephoscope.learners.instinct.summarize import (
    CONSUMER,
    _advance,
    _cursor,
    _fetch,
    _format_row_snippet,
    _summarize,
    cmd_commit,
    cmd_write,
    main,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_db) -> Generator[sqlite3.Connection, None, None]:
    """tmp_db with tools, sessions, projects pre-seeded for v_tool_calls.

    Sets row_factory = sqlite3.Row so summarize functions can use column-name
    indexing (row["field"]) as they do against the live DB.
    """
    tmp_db.row_factory = sqlite3.Row
    tmp_db.executemany(
        "INSERT OR IGNORE INTO tools(name) VALUES (?)",
        [("Bash",), ("Read",), ("Edit",), ("Write",), ("Task",), ("Grep",), ("Glob",)],
    )
    tmp_db.commit()
    yield tmp_db


def _insert_project(conn: sqlite3.Connection, cwd: str = "/proj") -> int:
    conn.execute(
        "INSERT OR IGNORE INTO projects(cwd, name, root, first_seen, last_seen)"
        " VALUES (?, 'proj', ?, '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')",
        (cwd, cwd),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM projects WHERE cwd=?", (cwd,)).fetchone()
    return int(row[0])


def _insert_session(
    conn: sqlite3.Connection, project_id: int, uuid: str = "sess-1"
) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO sessions(session_uuid, project_id, transcript_path,"
        " started_at, last_activity)"
        " VALUES (?, ?, '/t', '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')",
        (uuid, project_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM sessions WHERE session_uuid=?", (uuid,)
    ).fetchone()
    return int(row[0])


def _tool_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM tools WHERE name=?", (name,)).fetchone()
    return int(row[0])


def _status_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM call_statuses WHERE name=?", (name,)).fetchone()
    return int(row[0])


def _insert_file_path(conn: sqlite3.Connection, path: str) -> int:
    """Insert a file_paths row and return its id."""
    now = "2024-01-01T00:00:00Z"
    conn.execute(
        "INSERT OR IGNORE INTO file_paths(path, first_seen, last_seen)"
        " VALUES (?, ?, ?)",
        (path, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM file_paths WHERE path = ?", (path,)).fetchone()
    if row is None:
        raise RuntimeError(f"file_paths insert failed for path={path!r}")
    return int(row["id"])


def _insert_tool_call(
    conn: sqlite3.Connection,
    session_id: int,
    tool: str,
    *,
    ok: bool = True,
    command: str | None = None,
    file_path: str | None = None,
    pattern: str | None = None,
    ts: str = "2024-01-01T00:00:00Z",
) -> int:
    status = "ok" if ok else "err"
    file_path_id = _insert_file_path(conn, file_path) if file_path else None
    conn.execute(
        "INSERT INTO tool_calls(session_id, tool_id, status_id, ts,"
        " command, file_path_id, pattern, ok)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            _tool_id(conn, tool),
            _status_id(conn, status),
            ts,
            command,
            file_path_id,
            pattern,
            1 if ok else 0,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM tool_calls ORDER BY id DESC LIMIT 1").fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# _cursor — reads last_processed_id from consumer_cursors
# ---------------------------------------------------------------------------


def test_cursor_returns_zero_when_no_row(conn):
    assert _cursor(conn) == 0


def test_cursor_returns_stored_value(conn):
    conn.execute(
        "INSERT INTO consumer_cursors(consumer, last_processed_id, updated_at)"
        " VALUES (?, 42, '2024-01-01T00:00:00Z')",
        (CONSUMER,),
    )
    conn.commit()
    assert _cursor(conn) == 42


# ---------------------------------------------------------------------------
# _advance — UPSERT cursor
# ---------------------------------------------------------------------------


def test_advance_creates_row(conn):
    _advance(conn, 99)
    assert _cursor(conn) == 99


def test_advance_updates_existing_row(conn):
    _advance(conn, 10)
    _advance(conn, 50)
    assert _cursor(conn) == 50


def test_advance_does_not_go_backward(conn):
    """_advance always writes the value given; caller is responsible for ordering."""
    _advance(conn, 50)
    _advance(conn, 10)
    assert _cursor(conn) == 10  # _advance is a raw write; caller controls ordering


# ---------------------------------------------------------------------------
# _fetch — returns rows newer than since_id
# ---------------------------------------------------------------------------


def test_fetch_empty_when_no_rows(conn):
    assert _fetch(conn, 0) == []


def test_fetch_respects_since_id(conn):
    proj_id = _insert_project(conn)
    sess_id = _insert_session(conn, proj_id)
    id1 = _insert_tool_call(conn, sess_id, "Bash", command="ls")
    id2 = _insert_tool_call(conn, sess_id, "Bash", command="pwd")

    rows = _fetch(conn, id1)
    assert len(rows) == 1
    assert int(rows[0]["id"]) == id2


def test_fetch_returns_all_columns(conn):
    proj_id = _insert_project(conn)
    sess_id = _insert_session(conn, proj_id)
    _insert_tool_call(conn, sess_id, "Bash", command="echo hi")

    rows = _fetch(conn, 0)
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "Bash"
    assert row["command"] == "echo hi"


# ---------------------------------------------------------------------------
# _format_row_snippet — per-tool rendering
# ---------------------------------------------------------------------------


def _make_mock_row(**kwargs) -> mock.MagicMock:
    row = mock.MagicMock()
    row.__getitem__ = lambda self, k: kwargs.get(k)
    return row


def test_format_bash_with_command():
    row = _make_mock_row(tool="Bash", command="git status")
    assert _format_row_snippet(row) == "Bash: git status"


def test_format_bash_no_command():
    row = _make_mock_row(tool="Bash", command=None)
    assert _format_row_snippet(row) == "Bash"


def test_format_task_with_subagent_and_description():
    row = _make_mock_row(
        tool="Task", subagent_type="code-reviewer", description="review PR"
    )
    assert _format_row_snippet(row) == "Task: code-reviewer — review PR"


def test_format_task_subagent_only():
    row = _make_mock_row(tool="Task", subagent_type="tdd-guide", description=None)
    assert _format_row_snippet(row) == "Task: tdd-guide"


def test_format_task_no_details():
    row = _make_mock_row(tool="Task", subagent_type=None, description=None)
    assert _format_row_snippet(row) == "Task: (no details)"


def test_format_read_with_file_path():
    row = _make_mock_row(
        tool="Read", file_path="/some/file.py", subagent_type=None, description=None
    )
    assert _format_row_snippet(row) == "Read: /some/file.py"


def test_format_edit_with_file_path():
    row = _make_mock_row(
        tool="Edit", file_path="/src/foo.py", subagent_type=None, description=None
    )
    assert _format_row_snippet(row) == "Edit: /src/foo.py"


def test_format_grep_with_pattern():
    row = _make_mock_row(tool="Grep", pattern="def main", file_path=None)
    assert _format_row_snippet(row) == "Grep: def main"


def test_format_glob_with_pattern():
    row = _make_mock_row(tool="Glob", pattern="**/*.py", file_path=None)
    assert _format_row_snippet(row) == "Glob: **/*.py"


def test_format_unknown_tool():
    row = _make_mock_row(
        tool=None,
        command=None,
        file_path=None,
        pattern=None,
        subagent_type=None,
        description=None,
    )
    assert _format_row_snippet(row) == "(unknown tool)"


def test_format_unknown_tool_with_name():
    row = _make_mock_row(
        tool="SomeFutureTool",
        command=None,
        file_path=None,
        pattern=None,
        subagent_type=None,
        description=None,
    )
    assert _format_row_snippet(row) == "SomeFutureTool"


# ---------------------------------------------------------------------------
# _summarize — Markdown rendering from a row list
# ---------------------------------------------------------------------------


def test_summarize_empty_rows():
    result = _summarize([])
    assert "No activity" in result


def test_summarize_contains_tool_frequency_section(conn):
    proj_id = _insert_project(conn)
    sess_id = _insert_session(conn, proj_id)
    _insert_tool_call(conn, sess_id, "Bash", command="ls")
    _insert_tool_call(conn, sess_id, "Read", file_path="/f")

    rows = _fetch(conn, 0)
    output = _summarize(rows)
    assert "## Tool frequency" in output
    assert "Bash" in output
    assert "Read" in output


def test_summarize_contains_per_project_section(conn):
    proj_id = _insert_project(conn, cwd="/myproject")
    sess_id = _insert_session(conn, proj_id)
    _insert_tool_call(conn, sess_id, "Bash", command="pwd")

    rows = _fetch(conn, 0)
    output = _summarize(rows)
    assert "## Per-project activity" in output


def test_summarize_errors_section_only_when_errors_present(conn):
    proj_id = _insert_project(conn)
    sess_id = _insert_session(conn, proj_id)
    _insert_tool_call(conn, sess_id, "Bash", command="ls", ok=True)

    rows = _fetch(conn, 0)
    output = _summarize(rows)
    assert "Recent errors" not in output


def test_summarize_errors_section_present_on_failure(conn):
    proj_id = _insert_project(conn)
    sess_id = _insert_session(conn, proj_id)
    _insert_tool_call(conn, sess_id, "Bash", command="fail-cmd", ok=False)

    rows = _fetch(conn, 0)
    output = _summarize(rows)
    assert "Recent errors" in output


def test_summarize_sequences_section_requires_repeat(conn):
    """Repeated sequence only shows when the same N-tuple occurs 2+ times."""
    proj_id = _insert_project(conn)
    sess_id = _insert_session(conn, proj_id)
    # Repeat: Bash → Read → Bash × 2
    for _ in range(2):
        _insert_tool_call(conn, sess_id, "Bash", command="ls")
        _insert_tool_call(conn, sess_id, "Read", file_path="/f")
        _insert_tool_call(conn, sess_id, "Bash", command="cat /f")

    rows = _fetch(conn, 0)
    output = _summarize(rows)
    assert "Common" in output


def test_summarize_row_count_in_header(conn):
    proj_id = _insert_project(conn)
    sess_id = _insert_session(conn, proj_id)
    for _ in range(5):
        _insert_tool_call(conn, sess_id, "Bash", command="x")

    rows = _fetch(conn, 0)
    output = _summarize(rows)
    assert "5 tool calls" in output


# ---------------------------------------------------------------------------
# cmd_write — writes file, emits JSON meta to stdout, returns 0
# ---------------------------------------------------------------------------


def test_cmd_write_happy_path(conn, tmp_path, capsys):
    proj_id = _insert_project(conn)
    sess_id = _insert_session(conn, proj_id)
    for _ in range(12):
        _insert_tool_call(conn, sess_id, "Bash", command="ls")

    out_file = tmp_path / "summary.txt"
    write_args = argparse.Namespace(output=str(out_file), min_rows=10)

    rc = cmd_write(write_args)
    assert rc == 0
    assert out_file.exists()
    content = out_file.read_text()
    assert "tool calls" in content

    captured = capsys.readouterr()
    meta = json.loads(captured.out)
    assert meta["rows"] == 12
    assert "max_id" in meta


def test_cmd_write_returns_2_when_below_min_rows(conn, tmp_path):
    proj_id = _insert_project(conn)
    sess_id = _insert_session(conn, proj_id)
    for _ in range(5):
        _insert_tool_call(conn, sess_id, "Bash", command="ls")

    out_file = tmp_path / "summary.txt"
    write_args = argparse.Namespace(output=str(out_file), min_rows=10)

    rc = cmd_write(write_args)
    assert rc == 2
    assert not out_file.exists()


def test_cmd_write_creates_parent_dirs(conn, tmp_path):
    proj_id = _insert_project(conn)
    sess_id = _insert_session(conn, proj_id)
    for _ in range(10):
        _insert_tool_call(conn, sess_id, "Bash", command="ls")

    out_file = tmp_path / "a" / "b" / "c" / "summary.txt"
    write_args = argparse.Namespace(output=str(out_file), min_rows=10)

    rc = cmd_write(write_args)
    assert rc == 0
    assert out_file.exists()


# ---------------------------------------------------------------------------
# cmd_commit — advances the cursor
# ---------------------------------------------------------------------------


def test_cmd_commit_advances_cursor(conn):
    commit_args = argparse.Namespace(max_id=77)

    rc = cmd_commit(commit_args)
    assert rc == 0
    assert _cursor(conn) == 77


# ---------------------------------------------------------------------------
# main() — integration test for the claude CLI spawn path
# ---------------------------------------------------------------------------


def test_main_write_subcommand(conn, tmp_path, capsys):
    proj_id = _insert_project(conn)
    sess_id = _insert_session(conn, proj_id)
    for _ in range(10):
        _insert_tool_call(conn, sess_id, "Bash", command="ls")

    out_file = tmp_path / "out.txt"
    rc = main(["write", "--output", str(out_file), "--min-rows", "10"])
    assert rc == 0
    assert out_file.exists()


def test_main_commit_subcommand(conn):
    rc = main(["commit", "--max-id", "123"])
    assert rc == 0
    assert _cursor(conn) == 123


def test_main_write_returns_2_nothing_new(conn, tmp_path):
    out_file = tmp_path / "out.txt"
    # No rows seeded — min_rows default 10 triggers early return 2.
    rc = main(["write", "--output", str(out_file)])
    assert rc == 2
