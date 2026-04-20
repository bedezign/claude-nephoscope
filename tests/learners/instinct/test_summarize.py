"""Tests for learners.instinct.summarize — the observer-agent feed."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lib.db import (
    _upsert_project,
    _upsert_session,
    lookup_or_insert_tool_id,
    lookup_permission_mode_id,
    lookup_status_id,
)
from learners.instinct import summarize


def _insert_call(conn, *, command: str | None, session_uuid: str, tool: str = "Bash", ok: int = 1, ts: str = "2026-04-20T10:00:00.000Z") -> int:
    project_id = _upsert_project(conn, cwd=f"/tmp/test-{session_uuid}", now=ts)
    session_int = _upsert_session(conn, session_uuid, project_id, ts)
    tool_id = lookup_or_insert_tool_id(conn, tool)
    status_id = lookup_status_id(conn, "ok" if ok else "err")
    permission_mode_id = lookup_permission_mode_id(conn, "default")
    cur = conn.execute(
        """
        INSERT INTO tool_calls
          (ts, completed_ts, session_id, project_id, ok,
           command, args_json, tool_use_id,
           status_id, permission_mode_id, tool_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            ts, ts, session_int, project_id, ok,
            command, "{}", f"use::{command}::{session_uuid}",
            status_id, permission_mode_id, tool_id,
        ),
    )
    return int(cur.lastrowid or 0)


def _cursor_value(conn) -> int:
    row = conn.execute(
        "SELECT last_processed_id FROM consumer_cursors WHERE consumer=?;",
        (summarize.CONSUMER,),
    ).fetchone()
    return int(row[0]) if row else 0


def test_write_below_min_rows_returns_empty_and_no_cursor_move(tmp_db, tmp_path: Path, monkeypatch, capsys):
    for i in range(3):
        _insert_call(tmp_db, command=f"ls /tmp/{i}", session_uuid="s-a")

    out = tmp_path / "summary.txt"
    rc = summarize.main(["write", "--output", str(out), "--min-rows", "10"])
    assert rc == 2
    assert not out.exists()
    assert _cursor_value(tmp_db) == 0


def test_write_produces_summary_with_expected_sections(tmp_db, tmp_path: Path, capsys):
    # 12 varied calls across 2 sessions to exercise every section.
    for i in range(6):
        _insert_call(tmp_db, command=f"ls /tmp/{i}", session_uuid="s-a")
        _insert_call(tmp_db, command=None, session_uuid="s-a", tool="Read")
    _insert_call(tmp_db, command="broken", session_uuid="s-b", tool="Bash", ok=0)

    out = tmp_path / "summary.txt"
    rc = summarize.main(["write", "--output", str(out), "--min-rows", "5"])
    assert rc == 0
    text = out.read_text()
    assert "Observation summary" in text
    assert "## Tool frequency" in text
    assert "## Per-project activity" in text
    assert "## Sample recent calls (last 20)" in text
    # Error row should surface.
    assert "## Recent errors" in text
    # Stdout emits JSON with max_id + rows count.
    captured = capsys.readouterr().out.strip()
    meta = json.loads(captured)
    assert meta["rows"] == 13
    assert meta["max_id"] > 0


def test_commit_advances_cursor(tmp_db, capsys):
    for i in range(12):
        _insert_call(tmp_db, command=f"ls /tmp/{i}", session_uuid="s-a")
    rc = summarize.main(["commit", "--max-id", "7"])
    assert rc == 0
    assert _cursor_value(tmp_db) == 7
    # Second commit updates.
    rc = summarize.main(["commit", "--max-id", "9"])
    assert _cursor_value(tmp_db) == 9


def test_write_respects_cursor_skips_already_processed(tmp_db, tmp_path: Path, capsys):
    ids = [
        _insert_call(tmp_db, command=f"ls /tmp/{i}", session_uuid="s-a")
        for i in range(12)
    ]
    # Advance cursor past half.
    summarize.main(["commit", "--max-id", str(ids[5])])

    out = tmp_path / "summary.txt"
    rc = summarize.main(["write", "--output", str(out), "--min-rows", "1"])
    assert rc == 0
    meta = json.loads(capsys.readouterr().out.strip())
    assert meta["rows"] == 6  # only ids[6..11]


def test_sequence_detection_flags_repeated_patterns(tmp_db, tmp_path: Path, capsys):
    # Build a repeating Grep → Read → Edit sequence within one session.
    for _ in range(4):
        _insert_call(tmp_db, command=None, session_uuid="seq", tool="Grep")
        _insert_call(tmp_db, command=None, session_uuid="seq", tool="Read")
        _insert_call(tmp_db, command=None, session_uuid="seq", tool="Edit")

    out = tmp_path / "summary.txt"
    rc = summarize.main(["write", "--output", str(out), "--min-rows", "1"])
    assert rc == 0
    text = out.read_text()
    assert "Common 3-tool sequences" in text
    assert "Grep → Read → Edit" in text


def test_unknown_project_falls_back_gracefully(tmp_db, tmp_path: Path, capsys):
    # Insert calls with NULL project_id by writing the row directly (bypassing
    # _upsert_project). The view LEFT JOINs projects so project_name is NULL.
    tool_id = lookup_or_insert_tool_id(tmp_db, "Bash")
    status_id = lookup_status_id(tmp_db, "ok")
    for i in range(10):
        tmp_db.execute(
            """
            INSERT INTO tool_calls
              (ts, session_id, project_id, ok, command, args_json,
               tool_use_id, status_id, tool_id)
            VALUES (?, NULL, NULL, 1, ?, '{}', ?, ?, ?);
            """,
            ("2026-04-20T10:00:00.000Z", f"cmd-{i}", f"use-{i}", status_id, tool_id),
        )
    tmp_db.commit()

    out = tmp_path / "summary.txt"
    rc = summarize.main(["write", "--output", str(out), "--min-rows", "1"])
    assert rc == 0
    text = out.read_text()
    assert "(unknown)" in text
