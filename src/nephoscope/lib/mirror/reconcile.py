"""Reconcile engine: diff DB permissions vs JSON mirror entries, apply resolution.

Public API
----------
diff(db_rows, json_rows) -> Diff       Pure diff computation, no side effects.
reconcile(conn, target_path, *, mode)  Full orchestration: diff → resolve → apply → sync.

Data structures
---------------
Diff           Structured diff: only_in_db, only_in_json, conflicting, matching.
ConflictEntry  Same logical key in both DB and JSON but with different decisions.
Resolution     DB_WINS / JSON_WINS / PER_ENTRY.
ReconcileReport Result returned by reconcile().
ReconcileError  Base exception for this module.

Modes
-----
plan           Return diff only; no side effects.
interactive    Prompt user (via input()) for bulk or per-entry resolution.
               Auto-switches to 'adopt' when mirror hash is NULL (first-touch).
auto-db-wins   Apply DB_WINS non-interactively.
auto-json-wins Apply JSON_WINS non-interactively.
adopt          Same as auto-json-wins; semantically labeled for first-touch UX.

Logical key
-----------
Rows are matched on (tool, verb, subcommand, flags, path_spec).
The conflict dimension is decision (allow / deny / ask) — not part of the key.

Decision normalization
----------------------
DB stores: 'approved' / 'rejected' / 'ask'
JSON stores: 'allow' / 'deny' / 'ask'
Mapping: approved ↔ allow, rejected ↔ deny, ask ↔ ask.

Resolution semantics
--------------------
JSON_WINS:
  only_in_json → INSERT into DB (source='reconcile-adopt')
  only_in_db   → DELETE from DB
  conflicting  → UPDATE DB decision to match JSON

DB_WINS:
  only_in_json → dropped (not inserted into DB; absent from regenerated JSON)
  only_in_db   → kept in DB; regenerated JSON includes it
  conflicting  → DB decision retained; regenerated JSON reflects DB

After any DB mutations, writer.sync_* regenerates the JSON mirror and stamps hash.
"""

from __future__ import annotations

import enum
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import datetime as _dt

from nephoscope.lib.mirror.ingester import parse_permissions_json, IngesterError  # noqa: F401 (re-exported for tests)
from nephoscope.lib.mirror.tool_class import classify


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ReconcileError(Exception):
    """Base exception for reconcile engine failures."""


# ---------------------------------------------------------------------------
# Decision normalization
# ---------------------------------------------------------------------------

_DB_TO_JSON: dict[str, str] = {
    "approved": "allow",
    "rejected": "deny",
    "ask": "ask",
}

_JSON_TO_DB: dict[str, str] = {
    "allow": "approved",
    "deny": "rejected",
    "ask": "ask",
}


# ---------------------------------------------------------------------------
# Resolution enum
# ---------------------------------------------------------------------------


class Resolution(enum.Enum):
    """Top-level resolution strategy for a reconcile operation."""

    DB_WINS = "db-wins"
    JSON_WINS = "json-wins"
    PER_ENTRY = "per-entry"


# ---------------------------------------------------------------------------
# Logical key helpers
# ---------------------------------------------------------------------------

# (tool, verb, subcommand, flags, path_spec)
LogicalKey = tuple[str, str, str | None, str | None, str | None]


def _norm_flags(flags: str | None) -> str:
    """Canonical flags axis: None and '[]' both mean 'no flags'.

    The DB stores '[]' (see reconcile INSERT path); the ingester emits None
    for non-Bash tools. Without this, every file/MCP/orchestration rule
    diffs as only_in_db + only_in_json on real reconcile runs.
    """
    return flags if flags else "[]"


def _key_from_db_row(row: dict[str, Any]) -> LogicalKey:
    """Derive logical key from a DB permissions+rule_shapes joined row.

    For bash-class verbs the outer tool name is 'Bash'; for all other
    tool classes tool == verb.
    """
    verb: str = row["verb"]
    tc = classify(verb)
    tool = "Bash" if tc == "bash" else verb
    return (
        tool,
        verb,
        row.get("subcommand"),
        _norm_flags(row.get("flags")),
        row.get("path_spec"),
    )


def _key_from_json_row(row: dict[str, Any]) -> LogicalKey:
    """Derive logical key from an ingester-produced JSON row."""
    return (
        row["tool"],
        row["verb"],
        row.get("subcommand"),
        _norm_flags(row.get("flags")),
        row.get("path_spec"),
    )


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ConflictEntry:
    """A rule present in both DB and JSON but with differing decisions."""

    key: LogicalKey
    db_row: dict[str, Any]
    json_row: dict[str, Any]
    db_decision: str  # normalized to allow/deny/ask
    json_decision: str  # allow/deny/ask


@dataclass
class Diff:
    """Structured diff between DB rows and JSON rows for one mirror scope."""

    only_in_db: list[dict[str, Any]] = field(default_factory=list)
    only_in_json: list[dict[str, Any]] = field(default_factory=list)
    conflicting: list[ConflictEntry] = field(default_factory=list)
    matching: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when DB and JSON are fully in sync (no mutations needed)."""
        return not self.only_in_db and not self.only_in_json and not self.conflicting


@dataclass
class ReconcileReport:
    """Result of a reconcile() call."""

    mode: str  # Effective mode (may differ from requested when first-touch)
    diff: Diff  # The computed diff
    applied: bool  # Whether DB mutations were applied
    db_inserts: int = 0  # Rows inserted into DB
    db_deletes: int = 0  # Rows deleted from DB
    db_updates: int = 0  # Rows updated in DB
    first_touch: bool = False  # True when hash was NULL → adopt auto-triggered


# ---------------------------------------------------------------------------
# Public: pure diff
# ---------------------------------------------------------------------------


def diff(
    db_rows: list[dict[str, Any]],
    json_rows: list[dict[str, Any]],
) -> Diff:
    """Compute the diff between DB permission rows and JSON permission rows.

    Matching is by logical key (tool, verb, subcommand, flags, path_spec).
    Conflict detection is by decision (allow/deny/ask).

    Parameters
    ----------
    db_rows:
        Dicts from a ``permissions JOIN rule_shapes`` query.
        Required keys: verb, subcommand, flags, path_spec, decision, id.
        decision values: 'approved', 'rejected', 'ask'.

    json_rows:
        Dicts from ``ingester.parse_permissions_json``.
        Required keys: tool, verb, subcommand, flags, path_spec, decision.
        decision values: 'allow', 'deny', 'ask'.

    Returns
    -------
    Diff with only_in_db, only_in_json, conflicting, matching populated.
    """
    db_map: dict[LogicalKey, dict[str, Any]] = {}
    for row in db_rows:
        key = _key_from_db_row(row)
        db_map[key] = row

    json_map: dict[LogicalKey, dict[str, Any]] = {}
    for row in json_rows:
        key = _key_from_json_row(row)
        json_map[key] = row

    result = Diff()

    for key, db_row in db_map.items():
        if key not in json_map:
            result.only_in_db.append(db_row)
        else:
            json_row = json_map[key]
            raw_db_decision: str = db_row["decision"]
            db_decision_norm: str = _DB_TO_JSON.get(raw_db_decision, raw_db_decision)
            json_decision: str = json_row["decision"]
            if db_decision_norm == json_decision:
                result.matching.append(db_row)
            else:
                result.conflicting.append(
                    ConflictEntry(
                        key=key,
                        db_row=db_row,
                        json_row=json_row,
                        db_decision=db_decision_norm,
                        json_decision=json_decision,
                    )
                )

    for key, json_row in json_map.items():
        if key not in db_map:
            result.only_in_json.append(json_row)

    return result


# ---------------------------------------------------------------------------
# Internal: scope detection
# ---------------------------------------------------------------------------


def _detect_scope(
    conn: sqlite3.Connection, target_path: Path
) -> tuple[int | None, bool]:
    """Return (project_id, is_global) for target_path.

    Compares resolved absolute paths.

    Raises ReconcileError if the path is not registered in DB.
    """
    target_abs = str(target_path.expanduser().resolve())

    row = conn.execute(
        "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
    ).fetchone()
    if row and row[0]:
        global_abs = str(Path(row[0]).expanduser().resolve())
        if global_abs == target_abs:
            return None, True

    # Check project rows.
    all_projects = conn.execute(
        "SELECT id, settings_json_path FROM projects"
        " WHERE settings_json_path IS NOT NULL;"
    ).fetchall()
    for proj_id, proj_path in all_projects:
        if str(Path(proj_path).expanduser().resolve()) == target_abs:
            return proj_id, False

    raise ReconcileError(
        f"Cannot determine scope for {target_path}: "
        "not registered as the global mirror path or any project path. "
        "Set settings_json_path in global_mirror or projects before reconciling."
    )


# ---------------------------------------------------------------------------
# Internal: DB row reader
# ---------------------------------------------------------------------------


def _read_db_rows(
    conn: sqlite3.Connection, project_id: int | None
) -> list[dict[str, Any]]:
    """Read non-session permission rows for the given scope."""
    if project_id is None:
        rows = conn.execute(
            """
            SELECT p.id, p.decision,
                   rs.verb, rs.subcommand, rs.flags, rs.path_spec
              FROM permissions p
              JOIN rule_shapes rs ON rs.id = p.rule_shape_id
             WHERE p.project_id IS NULL AND p.session_id IS NULL
             ORDER BY p.decided_at ASC, p.id ASC;
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT p.id, p.decision,
                   rs.verb, rs.subcommand, rs.flags, rs.path_spec
              FROM permissions p
              JOIN rule_shapes rs ON rs.id = p.rule_shape_id
             WHERE p.project_id = ? AND p.session_id IS NULL
             ORDER BY p.decided_at ASC, p.id ASC;
            """,
            (project_id,),
        ).fetchall()

    return [
        {
            "id": r[0],
            "decision": r[1],
            "verb": r[2],
            "subcommand": r[3],
            "flags": r[4],
            "path_spec": r[5],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Internal: first-touch detection
# ---------------------------------------------------------------------------


def _is_first_touch(conn: sqlite3.Connection, project_id: int | None) -> bool:
    """Return True when the stored mirror hash is NULL (first-touch scenario)."""
    if project_id is None:
        row = conn.execute(
            "SELECT settings_json_sha256 FROM global_mirror WHERE id = 1;"
        ).fetchone()
        return row is None or row[0] is None
    row = conn.execute(
        "SELECT settings_json_sha256 FROM projects WHERE id = ?;",
        (project_id,),
    ).fetchone()
    return row is None or row[0] is None


# ---------------------------------------------------------------------------
# Internal: mirror sync dispatcher
# ---------------------------------------------------------------------------


def _sync(conn: sqlite3.Connection, project_id: int | None) -> None:
    """Regenerate the JSON mirror and stamp the hash.

    Delegates to writer.sync_global or writer.sync_project.
    """
    from nephoscope.lib.mirror import (
        writer,
    )  # late import: avoids circular at module load

    if project_id is None:
        writer.sync_global(conn)
    else:
        writer.sync_project(conn, project_id)


# ---------------------------------------------------------------------------
# Internal: apply JSON_WINS mutations
# ---------------------------------------------------------------------------


def _now_ts() -> str:
    return (
        _dt.datetime.now(tz=_dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _upsert_rule_shape(
    conn: sqlite3.Connection,
    verb: str,
    subcommand: str | None,
    flags: str | None,
    path_spec: str | None,
    ts: str,
) -> int:
    """Find or insert a rule_shapes row; return its id."""
    flags_stored = flags if flags is not None else "[]"
    row = conn.execute(
        "SELECT id FROM rule_shapes"
        " WHERE verb = ?"
        " AND IFNULL(subcommand, '') = IFNULL(?, '')"
        " AND flags = ?"
        " AND IFNULL(path_spec, '') = IFNULL(?, '');",
        (verb, subcommand, flags_stored, path_spec),
    ).fetchone()
    if row is not None:
        conn.execute("UPDATE rule_shapes SET last_seen = ? WHERE id = ?;", (ts, row[0]))
        return int(row[0])
    cur = conn.execute(
        "INSERT INTO rule_shapes(verb, subcommand, flags, path_spec, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?, ?);",
        (verb, subcommand, flags_stored, path_spec, ts, ts),
    )
    return int(cur.lastrowid or 0)


def _apply_json_wins(
    conn: sqlite3.Connection,
    d: Diff,
    project_id: int | None,
) -> tuple[int, int, int]:
    """Apply JSON_WINS resolution: insert/delete/update DB to match JSON.

    Returns (inserts, deletes, updates).
    """
    ts = _now_ts()
    inserts = deletes = updates = 0

    for json_row in d.only_in_json:
        shape_id = _upsert_rule_shape(
            conn,
            verb=json_row["verb"],
            subcommand=json_row.get("subcommand"),
            flags=json_row.get("flags"),
            path_spec=json_row.get("path_spec"),
            ts=ts,
        )
        db_decision = _JSON_TO_DB[json_row["decision"]]
        try:
            conn.execute(
                "INSERT INTO permissions"
                " (rule_shape_id, session_id, project_id, decision, source, reason, decided_at)"
                " VALUES (?, NULL, ?, ?, 'reconcile-adopt', NULL, ?);",
                (shape_id, project_id, db_decision, ts),
            )
        except Exception as exc:
            raise ReconcileError(
                f"Failed to insert permission for verb={json_row['verb']!r}"
                f" decision={db_decision!r}: {exc}"
            ) from exc
        inserts += 1

    for db_row in d.only_in_db:
        conn.execute(
            "DELETE FROM permissions WHERE id = ?;",
            (db_row["id"],),
        )
        deletes += 1

    for entry in d.conflicting:
        db_decision = _JSON_TO_DB[entry.json_decision]
        conn.execute(
            "UPDATE permissions SET decision = ? WHERE id = ?;",
            (db_decision, entry.db_row["id"]),
        )
        updates += 1

    return inserts, deletes, updates


# ---------------------------------------------------------------------------
# Internal: interactive prompting
# ---------------------------------------------------------------------------


def _prompt_bulk(d: Diff) -> Resolution:
    """Print diff summary and ask for a bulk resolution choice."""
    print(
        f"\nReconcile diff:"
        f"  only in DB   : {len(d.only_in_db)}"
        f"  only in JSON : {len(d.only_in_json)}"
        f"  conflicting  : {len(d.conflicting)}"
    )
    while True:
        choice = (
            input("Resolve all: [d]b-wins / [j]son-wins / [p]er-entry? ")
            .strip()
            .lower()
        )
        if choice in ("d", "db", "db-wins"):
            return Resolution.DB_WINS
        if choice in ("j", "json", "json-wins"):
            return Resolution.JSON_WINS
        if choice in ("p", "per", "per-entry"):
            return Resolution.PER_ENTRY
        print(f"  Unknown choice {choice!r}; enter d, j, or p.")


def _fmt_key(key: LogicalKey) -> str:
    tool, verb, sub, flags, path = key
    parts = [f"tool={tool}"]
    if tool != verb:
        parts.append(f"verb={verb}")
    if sub:
        parts.append(f"sub={sub}")
    if flags and flags != "[]":
        parts.append(f"flags={flags}")
    if path:
        parts.append(f"path={path}")
    return " ".join(parts)


def _prompt_json_only(json_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prompt the user to add or skip each JSON-only row. Returns rows to insert."""
    to_insert: list[dict[str, Any]] = []
    for json_row in json_rows:
        key = _key_from_json_row(json_row)
        print(f"\n  Only in JSON: {_fmt_key(key)}  decision={json_row['decision']}")
        while True:
            c = input("  [a]dd to DB or [s]kip? ").strip().lower()
            if c in ("a", "add"):
                to_insert.append(json_row)
                break
            if c in ("s", "skip"):
                break
            print("  Enter a or s.")
    return to_insert


def _prompt_db_only(db_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prompt the user to keep or remove each DB-only row. Returns rows to delete."""
    to_delete: list[dict[str, Any]] = []
    for db_row in db_rows:
        key = _key_from_db_row(db_row)
        db_dec = _DB_TO_JSON.get(db_row["decision"], db_row["decision"])
        print(f"\n  Only in DB: {_fmt_key(key)}  decision={db_dec}")
        while True:
            c = input("  [k]eep in DB or [r]emove from DB? ").strip().lower()
            if c in ("k", "keep"):
                break
            if c in ("r", "remove"):
                to_delete.append(db_row)
                break
            print("  Enter k or r.")
    return to_delete


def _prompt_conflicts(
    conflicting: list[ConflictEntry],
) -> list[tuple[dict[str, Any], str]]:
    """Prompt the user to resolve each conflict. Returns (db_row, new_db_decision) pairs."""
    updates: list[tuple[dict[str, Any], str]] = []
    for entry in conflicting:
        print(
            f"\n  Conflict: {_fmt_key(entry.key)}"
            f"  DB={entry.db_decision} vs JSON={entry.json_decision}"
        )
        while True:
            c = (
                input(
                    f"  Use [d]b ({entry.db_decision}) or [j]son ({entry.json_decision})? "
                )
                .strip()
                .lower()
            )
            if c in ("d", "db"):
                break  # DB wins: no update needed
            if c in ("j", "json"):
                updates.append((entry.db_row, _JSON_TO_DB[entry.json_decision]))
                break
            print("  Enter d or j.")
    return updates


def _prompt_per_entry(
    d: Diff,
) -> tuple[
    list[dict[str, Any]],  # only_in_json to insert
    list[dict[str, Any]],  # only_in_db to delete
    list[tuple[dict[str, Any], str]],  # (conflicting.db_row, winning_db_decision)
]:
    """Prompt user for each diff item; return lists of actions to apply."""
    to_insert = _prompt_json_only(d.only_in_json)
    to_delete = _prompt_db_only(d.only_in_db)
    conflict_updates = _prompt_conflicts(d.conflicting)
    return to_insert, to_delete, conflict_updates


def _apply_per_entry_actions(
    conn: sqlite3.Connection,
    to_insert: list[dict[str, Any]],
    to_delete: list[dict[str, Any]],
    conflict_updates: list[tuple[dict[str, Any], str]],
    project_id: int | None,
) -> tuple[int, int, int]:
    """Apply per-entry resolved actions to the DB."""
    ts = _now_ts()
    inserts = deletes = updates = 0

    for json_row in to_insert:
        shape_id = _upsert_rule_shape(
            conn,
            verb=json_row["verb"],
            subcommand=json_row.get("subcommand"),
            flags=json_row.get("flags"),
            path_spec=json_row.get("path_spec"),
            ts=ts,
        )
        db_decision = _JSON_TO_DB[json_row["decision"]]
        try:
            conn.execute(
                "INSERT INTO permissions"
                " (rule_shape_id, session_id, project_id, decision, source, reason, decided_at)"
                " VALUES (?, NULL, ?, ?, 'reconcile-adopt', NULL, ?);",
                (shape_id, project_id, db_decision, ts),
            )
        except Exception as exc:
            raise ReconcileError(
                f"Failed to insert permission for verb={json_row['verb']!r}"
                f" decision={db_decision!r}: {exc}"
            ) from exc
        inserts += 1

    for db_row in to_delete:
        conn.execute("DELETE FROM permissions WHERE id = ?;", (db_row["id"],))
        deletes += 1

    for db_row, new_db_decision in conflict_updates:
        conn.execute(
            "UPDATE permissions SET decision = ? WHERE id = ?;",
            (new_db_decision, db_row["id"]),
        )
        updates += 1

    return inserts, deletes, updates


# ---------------------------------------------------------------------------
# Public: reconcile
# ---------------------------------------------------------------------------


def _stamp_additional_dirs_cache(
    conn: sqlite3.Connection,
    project_id: int | None,
    target_path: Path,
    raw_data: dict[str, Any],
    mtime: float,
) -> None:
    """Persist settings_json_mtime + additional_dirs into the cache row.

    ``raw_data`` is the already-parsed settings.json dict.  ``mtime`` must
    have been captured *before* any write that would advance the file mtime
    (i.e. before the reconcile sync call).

    This function runs in every reconcile mode, including ``plan``.  The cache
    tracks denormalized file metadata (mtime + directory list) derived from the
    file on disk — it is independent of reconcile's permission-resolution
    decisions.  Updating it in plan mode is intentional: plan mode reads the
    file to compute the diff, so stamping the mtime and dirs at that point is
    correct and lets callers of ``get_additional_dirs`` benefit from the warm
    cache without triggering a second file read.

    Harmless when the row doesn't exist yet (UPDATE touches zero rows).
    """
    dirs: list[str] = [
        str(d)
        for d in (
            (raw_data.get("permissions") or {}).get("additionalDirectories") or []
        )
    ]
    table = "global_mirror" if project_id is None else "projects"
    id_clause = "id = 1" if project_id is None else "id = ?"
    id_args: tuple[int, ...] = () if project_id is None else (project_id,)
    cur = conn.execute(
        f"UPDATE {table}"
        f" SET settings_json_mtime = ?, additional_dirs = ?"
        f" WHERE {id_clause};",
        (mtime, json.dumps(dirs)) + id_args,
    )
    if cur.rowcount == 0:
        row_id = 1 if project_id is None else project_id
        print(
            f"WARNING: _stamp_additional_dirs_cache: no row updated"
            f" (table={table}, id={row_id});"
            f" cache will fall back to slow path until row exists",
            file=sys.stderr,
        )


_VALID_MODES = frozenset(
    {"interactive", "plan", "auto-db-wins", "auto-json-wins", "adopt"}
)


def reconcile(
    conn: sqlite3.Connection,
    target_path: "Path | str",
    *,
    mode: str = "interactive",
) -> ReconcileReport:
    """Reconcile DB permissions with a JSON mirror file.

    Parameters
    ----------
    conn:
        SQLite connection to the observability DB (with schema applied).
    target_path:
        Path to the settings.json or settings.local.json file to reconcile.
        Must be registered in global_mirror.settings_json_path or
        projects.settings_json_path.
    mode:
        'interactive'    — Prompt user for bulk or per-entry resolution.
                           Auto-switches to 'adopt' when hash is NULL.
        'plan'           — Return diff only; no DB mutations, no sync.
        'auto-db-wins'   — DB_WINS non-interactively.
        'auto-json-wins' — JSON_WINS non-interactively.
        'adopt'          — JSON_WINS; semantically labeled for first-touch.

    Returns
    -------
    ReconcileReport

    Raises
    ------
    ReconcileError
        On invalid mode, unknown target_path scope, or IngesterError.
    """
    if mode not in _VALID_MODES:
        raise ReconcileError(
            f"Invalid mode {mode!r}; must be one of {sorted(_VALID_MODES)}"
        )

    target_path = Path(target_path)

    # --- Detect scope ---
    project_id, _ = _detect_scope(conn, target_path)

    # --- Read JSON rows (absent file → empty list) ---
    # Capture mtime *before* any write so it stays consistent with the content
    # we're about to read.  raw_data is kept for the additional_dirs cache stamp.
    json_rows: list[dict[str, Any]] = []
    _raw_data: dict[str, Any] = {}
    _file_mtime: float | None = None
    if target_path.exists():
        _file_mtime = target_path.stat().st_mtime
        try:
            _raw_bytes = target_path.read_bytes()
            _raw_data = json.loads(_raw_bytes)
            json_rows = parse_permissions_json(target_path)
        except IngesterError as exc:
            raise ReconcileError(f"Cannot parse {target_path}: {exc}") from exc
        except (ValueError, TypeError):
            # Malformed JSON — treat as empty permissions, don't stamp cache.
            _raw_data = {}

    # --- Read DB rows ---
    db_rows = _read_db_rows(conn, project_id)

    # --- Compute diff ---
    d = diff(db_rows, json_rows)

    # --- Stamp additional_dirs cache ---
    # Done unconditionally at this point so that plan mode also populates the
    # cache (it reads the file but writes no permission rows).  mtime was
    # captured before any write so it is consistent with the parsed content.
    if _file_mtime is not None and _raw_data:
        _stamp_additional_dirs_cache(
            conn, project_id, target_path, _raw_data, _file_mtime
        )

    # --- First-touch detection ---
    first_touch = _is_first_touch(conn, project_id)
    effective_mode = mode
    if first_touch and mode == "interactive":
        effective_mode = "adopt"

    # --- Plan mode: no side effects ---
    if effective_mode == "plan":
        return ReconcileReport(
            mode=effective_mode,
            diff=d,
            applied=False,
            first_touch=first_touch,
        )

    # --- No-op: diff is empty ---
    if d.is_empty:
        # Ensure mirror file exists + hash is stamped even when diff is empty.
        _sync(conn, project_id)
        return ReconcileReport(
            mode=effective_mode,
            diff=d,
            applied=False,
            first_touch=first_touch,
        )

    # --- Resolve and apply ---
    report = ReconcileReport(
        mode=effective_mode,
        diff=d,
        applied=True,
        first_touch=first_touch,
    )
    _apply_resolution(conn, effective_mode, d, project_id, report)

    # Regenerate JSON mirror + stamp hash.
    _sync(conn, project_id)

    return report


def _apply_resolution(
    conn: sqlite3.Connection,
    effective_mode: str,
    d: Diff,
    project_id: int | None,
    report: ReconcileReport,
) -> None:
    """Apply the resolution strategy to the diff, mutating report in place."""
    if effective_mode in ("adopt", "auto-json-wins"):
        ins, dels, upds = _apply_json_wins(conn, d, project_id)
        report.db_inserts = ins
        report.db_deletes = dels
        report.db_updates = upds
        return

    if effective_mode == "auto-db-wins":
        # DB rows are authoritative: no DB mutations needed.
        # Regenerate JSON from DB (drops only_in_json, keeps only_in_db).
        return

    if effective_mode == "interactive":
        bulk = _prompt_bulk(d)
        if bulk == Resolution.JSON_WINS:
            ins, dels, upds = _apply_json_wins(conn, d, project_id)
            report.db_inserts = ins
            report.db_deletes = dels
            report.db_updates = upds
        elif bulk == Resolution.PER_ENTRY:
            to_insert, to_delete, conflict_updates = _prompt_per_entry(d)
            ins, dels, upds = _apply_per_entry_actions(
                conn, to_insert, to_delete, conflict_updates, project_id
            )
            report.db_inserts = ins
            report.db_deletes = dels
            report.db_updates = upds
        # DB_WINS: no DB mutations; sync regenerates JSON
