"""nephoscope-migrate — apply schema deltas and normalize flags in the observations DB.

Runs in-place inside a single transaction:
  1. Schema updates  — adds any missing columns/indexes (idempotent).
  2. Flags normalization — POSIX cluster expansion + sort applied to all
     flag columns; colliding rows (e.g. ``["-rf"]`` vs ``["-r","-f"]``)
     are merged.

Migration scope
---------------
- ``rule_shapes.flags``           — normalize; merge collisions (MIN/MAX dates)
- ``permission_candidates.flags`` — normalize; merge collisions (sum observations)
- ``permission_ask_pending.flags`` — normalize in-place
- ``permissions.rule_shape_id``   — remapped when a rule_shape collision merged
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from pathlib import Path

from nephoscope.learners.permission.canonicalize import normalize_flags
from nephoscope.lib.paths import observations_db_path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _norm_flags(flags_json: str) -> str:
    if flags_json == "*":
        return "*"
    return json.dumps(normalize_flags(json.loads(flags_json)), separators=(",", ":"))


# ---------------------------------------------------------------------------
# Schema delta (idempotent)
# ---------------------------------------------------------------------------


def _apply_schema_delta(conn: sqlite3.Connection) -> list[str]:
    applied: list[str] = []

    rs_cols = {r[1] for r in conn.execute("PRAGMA table_info(rule_shapes)")}
    if "tool" not in rs_cols:
        conn.execute(
            "ALTER TABLE rule_shapes ADD COLUMN tool TEXT NOT NULL DEFAULT 'Bash'"
            " CHECK (tool IN ('Bash', 'Read', 'Write', 'Edit'))"
        )
        conn.execute("DROP INDEX IF EXISTS idx_rule_shapes_unique")
        conn.execute(
            "CREATE UNIQUE INDEX idx_rule_shapes_unique"
            " ON rule_shapes(verb, IFNULL(subcommand, ''), flags,"
            "                IFNULL(path_spec, ''), context, tool)"
        )
        applied.append("rule_shapes.tool + index")

    perm_cols = {r[1] for r in conn.execute("PRAGMA table_info(permissions)")}
    if "danger_accepted" not in perm_cols:
        conn.execute("ALTER TABLE permissions ADD COLUMN danger_accepted TEXT")
        applied.append("permissions.danger_accepted")

    return applied


# ---------------------------------------------------------------------------
# In-place flag normalization
# ---------------------------------------------------------------------------


def _normalize_rule_shapes(conn: sqlite3.Connection) -> tuple[int, int]:
    """Normalize flags in rule_shapes in-place; merge collisions.

    Returns (rows_updated, rows_merged).
    """
    rows = conn.execute(
        "SELECT id, verb, subcommand, flags, path_spec, context, tool,"
        "       first_seen, last_seen"
        " FROM rule_shapes ORDER BY id"
    ).fetchall()

    # Group by normalized key; lowest id wins.
    groups: dict[tuple, list[tuple]] = {}
    for (
        row_id,
        verb,
        subcommand,
        flags,
        path_spec,
        context,
        tool,
        first_seen,
        last_seen,
    ) in rows:
        norm = _norm_flags(flags)
        key = (verb, subcommand, norm, path_spec, context, tool)
        groups.setdefault(key, []).append((row_id, first_seen, last_seen))

    id_map: dict[int, int] = {}
    merges: list[tuple] = []

    for key, group in groups.items():
        norm_flags = key[2]
        group.sort()
        winner_id = group[0][0]
        merged_first = min(r[1] for r in group)
        merged_last = max(r[2] for r in group)
        merges.append((winner_id, norm_flags, merged_first, merged_last))
        for row_id, _, _ in group:
            id_map[row_id] = winner_id

    losers = [old for old, new in id_map.items() if old != new]

    # Remap permissions before deleting losers (FK safety).
    for old_id, new_id in id_map.items():
        if old_id != new_id:
            conn.execute(
                "UPDATE permissions SET rule_shape_id=? WHERE rule_shape_id=?",
                (new_id, old_id),
            )
            conn.execute("DELETE FROM rule_shapes WHERE id=?", (old_id,))

    # Update all winners to normalized flags + merged dates.
    for winner_id, norm_flags, merged_first, merged_last in merges:
        conn.execute(
            "UPDATE rule_shapes SET flags=?, first_seen=?, last_seen=? WHERE id=?",
            (norm_flags, merged_first, merged_last, winner_id),
        )

    return len(rows), len(losers)


def _normalize_candidates(conn: sqlite3.Connection) -> tuple[int, int]:
    """Normalize flags in permission_candidates in-place; merge collisions.

    Returns (rows_updated, rows_merged).
    """
    rows = conn.execute(
        "SELECT id, verb, subcommand, flags, observations, distinct_sessions,"
        "       first_seen, last_seen, positional_paths"
        " FROM permission_candidates ORDER BY id"
    ).fetchall()

    groups: dict[tuple, list[tuple]] = {}
    for row_id, verb, subcommand, flags, obs, dsess, first_seen, last_seen, pos in rows:
        norm = _norm_flags(flags)
        key = (verb, subcommand, norm)
        groups.setdefault(key, []).append(
            (row_id, obs, dsess, first_seen, last_seen, pos)
        )

    merged_count = 0
    for key, group in groups.items():
        norm_flags = key[2]
        group.sort()
        winner_id = group[0][0]
        total_obs = sum(r[1] for r in group)
        max_dsess = max(r[2] for r in group)
        merged_first = min(r[3] for r in group)
        merged_last = max(r[4] for r in group)
        pos = next((r[5] for r in group if r[5] is not None), None)

        for row_id, _, _, _, _, _ in group[1:]:
            conn.execute(
                "DELETE FROM permission_candidate_sessions WHERE candidate_id=?",
                (row_id,),
            )
            conn.execute("DELETE FROM permission_candidates WHERE id=?", (row_id,))
            merged_count += 1

        conn.execute(
            "UPDATE permission_candidates"
            " SET flags=?, observations=?, distinct_sessions=?,"
            "     first_seen=?, last_seen=?, positional_paths=?"
            " WHERE id=?",
            (
                norm_flags,
                total_obs,
                max_dsess,
                merged_first,
                merged_last,
                pos,
                winner_id,
            ),
        )

    return len(rows), merged_count


def _normalize_ask_pending(conn: sqlite3.Connection) -> int:
    original_factory = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM permission_ask_pending").fetchall()
        updated = 0
        for row in rows:
            norm = _norm_flags(row["flags"])
            if norm != row["flags"]:
                conn.execute(
                    "UPDATE permission_ask_pending SET flags=? WHERE tool_use_id=?",
                    (norm, row["tool_use_id"]),
                )
                updated += 1
    finally:
        conn.row_factory = original_factory
    return updated


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _migrate(db_path: Path) -> int:
    print(f"DB: {db_path}")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN EXCLUSIVE")

        applied = _apply_schema_delta(conn)
        if applied:
            for item in applied:
                print(f"  schema: added {item}")
        else:
            print("  schema: already current")

        rs_total, rs_merged = _normalize_rule_shapes(conn)
        print(f"  rule_shapes: {rs_total} rows, {rs_merged} merged")

        cand_total, cand_merged = _normalize_candidates(conn)
        print(f"  permission_candidates: {cand_total} rows, {cand_merged} merged")

        ask_updated = _normalize_ask_pending(conn)
        print(f"  permission_ask_pending: {ask_updated} updated")

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.close()
        raise
    conn.close()
    print("Done.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nephoscope-migrate",
        description="Apply schema updates and normalize flags in the observations DB (in-place).",
    )
    parser.add_argument(
        "--db", metavar="PATH", help="DB path (default: OBSERVABILITY_DB)"
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else observations_db_path()
    if not db_path.exists():
        log.error("DB not found: %s", db_path)
        return 1

    return _migrate(db_path)
