"""Tests for the v8 recorder (``recorder/run.py``).

Covers the behaviours finalised in Phase 3.5 (v5 schema + v6 cleanup) and
Phase 3.6 (v7 FK columns + v8 drop of legacy TEXT columns):
    * ``status_id`` as the single source of truth (legacy TEXT ``status``
      column was dropped in v6)
    * ``permission_mode_id`` lookup (resolved / NULL-on-missing)
    * ``tool_extras`` sidecar entries (``name='payload'`` / ``name='response'``)
    * Orphan-post fallback writes response extra
    * ``sessions.transcript_path`` set-once-only — joined via INTEGER
      ``sessions.id``, the UUID now lives on ``session_uuid``
    * ``args_json`` minification (no ``", "`` or ``": "`` separators)
    * Truncation of payload (4096) and response (2048) sidecar blobs
    * No ``status`` TEXT column (v6 dropped it)
    * No ``tool`` / ``subagent_type`` / ``file_path`` / TEXT ``session_id``
      columns (v8 dropped them); recorder writes FK columns only
    * ``tool_id`` resolves to ``tools.name`` for known tools
    * ``subagent_type_id`` is NULL for non-Agent calls
    * ``file_path_id`` is reused across repeated observations of the same path
    * ``session_id`` on ``tool_calls`` is INTEGER and JOINs to ``sessions.id``

The ``tmp_db`` fixture (from ``tests/conftest.py``) applies every
``lib/schema/v*.sql`` migration to an isolated DB and points
``OBSERVABILITY_DB`` at it so ``recorder.run`` writes there too.
"""
from __future__ import annotations

import importlib
import sqlite3

import pytest


# The recorder module has to be reloaded per-test because it captures
# ``lib.db``'s ``_open`` at import time; the fixture reloads ``lib.db`` to
# pick up the patched ``OBSERVABILITY_DB``, so we must also reload the
# recorder to make it see the refreshed bindings.
@pytest.fixture
def recorder(tmp_db):
    import recorder.run as run_module

    importlib.reload(run_module)
    return run_module


def _pending_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM call_statuses WHERE name = 'pending';"
    ).fetchone()
    assert row is not None
    return int(row[0])


def _ok_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM call_statuses WHERE name = 'ok';"
    ).fetchone()
    assert row is not None
    return int(row[0])


def _err_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM call_statuses WHERE name = 'err';"
    ).fetchone()
    assert row is not None
    return int(row[0])


def _default_mode_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM permission_modes WHERE name = 'default';"
    ).fetchone()
    assert row is not None
    return int(row[0])


def _pre_payload(**overrides):
    base = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "cwd": "/tmp/rec-test",
        "session_id": "sess-rec-1",
        "tool_use_id": "toolu_rec_1",
        "permission_mode": "default",
    }
    base.update(overrides)
    return base


def _post_payload(**overrides):
    base = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "cwd": "/tmp/rec-test",
        "session_id": "sess-rec-1",
        "tool_use_id": "toolu_rec_1",
        "tool_response": {"stdout": "hi"},
    }
    base.update(overrides)
    return base


# ---------- pre-handler ----------------------------------------------------


def test_pre_inserts_pending_row_with_permission_mode_and_payload_extra(
    tmp_db, recorder
):
    payload = _pre_payload()
    recorder._handle("pre", payload)

    row = tmp_db.execute(
        """
        SELECT tc.id,
               (SELECT name FROM tools WHERE id = tc.tool_id) AS tool_name,
               tc.tool_use_id, tc.status_id, tc.permission_mode_id,
               tc.completed_ts, tc.ok
          FROM tool_calls tc WHERE tc.tool_use_id = ?;
        """,
        (payload["tool_use_id"],),
    ).fetchone()
    assert row is not None
    (
        call_id,
        tool,
        tool_use_id,
        status_id,
        permission_mode_id,
        completed_ts,
        ok,
    ) = row
    assert tool == "Bash"
    assert tool_use_id == "toolu_rec_1"
    assert status_id == _pending_id(tmp_db)
    assert permission_mode_id == _default_mode_id(tmp_db)
    assert completed_ts is None
    assert ok is None

    extra = tmp_db.execute(
        "SELECT name, value FROM tool_extras WHERE tool_call_id = ?;",
        (call_id,),
    ).fetchone()
    assert extra is not None
    assert extra[0] == "payload"
    # Minified JSON has no ", " or ": " separators; the recorder uses
    # lib.db.minify_json which passes separators=(",", ":").
    assert ", " not in extra[1]
    assert ": " not in extra[1]
    # Roundtrip the stored blob: it's a real JSON object and contains the
    # original tool_name.
    import json as _json

    parsed = _json.loads(extra[1])
    assert parsed["tool_name"] == "Bash"
    assert parsed["tool_use_id"] == "toolu_rec_1"


def test_pre_permission_mode_null_when_payload_omits_it(tmp_db, recorder):
    payload = _pre_payload()
    del payload["permission_mode"]
    recorder._handle("pre", payload)

    row = tmp_db.execute(
        "SELECT permission_mode_id FROM tool_calls WHERE tool_use_id = ?;",
        (payload["tool_use_id"],),
    ).fetchone()
    assert row is not None
    assert row[0] is None


def test_pre_args_json_is_minified(tmp_db, recorder):
    # A tool_input with strings that would normally be space-separated by
    # the default JSON encoder.
    payload = _pre_payload(tool_input={"command": "echo hi", "description": "x"})
    recorder._handle("pre", payload)
    row = tmp_db.execute(
        "SELECT args_json FROM tool_calls WHERE tool_use_id = ?;",
        (payload["tool_use_id"],),
    ).fetchone()
    assert row is not None
    args_json = row[0]
    assert args_json is not None
    assert ", " not in args_json
    assert ": " not in args_json
    # Still valid JSON.
    import json as _json

    parsed = _json.loads(args_json)
    assert parsed["command"] == "echo hi"


def test_pre_payload_truncated_to_4096_chars(tmp_db, recorder):
    big = "x" * 10_000
    payload = _pre_payload(tool_input={"command": big})
    recorder._handle("pre", payload)
    extra = tmp_db.execute(
        """
        SELECT value FROM tool_extras
         WHERE tool_call_id = (SELECT id FROM tool_calls WHERE tool_use_id = ?)
           AND name = 'payload';
        """,
        (payload["tool_use_id"],),
    ).fetchone()
    assert extra is not None
    assert len(extra[0]) == 4096


# ---------- post-handler (matching pre) -----------------------------------


def test_post_updates_status_to_ok_and_writes_response_extra(tmp_db, recorder):
    recorder._handle("pre", _pre_payload())
    recorder._handle("post", _post_payload())

    row = tmp_db.execute(
        """
        SELECT status_id, ok, completed_ts
          FROM tool_calls WHERE tool_use_id = ?;
        """,
        ("toolu_rec_1",),
    ).fetchone()
    assert row is not None
    status_id, ok, completed_ts = row
    assert status_id == _ok_id(tmp_db)
    assert ok == 1
    assert completed_ts is not None

    response_extra = tmp_db.execute(
        """
        SELECT value FROM tool_extras
         WHERE tool_call_id = (SELECT id FROM tool_calls WHERE tool_use_id = ?)
           AND name = 'response';
        """,
        ("toolu_rec_1",),
    ).fetchone()
    assert response_extra is not None
    import json as _json

    parsed = _json.loads(response_extra[0])
    assert parsed["stdout"] == "hi"


def test_post_with_is_error_true_sets_status_err(tmp_db, recorder):
    recorder._handle("pre", _pre_payload())
    recorder._handle(
        "post",
        _post_payload(tool_response={"is_error": True, "stderr": "boom"}),
    )
    row = tmp_db.execute(
        "SELECT status_id, ok FROM tool_calls WHERE tool_use_id = ?;",
        ("toolu_rec_1",),
    ).fetchone()
    assert row is not None
    status_id, ok = row
    assert status_id == _err_id(tmp_db)
    assert ok == 0


def test_post_response_truncated_to_2048_chars(tmp_db, recorder):
    recorder._handle("pre", _pre_payload())
    big = "y" * 10_000
    recorder._handle(
        "post",
        _post_payload(tool_response={"stdout": big}),
    )
    extra = tmp_db.execute(
        """
        SELECT value FROM tool_extras
         WHERE tool_call_id = (SELECT id FROM tool_calls WHERE tool_use_id = ?)
           AND name = 'response';
        """,
        ("toolu_rec_1",),
    ).fetchone()
    assert extra is not None
    assert len(extra[0]) == 2048


# ---------- orphan post ---------------------------------------------------


def test_orphan_post_inserts_row_and_writes_response_extra(tmp_db, recorder):
    # No matching pre first.
    recorder._handle(
        "post",
        _post_payload(tool_use_id="toolu_orphan_1"),
    )
    row = tmp_db.execute(
        """
        SELECT id, status_id, ok
          FROM tool_calls WHERE tool_use_id = ?;
        """,
        ("toolu_orphan_1",),
    ).fetchone()
    assert row is not None
    call_id, status_id, ok = row
    assert status_id == _ok_id(tmp_db)
    assert ok == 1

    extra = tmp_db.execute(
        "SELECT name, value FROM tool_extras WHERE tool_call_id = ?;",
        (call_id,),
    ).fetchone()
    assert extra is not None
    assert extra[0] == "response"


# ---------- transcript_path on sessions -----------------------------------


def test_transcript_path_set_from_pre_payload(tmp_db, recorder):
    recorder._handle(
        "pre",
        _pre_payload(transcript_path="/tmp/t-1.jsonl"),
    )
    # Post-v7 sessions keeps the UUID on `session_uuid`; `id` is INTEGER.
    row = tmp_db.execute(
        "SELECT transcript_path FROM sessions WHERE session_uuid = ?;",
        ("sess-rec-1",),
    ).fetchone()
    assert row is not None
    assert row[0] == "/tmp/t-1.jsonl"


def test_second_pre_does_not_overwrite_transcript_path(tmp_db, recorder):
    recorder._handle(
        "pre",
        _pre_payload(transcript_path="/tmp/t-1.jsonl"),
    )
    recorder._handle(
        "pre",
        _pre_payload(
            tool_use_id="toolu_rec_2",
            transcript_path="/tmp/different.jsonl",
        ),
    )
    row = tmp_db.execute(
        "SELECT transcript_path FROM sessions WHERE session_uuid = ?;",
        ("sess-rec-1",),
    ).fetchone()
    assert row is not None
    # Set-once: the first path wins.
    assert row[0] == "/tmp/t-1.jsonl"


# ---------- v6: status TEXT column has been dropped -----------------------


def test_status_text_column_is_gone(tmp_db, recorder):
    """Any reference to `tool_calls.status` should fail — v6 dropped it."""
    # Insert a row so we have something to query.
    recorder._handle("pre", _pre_payload())
    with pytest.raises(sqlite3.OperationalError):
        tmp_db.execute("SELECT status FROM tool_calls;").fetchone()


# ---------- v7: FK columns resolve correctly ------------------------------


def test_tool_id_resolved_for_known_tool(tmp_db, recorder):
    """`tool_id` on the row matches the id in the `tools` lookup table."""
    recorder._handle("pre", _pre_payload())
    row = tmp_db.execute(
        """
        SELECT tc.tool_id, t.id
          FROM tool_calls tc
          JOIN tools t ON t.name = 'Bash'
         WHERE tc.tool_use_id = 'toolu_rec_1';
        """
    ).fetchone()
    assert row is not None
    tool_id, tools_id = row
    assert tool_id == tools_id
    # Sanity: only one Bash entry in the lookup table (auto-insert is
    # idempotent).
    (count,) = tmp_db.execute(
        "SELECT COUNT(*) FROM tools WHERE name = 'Bash';"
    ).fetchone()
    assert count == 1


def test_subagent_type_id_null_for_non_agent_call(tmp_db, recorder):
    """Bash calls have no subagent_type — FK stays NULL."""
    recorder._handle("pre", _pre_payload())
    row = tmp_db.execute(
        "SELECT subagent_type_id FROM tool_calls WHERE tool_use_id = ?;",
        ("toolu_rec_1",),
    ).fetchone()
    assert row is not None
    assert row[0] is None


def test_file_path_id_reused_on_second_observation(tmp_db, recorder):
    """Two Reads of the same path share a single file_paths row."""
    first = {
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/same.txt"},
        "cwd": "/tmp/rec-test",
        "session_id": "sess-rec-1",
        "tool_use_id": "toolu_read_1",
        "permission_mode": "default",
    }
    second = {
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/same.txt"},
        "cwd": "/tmp/rec-test",
        "session_id": "sess-rec-1",
        "tool_use_id": "toolu_read_2",
        "permission_mode": "default",
    }
    recorder._handle("pre", first)
    recorder._handle("pre", second)

    ids = tmp_db.execute(
        """
        SELECT file_path_id FROM tool_calls
         WHERE tool_use_id IN ('toolu_read_1', 'toolu_read_2')
         ORDER BY tool_use_id;
        """
    ).fetchall()
    assert len(ids) == 2
    assert ids[0][0] is not None
    assert ids[0][0] == ids[1][0]

    # Single row in the lookup table.
    (count,) = tmp_db.execute(
        "SELECT COUNT(*) FROM file_paths WHERE path = '/tmp/same.txt';"
    ).fetchone()
    assert count == 1


def test_session_id_is_integer_not_text(tmp_db, recorder):
    """`tool_calls.session_id` is INTEGER; JOINs to `sessions.id`."""
    recorder._handle("pre", _pre_payload())
    row = tmp_db.execute(
        """
        SELECT tc.session_id,
               (SELECT session_uuid FROM sessions WHERE id = tc.session_id)
          FROM tool_calls tc WHERE tc.tool_use_id = 'toolu_rec_1';
        """
    ).fetchone()
    assert row is not None
    session_id_val, resolved_uuid = row
    assert isinstance(session_id_val, int)
    assert resolved_uuid == "sess-rec-1"


# ---------- v8: legacy TEXT columns dropped -------------------------------


def test_no_legacy_text_columns_remain(tmp_db, recorder):
    """v8 DROP COLUMN pass — the old TEXT columns no longer exist."""
    # Insert a row first so the table is populated; the assertion is on
    # schema shape, not data.
    recorder._handle("pre", _pre_payload())
    for col in ("tool", "subagent_type", "file_path"):
        with pytest.raises(sqlite3.OperationalError):
            tmp_db.execute(f"SELECT {col} FROM tool_calls LIMIT 1;").fetchone()
    # The TEXT session_id was dropped in v8; the INTEGER one replaces it.
    # We can't assert "no session_id column" since the INTEGER FK column
    # was renamed into that slot, so instead assert the column's declared
    # type now resolves as INTEGER via PRAGMA.
    cols = {
        name: decl_type
        for (_, name, decl_type, *_rest) in tmp_db.execute(
            "PRAGMA table_info(tool_calls);"
        ).fetchall()
    }
    # session_id is now the renamed FK column — declared INTEGER.
    assert cols["session_id"].upper() == "INTEGER"
