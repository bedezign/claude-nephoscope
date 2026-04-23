"""Database helpers for the observations module.

Single flat schema.sql, no schema versioning, no migration code. Helpers for
the permission tables (rule_shapes, permissions) and candidate tracking
(permission_candidates, permission_candidate_sessions).
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from pathlib import Path
from typing import Any

from nephoscope.lib.paths import observations_db_path

MAX_STR = 500


def _db_path() -> Path:
    """Resolve the observations DB path at call time.

    Delegates to :func:`nephoscope.lib.paths.observations_db_path`. Resolving
    lazily (instead of caching at import) lets tests use
    ``monkeypatch.setenv`` without also having to patch this module's globals.
    """
    return observations_db_path()


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
        return value[:MAX_STR] + "…"
    return value


def _open() -> sqlite3.Connection:
    """Open the observations DB with WAL mode enabled.

    If the DB file is empty, runs the schema.sql to bootstrap tables and views.
    Raises RuntimeError if schema.sql is missing when needed.
    """
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    # If DB is empty, load schema.
    cur = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table';")
    if cur.fetchone()[0] == 0:
        schema_path = Path(__file__).resolve().parent / "schema.sql"
        if not schema_path.exists():
            raise RuntimeError(f"schema.sql not found: {schema_path}")
        sql = schema_path.read_text(encoding="utf-8")
        conn.executescript(sql)

    return conn


def _project_name(cwd: str) -> str:
    """Derive a short project name from a cwd, stripping trailing `/repository`."""
    trimmed = cwd.removesuffix("/repository").rstrip("/")
    return Path(trimmed).name or trimmed or cwd


def _resolve_project_root(cwd: str) -> str | None:
    """Thin wrapper so lib/db.py doesn't import lib/scope.py at module load.

    scope imports subprocess which is heavy-ish, and db.py is used by hot
    paths that shouldn't pay that cost unless they actually create projects.
    """
    from nephoscope.lib.scope import resolve_project_root

    return resolve_project_root(cwd)


def upsert_project(conn: sqlite3.Connection, cwd: str, now: str) -> int:
    """Insert-or-touch a project row keyed by cwd; return its id.

    On first insertion, resolves and stores the project root.
    On subsequent touches, updates last_seen.
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


def upsert_session(
    conn: sqlite3.Connection, session_uuid: str, project_id: int | None, now: str
) -> int:
    """Insert-or-touch a session row keyed by UUID; return its INTEGER id.

    Returns the numeric id for FK writes on tool_calls.session_id,
    permission_candidate_sessions.session_id, etc.
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


def upsert_candidate(
    conn: sqlite3.Connection,
    verb: str,
    subcommand: str | None,
    flags_json: str,
    session_id: int,
    ts: str,
) -> int:
    """Insert-or-touch a permission candidate; track distinct sessions.

    Upserts permission_candidates row keyed by (verb, subcommand, flags).
    On touch, bumps last_seen and increments observations.
    On first occurrence for a session, increments distinct_sessions and
    inserts a permission_candidate_sessions row.

    Returns the permission_candidates.id.

    Args:
        conn: SQLite connection
        verb: command verb (e.g., "Read")
        subcommand: optional subcommand (e.g., "directory")
        flags_json: minified JSON array of flags
        session_id: sessions.id (numeric)
        ts: ISO-8601 timestamp
    """
    # Find or create the candidate row.
    row = conn.execute(
        "SELECT id FROM permission_candidates"
        " WHERE verb = ? AND IFNULL(subcommand, '') = IFNULL(?, '')"
        " AND flags = ?;",
        (verb, subcommand, flags_json),
    ).fetchone()

    if row is not None:
        cand_id = int(row[0])
        # Touch the row and increment observations.
        conn.execute(
            "UPDATE permission_candidates"
            " SET observations = observations + 1, last_seen = ?"
            " WHERE id = ?;",
            (ts, cand_id),
        )
    else:
        # New candidate.
        cur = conn.execute(
            "INSERT INTO permission_candidates"
            " (verb, subcommand, flags, observations, distinct_sessions,"
            "  first_seen, last_seen)"
            " VALUES (?, ?, ?, 1, 0, ?, ?);",
            (verb, subcommand, flags_json, ts, ts),
        )
        cand_id = int(cur.lastrowid or 0)

    # Check if this session has seen this candidate before.
    existing_session = conn.execute(
        "SELECT 1 FROM permission_candidate_sessions"
        " WHERE candidate_id = ? AND session_id = ?;",
        (cand_id, session_id),
    ).fetchone()

    if existing_session is None:
        # First occurrence for this session — bump distinct_sessions and insert tracking row.
        conn.execute(
            "UPDATE permission_candidates"
            " SET distinct_sessions = distinct_sessions + 1"
            " WHERE id = ?;",
            (cand_id,),
        )
        conn.execute(
            "INSERT INTO permission_candidate_sessions"
            " (candidate_id, session_id, last_seen)"
            " VALUES (?, ?, ?);",
            (cand_id, session_id, ts),
        )
    else:
        # Seen before in this session — just update last_seen.
        conn.execute(
            "UPDATE permission_candidate_sessions"
            " SET last_seen = ?"
            " WHERE candidate_id = ? AND session_id = ?;",
            (ts, cand_id, session_id),
        )

    return cand_id


def upsert_rule_shape(
    conn: sqlite3.Connection,
    verb: str,
    subcommand: str | None,
    flags_json: str,
    path_spec: str | None,
    ts: str,
) -> int:
    """Insert-or-touch a rule shape; return its id.

    Rule shapes are the basis for permission rules. They can carry patterns:
    - verb may be "$VAR/..." prefix
    - flags may be "*" wildcard
    - path_spec may be "$VAR/**" glob, "" (no paths), or NULL (any paths)

    Returns the rule_shapes.id.
    """
    row = conn.execute(
        "SELECT id FROM rule_shapes"
        " WHERE verb = ? AND IFNULL(subcommand, '') = IFNULL(?, '')"
        " AND flags = ? AND IFNULL(path_spec, '') = IFNULL(?, '');",
        (verb, subcommand, flags_json, path_spec),
    ).fetchone()

    if row is not None:
        shape_id = int(row[0])
        conn.execute(
            "UPDATE rule_shapes SET last_seen = ? WHERE id = ?;",
            (ts, shape_id),
        )
        return shape_id

    cur = conn.execute(
        "INSERT INTO rule_shapes(verb, subcommand, flags, path_spec,"
        "  first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?, ?);",
        (verb, subcommand, flags_json, path_spec, ts, ts),
    )
    return int(cur.lastrowid or 0)


def insert_permission(
    conn: sqlite3.Connection,
    rule_shape_id: int,
    session_id: int | None,
    project_id: int | None,
    decision: str,
    source: str,
    ts: str,
    reason: str | None = None,
) -> int:
    """Insert a permission decision row.

    Exactly one of session_id or project_id should be set (enforced by CHECK
    constraint in schema). If both are None, the permission is global-tier.

    Args:
        conn: SQLite connection
        rule_shape_id: rule_shapes.id
        session_id: sessions.id or None for non-session tiers
        project_id: projects.id or None for non-project tiers
        decision: 'approved' or 'rejected'
        source: 'session-ask', 'review', 'learner', 'seed', 'manual', 'migrated'
        ts: ISO-8601 timestamp
        reason: optional explanation

    Returns: permissions.id
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f"invalid decision: {decision!r}")

    cur = conn.execute(
        "INSERT INTO permissions"
        " (rule_shape_id, session_id, project_id, decision, source, reason,"
        "  decided_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?);",
        (rule_shape_id, session_id, project_id, decision, source, reason, ts),
    )
    return int(cur.lastrowid or 0)


def lookup_permissions(
    conn: sqlite3.Connection,
    rule_shape_id: int,
    session_id: int | None,
    project_id: int | None,
) -> list[dict[str, Any]]:
    """Look up permission decisions for a rule shape in tier priority.

    Returns a list of rows ordered by tier (session first, then project, then
    global). All matching rows are returned; caller typically takes the first
    (first decision wins). If a 'rejected' decision exists at any tier, it
    short-circuits.

    Args:
        conn: SQLite connection
        rule_shape_id: rule_shapes.id
        session_id: sessions.id or None (for session-tier lookup)
        project_id: projects.id or None (for project-tier lookup)

    Returns: list of dicts with keys: id, decision, source, reason, decided_at, session_id, project_id
    """
    rows = conn.execute(
        """
        SELECT id, decision, source, reason, decided_at, session_id, project_id
          FROM permissions
         WHERE rule_shape_id = ?
           AND (
             (session_id = ? AND session_id IS NOT NULL)
             OR (project_id = ? AND project_id IS NOT NULL)
             OR (session_id IS NULL AND project_id IS NULL)
           )
         ORDER BY
           CASE WHEN session_id IS NOT NULL THEN 0
                WHEN project_id IS NOT NULL THEN 1
                ELSE 2 END,
           decided_at DESC;
        """,
        (rule_shape_id, session_id, project_id),
    ).fetchall()

    return [
        {
            "id": row[0],
            "decision": row[1],
            "source": row[2],
            "reason": row[3],
            "decided_at": row[4],
            "session_id": row[5],
            "project_id": row[6],
        }
        for row in rows
    ]


# --- Existing v5–v7 helpers (unchanged) -------------------------------------------


def minify_json(obj: Any) -> str:
    """Dump obj to a compact JSON string (no whitespace, UTF-8 kept raw)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def lookup_permission_mode_id(conn: sqlite3.Connection, name: str | None) -> int | None:
    """Resolve a permission-mode name to its lookup id.

    Returns None if name is None or unknown.
    """
    if name is None:
        return None
    row = conn.execute(
        "SELECT id FROM permission_modes WHERE name = ?;", (name,)
    ).fetchone()
    return int(row[0]) if row is not None else None


def lookup_status_id(conn: sqlite3.Connection, name: str) -> int:
    """Resolve a call-status name to its lookup id.

    Raises ValueError if name is unknown.
    """
    row = conn.execute(
        "SELECT id FROM call_statuses WHERE name = ?;", (name,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown call status: {name!r}")
    return int(row[0])


def lookup_or_insert_tool_id(conn: sqlite3.Connection, name: str) -> int:
    """Resolve a tool name to its lookup id; insert on first sight."""
    row = conn.execute("SELECT id FROM tools WHERE name = ?;", (name,)).fetchone()
    if row is not None:
        return int(row[0])
    cur = conn.execute("INSERT INTO tools(name) VALUES (?);", (name,))
    return int(cur.lastrowid or 0)


def lookup_or_insert_subagent_type_id(
    conn: sqlite3.Connection, name: str | None
) -> int | None:
    """Resolve a subagent type to its lookup id; insert on first sight.

    Returns None when name is None.
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

    On conflict, bumps last_seen. Returns None when path is None.
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


def write_extra(
    conn: sqlite3.Connection, tool_call_id: int, name: str, value: str
) -> None:
    """Upsert a sidecar extras row. Latest value wins for a given name."""
    conn.execute(
        "INSERT OR REPLACE INTO tool_extras(tool_call_id, name, value)"
        " VALUES (?, ?, ?);",
        (tool_call_id, name, value),
    )
