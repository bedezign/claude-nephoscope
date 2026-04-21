"""Database helpers for the observability module.

Extracted from ``~/.claude/skills/continuous-learning-v2/hooks/observe.py`` and
adapted for the new top-level observability tree:

- Points at ``~/.cache/claude/observability/observations.db`` by default
  (overridable via ``OBSERVABILITY_DB``).
- ``_migrate(conn)`` walks ``lib/schema/v*.sql`` on disk instead of an inline
  Python list — new schema versions land by dropping a file in place.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(
    os.environ.get(
        "OBSERVABILITY_DB",
        Path.home() / ".cache" / "claude" / "observability" / "observations.db",
    )
)

# Directory holding vN.sql files (walked in order by _migrate).
SCHEMA_DIR = Path(__file__).resolve().parent / "schema"

MAX_STR = 500

_SCHEMA_FILE_RE = re.compile(r"^v(\d+)\.sql$")


def _now() -> str:
    """UTC timestamp in ISO-8601 with millisecond precision, `Z`-suffixed."""
    return (
        _dt.datetime.now(tz=_dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _truncate(value: Any) -> Any:
    """Cap string length at MAX_STR (adds an ellipsis when clipped)."""
    if isinstance(value, str) and len(value) > MAX_STR:
        return value[:MAX_STR] + "\u2026"
    return value


def _open() -> sqlite3.Connection:
    """Open the observations DB with WAL mode enabled."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _discover_schema_files() -> list[tuple[int, Path]]:
    """Return ``[(version, path), ...]`` sorted by version, for vN.sql files."""
    if not SCHEMA_DIR.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for entry in SCHEMA_DIR.iterdir():
        if not entry.is_file():
            continue
        match = _SCHEMA_FILE_RE.match(entry.name)
        if match is None:
            continue
        found.append((int(match.group(1)), entry))
    found.sort(key=lambda item: item[0])
    return found


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply every unapplied migration under ``lib/schema/`` in order.

    Reads ``PRAGMA user_version``; for each ``vN.sql`` with N > current,
    executes the file and bumps ``user_version`` to N. Leaves user_version at
    the highest version successfully applied.
    """
    current = conn.execute("PRAGMA user_version;").fetchone()[0]
    pending = [(v, p) for (v, p) in _discover_schema_files() if v > current]
    if not pending:
        return
    for version, path in pending:
        sql = path.read_text(encoding="utf-8")
        conn.executescript(sql)
        # PRAGMA does not accept parameter binding, but version came from a
        # regex-matched integer so interpolation is safe.
        conn.execute(f"PRAGMA user_version = {int(version)};")


def _project_name(cwd: str) -> str:
    """Derive a short project name from a cwd, stripping trailing `/repository`."""
    trimmed = cwd.removesuffix("/repository").rstrip("/")
    return Path(trimmed).name or trimmed or cwd


def _upsert_project(conn: sqlite3.Connection, cwd: str, now: str) -> int:
    """Insert-or-touch a project row keyed by cwd; returns its id.

    On first insertion, resolves and stores the project root (see
    ``lib.scope.resolve_project_root``). On subsequent touches, if the row
    is missing a root (pre-v11 data), backfills it lazily — the resolver is
    cheap enough to amortize and keeps older projects usable.
    """
    row = conn.execute(
        "SELECT id, root FROM projects WHERE cwd = ?;", (cwd,)
    ).fetchone()
    if row is not None:
        proj_id = int(row[0])
        if row[1] is None:
            root = _resolve_project_root(cwd)
            conn.execute(
                "UPDATE projects SET last_seen = ?, root = ? WHERE id = ?;",
                (now, root, proj_id),
            )
        else:
            conn.execute(
                "UPDATE projects SET last_seen = ? WHERE id = ?;", (now, proj_id)
            )
        return proj_id
    root = _resolve_project_root(cwd)
    cur = conn.execute(
        "INSERT INTO projects(cwd, name, root, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?);",
        (cwd, _project_name(cwd), root, now, now),
    )
    return int(cur.lastrowid or 0)


def _resolve_project_root(cwd: str) -> str | None:
    """Thin wrapper so lib/db.py doesn't import lib/scope.py at module load
    (scope imports subprocess which is heavy-ish, and db.py is used by hot
    paths that shouldn't pay that cost unless they actually create projects).
    """
    from .scope import resolve_project_root

    return resolve_project_root(cwd)


def _upsert_session(
    conn: sqlite3.Connection, session_uuid: str, project_id: int, now: str
) -> int:
    """Insert-or-touch a session row keyed by UUID; return its INTEGER id.

    Post-v7 the sessions table is INTEGER-keyed with the UUID living on the
    `session_uuid` UNIQUE column. Callers (recorder, learner) hold the UUID
    from the hook payload and need the numeric id for FK writes on
    `tool_calls.session_id_new` / `permission_candidate_sessions.session_id_new`.

    Positional signature is unchanged from pre-v7 — the second parameter has
    always semantically been a UUID, only the column name flipped. Existing
    call sites that ignore the return value keep working.
    """
    row = conn.execute(
        "SELECT id FROM sessions WHERE session_uuid = ?;", (session_uuid,)
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE sessions SET last_activity = ?,"
            " project_id = COALESCE(project_id, ?) WHERE id = ?;",
            (now, project_id, row[0]),
        )
        return int(row[0])
    cur = conn.execute(
        "INSERT INTO sessions(session_uuid, project_id, started_at, last_activity)"
        " VALUES (?, ?, ?, ?);",
        (session_uuid, project_id, now, now),
    )
    return int(cur.lastrowid or 0)


# --- v5 helpers --------------------------------------------------------------
# These support the Phase 3.5 schema expansion: int-FK lookups for
# permission_mode and status, the command_shapes registry with its M2M
# junction, session transcript attribution, the tool_extras sidecar for
# heavy text, and a JSON-minification helper.


def minify_json(obj: Any) -> str:
    """Dump ``obj`` to a compact JSON string (no whitespace, UTF-8 kept raw).

    Used wherever we store JSON in SQLite — saves bytes in sidecar/args_json
    rows without destroying SQL inspectability.
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def lookup_permission_mode_id(conn: sqlite3.Connection, name: str | None) -> int | None:
    """Resolve a permission-mode name to its lookup id.

    Returns ``None`` if ``name`` is ``None`` (recorder uses this for payloads
    missing the field) or unknown (so unexpected values don't crash the
    recorder — the row just has a NULL FK). Callers that need strict
    validation should check for unknown names themselves.
    """
    if name is None:
        return None
    row = conn.execute(
        "SELECT id FROM permission_modes WHERE name = ?;", (name,)
    ).fetchone()
    return int(row[0]) if row is not None else None


def lookup_status_id(conn: sqlite3.Connection, name: str) -> int:
    """Resolve a call-status name to its lookup id.

    Raises ``ValueError`` if ``name`` is not a known status — statuses are
    a closed set (``pending | ok | err | denied | orphan``) and an unknown
    value is a bug in the caller, not data the recorder should silently
    accept.
    """
    row = conn.execute(
        "SELECT id FROM call_statuses WHERE name = ?;", (name,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown call status: {name!r}")
    return int(row[0])


def lookup_scope_id(conn: sqlite3.Connection, name: str) -> int:
    """Resolve a tool-call-scope name to its lookup id.

    Closed set (``within_project | outside_project | mixed | no_path |
    any``); unknown name is a bug, not data to accept. ``any`` is for
    permission rules and is never written to ``tool_calls.scope_id``.
    """
    row = conn.execute(
        "SELECT id FROM tool_call_scopes WHERE name = ?;", (name,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown tool_call_scope: {name!r}")
    return int(row[0])


def register_ask_pending(
    conn: sqlite3.Connection,
    tool_use_id: str,
    leaf_index: int,
    session_id: int,
    command_shape_id: int,
    scope_id: int,
    asked_at: str,
) -> None:
    """Write one pending row per ask'd leaf. Re-ask of same tool_use_id +
    leaf is idempotent (INSERT OR IGNORE); the hook only fires once per
    call so duplicates are unusual, but we guard against weird retries.
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO permission_ask_pending
          (tool_use_id, leaf_index, session_id, command_shape_id, scope_id, asked_at)
        VALUES (?, ?, ?, ?, ?, ?);
        """,
        (tool_use_id, leaf_index, session_id, command_shape_id, scope_id, asked_at),
    )


def promote_pending_to_session_approval(
    conn: sqlite3.Connection,
    tool_use_id: str,
    approved_at: str,
) -> int:
    """Move every pending row for ``tool_use_id`` into session_approvals.

    Returns the number of rows promoted. Called by the Post phase on
    status=ok. Idempotent via composite PK — re-runs never duplicate.
    Pending rows are always deleted, whether promoted or not (the caller
    only calls this on ok; err paths call ``drop_pending``).
    """
    rows = conn.execute(
        """
        SELECT session_id, command_shape_id, scope_id
          FROM permission_ask_pending
         WHERE tool_use_id = ?;
        """,
        (tool_use_id,),
    ).fetchall()
    for session_id, shape_id, scope_id in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO permission_session_approvals
              (session_id, command_shape_id, scope_id, approved_at)
            VALUES (?, ?, ?, ?);
            """,
            (session_id, shape_id, scope_id, approved_at),
        )
    conn.execute(
        "DELETE FROM permission_ask_pending WHERE tool_use_id = ?;",
        (tool_use_id,),
    )
    return len(rows)


def drop_pending(conn: sqlite3.Connection, tool_use_id: str) -> int:
    """Delete all ask_pending rows for a tool_use_id (Post phase on err)."""
    cur = conn.execute(
        "DELETE FROM permission_ask_pending WHERE tool_use_id = ?;",
        (tool_use_id,),
    )
    return cur.rowcount


def upsert_command_shape(
    conn: sqlite3.Connection,
    verb: str,
    subcommand: str | None,
    flags_json: str,
    ts: str,
) -> int:
    """Insert-or-touch a command shape; return its id.

    ``flags_json`` is the caller's already-minified JSON array (keep the
    encoding policy in one place — the canonicalizer — rather than here).
    The matching partial UNIQUE index uses ``IFNULL(subcommand, '')``, so
    NULL and missing subcommand collapse to the same shape.
    """
    row = conn.execute(
        "SELECT id FROM command_shapes"
        " WHERE verb = ? AND IFNULL(subcommand, '') = IFNULL(?, '')"
        " AND flags = ?;",
        (verb, subcommand, flags_json),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE command_shapes SET last_seen = ? WHERE id = ?;",
            (ts, row[0]),
        )
        return int(row[0])
    cur = conn.execute(
        "INSERT INTO command_shapes(verb, subcommand, flags, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?);",
        (verb, subcommand, flags_json, ts, ts),
    )
    return int(cur.lastrowid or 0)


def link_tool_call_shape(
    conn: sqlite3.Connection,
    tool_call_id: int,
    command_shape_id: int,
    leaf_index: int,
) -> None:
    """Attach a canonical leaf to a tool_call row. Idempotent per (call, leaf)."""
    conn.execute(
        "INSERT OR IGNORE INTO tool_call_shapes"
        "(tool_call_id, command_shape_id, leaf_index) VALUES (?, ?, ?);",
        (tool_call_id, command_shape_id, leaf_index),
    )


def set_session_transcript_path(
    conn: sqlite3.Connection, session_id: int, path: str
) -> None:
    """Record the transcript path for a session — set-once semantics.

    Only writes when the existing value is NULL, so later payloads with a
    stale or rotated path don't overwrite the original. ``session_id`` is
    the INTEGER ``sessions.id`` (post-v7), not the UUID string.
    """
    conn.execute(
        "UPDATE sessions SET transcript_path = ?"
        " WHERE id = ? AND transcript_path IS NULL;",
        (path, session_id),
    )


def write_extra(
    conn: sqlite3.Connection, tool_call_id: int, name: str, value: str
) -> None:
    """Upsert a sidecar extras row. Latest value wins for a given name."""
    conn.execute(
        "INSERT OR REPLACE INTO tool_extras(tool_call_id, name, value)"
        " VALUES (?, ?, ?);",
        (tool_call_id, name, value),
    )


# --- v7 helpers --------------------------------------------------------------
# Lookup-or-insert helpers for the new int-FK lookup tables introduced in
# Phase 3.6. Shape mirrors the v5 pattern (lookup_permission_mode_id /
# lookup_status_id) but with insert-on-miss semantics — the value space here
# is open (tool names, subagent types, file paths are discovered at runtime)
# rather than the closed enums v5 seeded up-front.


def lookup_or_insert_tool_id(conn: sqlite3.Connection, name: str) -> int:
    """Resolve a tool name to its lookup id; insert it on first sight.

    Callers pass the Claude Code hook's `tool_name` field directly; unknown
    values auto-register so the recorder never needs a schema update when a
    new tool ships.
    """
    row = conn.execute("SELECT id FROM tools WHERE name = ?;", (name,)).fetchone()
    if row is not None:
        return int(row[0])
    cur = conn.execute("INSERT INTO tools(name) VALUES (?);", (name,))
    return int(cur.lastrowid or 0)


def lookup_or_insert_subagent_type_id(
    conn: sqlite3.Connection, name: str | None
) -> int | None:
    """Resolve a subagent type to its lookup id; insert on first sight.

    Returns ``None`` when ``name`` is ``None`` — the column is nullable on
    `tool_calls` because most calls aren't Agent invocations.
    """
    if name is None:
        return None
    row = conn.execute(
        "SELECT id FROM subagent_types WHERE name = ?;", (name,)
    ).fetchone()
    if row is not None:
        return int(row[0])
    cur = conn.execute("INSERT INTO subagent_types(name) VALUES (?);", (name,))
    return int(cur.lastrowid or 0)


def lookup_or_insert_file_path_id(
    conn: sqlite3.Connection, path: str | None, ts: str
) -> int | None:
    """Resolve a file path to its lookup id; insert on first sight.

    On conflict (path already present) this also bumps `last_seen` so the
    registry doubles as a cheap "paths touched recently" index.

    Returns ``None`` when ``path`` is ``None`` — Read/Edit/Write carry it,
    but Bash/Grep/Glob/etc. don't.
    """
    if path is None:
        return None
    row = conn.execute("SELECT id FROM file_paths WHERE path = ?;", (path,)).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE file_paths SET last_seen = ? WHERE id = ?;",
            (ts, row[0]),
        )
        return int(row[0])
    cur = conn.execute(
        "INSERT INTO file_paths(path, first_seen, last_seen) VALUES (?, ?, ?);",
        (path, ts, ts),
    )
    return int(cur.lastrowid or 0)
