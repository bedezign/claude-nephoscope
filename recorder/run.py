"""Observability recorder — pre/post tool-call hook handler.

Reads a Claude Code hook payload from stdin and writes (or updates) a row in
``tool_calls``. The phase is passed as ``sys.argv[1]`` (``pre`` or ``post``);
if missing, we default to ``post`` so the row still ends up complete.

Pre phase:
    INSERT row with ``status_id=<pending>``, ``tool_use_id``, ``ts=_now()``,
    ``completed_ts=NULL``, ``ok=NULL``, and FK columns populated from lookup
    tables (``tool_id``, ``subagent_type_id``, ``file_path_id``, integer
    ``session_id``). Capture top-level ``permission_mode`` via the lookup
    table, store the full payload (truncated) in ``tool_extras(name='payload')``,
    and — if present — set ``sessions.transcript_path`` (set-once-only).

Post phase:
    UPDATE the row matched by ``tool_use_id`` setting ``completed_ts``,
    ``status_id`` (ok/err) and ``ok``. Capture the ``tool_response``
    (truncated) in ``tool_extras(name='response')``. If no pending row
    exists (orphan post), INSERT a complete row so nothing is lost.

Malformed input is silently swallowed. Unhandled exceptions are printed to
stderr (the hook harness surfaces them back to the model) but we still exit
0 so the user's tool call is never broken by the recorder.

Phase 8.5: ``scope_id`` and the ``tool_call_scopes`` / ``tool_call_shapes``
tables were dropped (``path_spec`` on rule_shapes now carries the path axis).
The recorder no longer classifies paths or writes a scope FK.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

OBSERVABILITY_ROOT = Path(__file__).resolve().parent.parent
if str(OBSERVABILITY_ROOT) not in sys.path:
    sys.path.insert(0, str(OBSERVABILITY_ROOT))

from lib.db import (  # noqa: E402
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
    upsert_project,
    upsert_session,
    write_extra,
)

PAYLOAD_MAX = 4096
RESPONSE_MAX = 2048


def _flatten(tool: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Project a tool-input dict into the flat columns stored on tool_calls."""
    row: dict[str, Any] = {}
    if tool in ("Task", "Agent"):
        if (v := tool_input.get("subagent_type")) is not None:
            row["subagent_type"] = _truncate(v)
        if (v := tool_input.get("description")) is not None:
            row["description"] = _truncate(v)
    elif tool == "Bash":
        if (v := tool_input.get("command")) is not None:
            row["command"] = _truncate(v)
        if (v := tool_input.get("description")) is not None:
            row["description"] = _truncate(v)
    elif tool in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        if (v := tool_input.get("file_path")) is not None:
            row["file_path"] = _truncate(v)
    elif tool in ("Grep", "Glob"):
        if (v := tool_input.get("pattern")) is not None:
            row["pattern"] = _truncate(v)
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

        if phase == "pre":
            permission_mode_id = lookup_permission_mode_id(
                conn, data.get("permission_mode")
            )
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

            transcript_path = data.get("transcript_path")
            if transcript_path and session_id_int is not None:
                conn.execute(
                    "UPDATE sessions SET transcript_path = ? "
                    "WHERE id = ? AND transcript_path IS NULL;",
                    (transcript_path, session_id_int),
                )
            return

        # -- post phase --------------------------------------------------
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
    finally:
        conn.close()


def main() -> None:
    phase = sys.argv[1] if len(sys.argv) > 1 else "post"
    if phase not in ("pre", "post"):
        phase = "post"
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
        _handle(phase, data)
    except Exception:  # noqa: BLE001 — never break the user's tool call.
        traceback.print_exc(file=sys.stderr)
        return


if __name__ == "__main__":
    main()
