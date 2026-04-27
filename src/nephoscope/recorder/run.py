"""Observations recorder — session-start / pre / post tool-call hook handler.

Reads a Claude Code hook payload from stdin and writes (or updates) a row in
``tool_calls``. The phase is passed as ``sys.argv[1]`` (``session_start``,
``pre``, or ``post``); unknown values are treated as ``post`` so stray
invocations still produce a complete row.

Pre phase:
    INSERT row with ``status_id=<pending>``, ``tool_use_id``, ``ts=_now()``,
    ``completed_ts=NULL``, ``ok=NULL``, and FK columns populated from lookup
    tables. Capture top-level ``permission_mode``, store the truncated
    payload in ``tool_extras(name='payload')``, and set
    ``sessions.transcript_path`` (set-once-only) when present.

Post phase:
    UPDATE the row matched by ``tool_use_id`` setting ``completed_ts``,
    ``status_id`` (ok/err) and ``ok``. Capture the ``tool_response``
    (truncated) in ``tool_extras(name='response')``. If no pending row
    exists (orphan post), INSERT a complete row so nothing is lost.

Session-start phase:
    Upsert the session + project rows only; no ``tool_calls`` write. Lazily
    bootstraps the observations DB on first run (creates parent dir, applies
    ``lib/schema.sql``).

An opt-out marker (see :mod:`nephoscope.lib.paths`) short-circuits all
phases. Malformed input is silently swallowed. Unhandled exceptions are
printed to stderr but we still exit 0 so the user's tool call is never
broken by the recorder.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any

from nephoscope.lib.db import (  # noqa: E402
    MAX_STR,
    _now,
    _open,
    _truncate,
    lookup_or_insert_file_path_id,
    lookup_or_insert_subagent_type_id,
    lookup_or_insert_tool_id,
    lookup_permission_mode_id,
    lookup_status_id,
    minify_json,
    set_session_extra_dirs,
    upsert_project,
    upsert_session,
    write_extra,
)
from nephoscope.lib.paths import (  # noqa: E402
    canonicalize,
    extract_add_dir_args,
    is_disabled,
    observations_db_path,
)
from nephoscope.lib.mirror.writer import cleanup_stale_tmp
from nephoscope.lib.scope import Scope, get_additional_dirs

PAYLOAD_MAX = 4096
RESPONSE_MAX = 2048


def _flatten_agent_fields(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Extract flat columns for Task/Agent tools."""
    row: dict[str, Any] = {}
    if (v := tool_input.get("subagent_type")) is not None:
        row["subagent_type"] = _truncate(v)
    if (v := tool_input.get("description")) is not None:
        row["description"] = _truncate(v)
    return row


def _flatten_bash_fields(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Extract flat columns for Bash tools."""
    row: dict[str, Any] = {}
    if (v := tool_input.get("command")) is not None:
        row["command"] = _truncate(v)
    if (v := tool_input.get("description")) is not None:
        row["description"] = _truncate(v)
    return row


def _flatten_file_fields(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Extract flat columns for file tools (Read, Edit, Write, etc.)."""
    row: dict[str, Any] = {}
    if (v := tool_input.get("file_path")) is not None:
        row["file_path"] = _truncate(v)
    return row


def _flatten_search_fields(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Extract flat columns for search tools (Grep, Glob)."""
    row: dict[str, Any] = {}
    if (v := tool_input.get("pattern")) is not None:
        row["pattern"] = _truncate(v)
    return row


def _flatten(tool: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Project a tool-input dict into the flat columns stored on tool_calls."""
    if tool in ("Task", "Agent"):
        row = _flatten_agent_fields(tool_input)
    elif tool == "Bash":
        row = _flatten_bash_fields(tool_input)
    elif tool in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        row = _flatten_file_fields(tool_input)
    elif tool in ("Grep", "Glob"):
        row = _flatten_search_fields(tool_input)
    else:
        row = {}
    try:
        raw = minify_json(tool_input)
    except (TypeError, ValueError):
        raw = str(tool_input)
    row["args_json"] = raw[: MAX_STR * 4]
    return row


def _ok(tool_response: Any) -> int | None:
    """Classify a tool_response payload as success (1) / failure (0) / unknown."""
    if not isinstance(tool_response, dict):
        return None
    if "is_error" in tool_response:
        return 0 if tool_response.get("is_error") else 1
    if "error" in tool_response:
        return 0
    return 1


def _status_name_from_ok(ok: int | None) -> str:
    """Map _ok() output to the schema's status enum name for completed rows."""
    return "err" if ok == 0 else "ok"


def _synthetic_use_id(session_id: str, tool: str, now: str) -> str:
    """Fallback identifier when the payload has no tool_use_id."""
    return f"synthetic::{session_id}::{tool}::{now}"


def _safe_minify(value: Any) -> str:
    """minify_json but fall back to ``str()`` for objects JSON can't serialise."""
    try:
        return minify_json(value)
    except (TypeError, ValueError):
        return str(value)


def _handle_pre(
    conn: sqlite3.Connection,
    data: dict[str, Any],
    flat: dict[str, Any],
    session_id_int: int | None,
    project_id: int | None,
    tool_use_id: str,
    tool_id: int,
    subagent_type_id: int | None,
    file_path_id: int | None,
    now: str,
) -> None:
    """Write a pending tool_calls row for the pre phase."""
    permission_mode_id = lookup_permission_mode_id(conn, data.get("permission_mode"))
    pending_id = lookup_status_id(conn, "pending")

    cur = conn.execute(
        """
        INSERT INTO tool_calls
          (ts, session_id, project_id, ok,
           command, pattern, description,
           args_json, tool_use_id, completed_ts,
           permission_mode_id, status_id,
           tool_id, subagent_type_id, file_path_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?);
        """,
        (
            now,
            session_id_int,
            project_id,
            None,
            flat.get("command"),
            flat.get("pattern"),
            flat.get("description"),
            flat.get("args_json"),
            tool_use_id,
            permission_mode_id,
            pending_id,
            tool_id,
            subagent_type_id,
            file_path_id,
        ),
    )
    tool_call_id = int(cur.lastrowid or 0)

    if tool_call_id > 0:
        payload_blob = _safe_minify(data)[:PAYLOAD_MAX]
        write_extra(conn, tool_call_id, "payload", payload_blob)

    transcript_path = canonicalize(data.get("transcript_path") or "")
    if transcript_path and session_id_int is not None:
        conn.execute(
            "UPDATE sessions SET transcript_path = ? "
            "WHERE id = ? AND transcript_path IS NULL;",
            (transcript_path, session_id_int),
        )


def _handle_post(
    conn: sqlite3.Connection,
    data: dict[str, Any],
    flat: dict[str, Any],
    session_id_int: int | None,
    project_id: int | None,
    tool_use_id: str,
    tool_id: int,
    subagent_type_id: int | None,
    file_path_id: int | None,
    now: str,
) -> None:
    """Update or orphan-insert a tool_calls row for the post phase."""
    ok = _ok(data.get("tool_response"))
    status_id = lookup_status_id(conn, _status_name_from_ok(ok))

    existing = conn.execute(
        "SELECT id FROM tool_calls WHERE tool_use_id = ? ORDER BY id DESC LIMIT 1;",
        (tool_use_id,),
    ).fetchone()

    if existing is not None:
        tool_call_id = int(existing[0])
        conn.execute(
            """
            UPDATE tool_calls
               SET completed_ts = ?,
                   status_id = ?,
                   ok = ?
             WHERE id = ?;
            """,
            (now, status_id, ok, tool_call_id),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO tool_calls
              (ts, session_id, project_id, ok,
               command, pattern, description,
               args_json, tool_use_id, completed_ts, status_id,
               tool_id, subagent_type_id, file_path_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                now,
                session_id_int,
                project_id,
                ok,
                flat.get("command"),
                flat.get("pattern"),
                flat.get("description"),
                flat.get("args_json"),
                tool_use_id,
                now,
                status_id,
                tool_id,
                subagent_type_id,
                file_path_id,
            ),
        )
        tool_call_id = int(cur.lastrowid or 0)

    tool_response = data.get("tool_response")
    if tool_response and tool_call_id > 0:
        response_blob = _safe_minify(tool_response)[:RESPONSE_MAX]
        write_extra(conn, tool_call_id, "response", response_blob)


def _handle(phase: str, data: dict[str, Any]) -> None:
    tool = data.get("tool_name") or data.get("tool") or "unknown"
    tool_input = data.get("tool_input") or data.get("input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    cwd = data.get("cwd") or ""
    session_uuid = data.get("session_id") or data.get("session") or "unknown"
    tool_use_id = data.get("tool_use_id")
    now = _now()
    if not tool_use_id:
        tool_use_id = _synthetic_use_id(session_uuid, tool, now)

    flat = _flatten(tool, tool_input)

    conn = _open()
    try:
        project_id = upsert_project(conn, cwd, now) if cwd else None
        session_id_int: int | None = None
        if project_id is not None and session_uuid != "unknown":
            session_id_int = upsert_session(conn, session_uuid, project_id, now)

        tool_id = lookup_or_insert_tool_id(conn, tool)
        subagent_type_id = lookup_or_insert_subagent_type_id(
            conn, flat.get("subagent_type")
        )
        file_path_id = lookup_or_insert_file_path_id(conn, flat.get("file_path"), now)

        common = (
            conn,
            data,
            flat,
            session_id_int,
            project_id,
            tool_use_id,
            tool_id,
            subagent_type_id,
            file_path_id,
            now,
        )
        if phase == "pre":
            _handle_pre(*common)
        else:
            _handle_post(*common)
    finally:
        conn.close()


def _handle_session_start(data: dict[str, Any]) -> None:
    """Handle a SessionStart hook payload.

    Upserts the session + project rows. No ``tool_calls`` write — the
    SessionStart phase is metadata-only. Missing ``session_id`` is a
    no-op (fails open; next pre/post will repair state).

    Also refreshes the additionalDirectories cache for the global mirror and
    the active project (if one exists). The mtime check makes each call cheap
    when the cache is already fresh. Errors are swallowed — a cache-refresh
    failure must never crash the session.
    """
    session_uuid = data.get("session_id") or data.get("session") or ""
    if not session_uuid:
        return
    cwd = data.get("cwd") or ""
    now = _now()
    conn = _open()
    try:
        project_id: int | None = None
        if cwd:
            project_id = upsert_project(conn, cwd, now)
        session_id = upsert_session(conn, session_uuid, project_id, now)

        try:
            extras = extract_add_dir_args()
            if extras:
                set_session_extra_dirs(conn, session_id, json.dumps(extras))
        except Exception as exc:  # noqa: BLE001 — capture failure must not crash the session.
            print(
                f"WARNING: _handle_session_start extra_dirs capture failed: {exc}",
                file=sys.stderr,
            )

        def _sweep(query: str, args: tuple = (), *, label: str) -> None:
            try:
                row = conn.execute(query, args).fetchone()
                if row and row[0]:
                    cleanup_stale_tmp(Path(row[0]).parent, 300)
            except Exception as exc:  # noqa: BLE001 — sweep failure must not crash the session.
                print(
                    f"WARNING: _handle_session_start sweep failed ({label}): {exc}",
                    file=sys.stderr,
                )

        def _warm(scope: Scope, *, label: str) -> None:
            try:
                get_additional_dirs(conn, scope)
            except Exception as exc:  # noqa: BLE001 — cache refresh must not crash.
                print(
                    f"WARNING: _handle_session_start cache warm-up failed ({label}): {exc}",
                    file=sys.stderr,
                )

        # Sweep stale .tmp files and warm the additionalDirectories cache.
        _sweep(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;",
            label="global mirror",
        )
        _warm(Scope("global_mirror", 1), label="global mirror")
        if project_id is not None:
            _sweep(
                "SELECT settings_json_path FROM projects WHERE id = ?;",
                (project_id,),
                label="active project",
            )
            _warm(Scope("projects", project_id), label="active project")
    finally:
        conn.close()


def _ensure_db_bootstrapped() -> None:
    """First-run bootstrap: materialise the DB file + schema if missing.

    Writes to the resolved observations DB path. Parents are created with
    ``mkdir -p`` semantics. ``lib.db._open`` already executes ``schema.sql``
    when the DB is empty, so this helper just needs to touch the path
    (opening + closing a connection is enough).
    """
    db_path = observations_db_path()
    if db_path.exists():
        return
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    try:
        conn = _open()
    except (sqlite3.Error, RuntimeError):
        return
    conn.close()


def main() -> None:
    # Opt-out marker — every phase short-circuits silently.
    if is_disabled():
        return

    phase = sys.argv[1] if len(sys.argv) > 1 else "post"

    # First-run bootstrap happens for every phase. session_start is the
    # plugin's natural first invocation, but pre/post firing first (e.g.
    # after the opt-out marker is cleared mid-session) must also repair.
    _ensure_db_bootstrapped()

    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return
    if not raw.strip():
        return
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(data, dict):
        return

    try:
        if phase == "session_start":
            _handle_session_start(data)
            return
        if phase not in ("pre", "post"):
            phase = "post"
        _handle(phase, data)
    except Exception:  # noqa: BLE001 — never break the user's tool call.
        traceback.print_exc(file=sys.stderr)
        return


if __name__ == "__main__":
    main()
