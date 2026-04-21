#!/usr/bin/env python
r"""
Collapse command_shapes rows with numeric flags into a single sentinel shape.

Any flags containing regex ^-\d+$ (negative integers like -1, -42) are
collapsed into a single "numeric sentinel" shape whose flags are normalized:
all matching numeric tokens become -<N> (a literal string), then the set is
deduplicated and sorted. Existing shapes are re-pointed to the new sentinel,
and old rows are deleted. All permission tables follow via FK cascade or
explicit INSERT OR IGNORE to avoid duplicates on the collapsed target.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def parse_flags(flags_json: str) -> list[str]:
    """Parse flags JSON string into a list of strings."""
    return json.loads(flags_json)


def serialize_flags(flags: list[str]) -> str:
    """Serialize flags list back to minified JSON."""
    return json.dumps(flags, separators=(",", ":"), sort_keys=False)


def normalize_flags(flags: list[str]) -> list[str]:
    """Replace all ^-\\d+$ tokens with -<N>, then deduplicate and sort."""
    normalized = []
    for flag in flags:
        if re.match(r"^-\d+$", flag):
            normalized.append("-<N>")
        else:
            normalized.append(flag)
    return sorted(set(normalized))


def has_numeric_flag(flags_json: str) -> bool:
    """Check if flags contain any ^-\\d+$ token."""
    try:
        flags = parse_flags(flags_json)
        return any(re.match(r"^-\d+$", f) for f in flags)
    except (json.JSONDecodeError, TypeError):
        return False


def run_migration(
    db_path: str,
    dry_run: bool = False,
) -> None:
    """Execute the migration."""
    path = Path(db_path)
    if not path.exists():
        print(f"Error: database not found at {path}", file=sys.stderr)
        sys.exit(1)

    # isolation_level=None → autocommit; we manage the transaction explicitly
    # via BEGIN/COMMIT/ROLLBACK below. Avoids Python's implicit transaction
    # layer interacting unpredictably with the explicit BEGIN.
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON;")

    try:
        # Collect all shapes with numeric flags.
        rows = conn.execute(
            "SELECT id, verb, subcommand, flags FROM command_shapes ORDER BY id"
        ).fetchall()

        matched_shapes: list[tuple[int, str, str | None, str]] = []
        for row_id, verb, subcommand, flags_json in rows:
            if has_numeric_flag(flags_json):
                matched_shapes.append((row_id, verb, subcommand, flags_json))

        if not matched_shapes:
            print("No shapes with numeric flags found. Nothing to migrate.")
            return

        print(f"Found {len(matched_shapes)} shape(s) with numeric flags.")

        # Dry-run: print planned operations.
        if dry_run:
            print("\n=== DRY RUN: Planned Operations ===\n")

        stats = {
            "matched": len(matched_shapes),
            "existing_targets": 0,
            "new_targets": 0,
            "junction_repointed": 0,
            "permission_active_migrated": 0,
            "permission_rejected_migrated": 0,
            "permission_candidates_migrated": 0,
            "permission_candidate_sessions_migrated": 0,
            "permission_ask_pending_migrated": 0,
            "permission_session_approvals_migrated": 0,
            "old_shapes_deleted": 0,
        }

        if not dry_run:
            conn.execute("BEGIN;")

        try:
            for old_id, verb, subcommand, old_flags_json in matched_shapes:
                old_flags = parse_flags(old_flags_json)
                normalized = normalize_flags(old_flags)
                new_flags_json = serialize_flags(normalized)

                if new_flags_json == old_flags_json:
                    if dry_run:
                        print(
                            f"  SKIP shape {old_id} ({verb}): "
                            f"normalized flags identical to original"
                        )
                    continue

                # Look up existing target shape.
                target_row = conn.execute(
                    "SELECT id FROM command_shapes "
                    "WHERE verb = ? AND IFNULL(subcommand, '') = ? AND flags = ?",
                    (verb, subcommand or "", new_flags_json),
                ).fetchone()

                if target_row:
                    collapsed_id = target_row[0]
                    stats["existing_targets"] += 1
                    if dry_run:
                        print(
                            f"  MERGE shape {old_id} into existing {collapsed_id} "
                            f"({verb}/{subcommand or '-'}) "
                            f"old_flags={old_flags_json[:50]}... "
                            f"→ {new_flags_json[:50]}..."
                        )
                else:
                    if dry_run:
                        # For dry-run, we don't actually insert; just predict the collapsed_id.
                        # Use negative numbers or a placeholder since we're not committing.
                        collapsed_id = -(old_id * 1000 + 1)
                        print(
                            f"  INSERT new shape (simulated id {collapsed_id}) ({verb}/{subcommand or '-'}) "
                            f"old={old_flags_json[:50]}... "
                            f"→ {new_flags_json[:50]}..."
                        )
                    else:
                        # Insert new target.
                        now_iso = (
                            datetime.now(timezone.utc)
                            .isoformat()
                            .replace("+00:00", "Z")
                        )
                        cursor = conn.execute(
                            "INSERT INTO command_shapes (verb, subcommand, flags, first_seen, last_seen) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (
                                verb,
                                subcommand,
                                new_flags_json,
                                now_iso,
                                now_iso,
                            ),
                        )
                        collapsed_id = cursor.lastrowid
                    stats["new_targets"] += 1

                if not dry_run:
                    # Repoint junction rows.
                    jct_count = conn.execute(
                        "UPDATE tool_call_shapes SET command_shape_id = ? "
                        "WHERE command_shape_id = ?",
                        (collapsed_id, old_id),
                    ).rowcount
                    stats["junction_repointed"] += jct_count

                    # Migrate permission_active.
                    active_rows = conn.execute(
                        "SELECT command_shape_id, scope_id, promoted_at, source "
                        "FROM permission_active WHERE command_shape_id = ?",
                        (old_id,),
                    ).fetchall()
                    for _, scope_id, promoted_at, source in active_rows:
                        conn.execute(
                            "INSERT OR IGNORE INTO permission_active "
                            "(command_shape_id, scope_id, promoted_at, source) "
                            "VALUES (?, ?, ?, ?)",
                            (collapsed_id, scope_id, promoted_at, source),
                        )
                    stats["permission_active_migrated"] += len(active_rows)

                    # Migrate permission_rejected.
                    rejected_rows = conn.execute(
                        "SELECT command_shape_id, scope_id, rejected_at, reason "
                        "FROM permission_rejected WHERE command_shape_id = ?",
                        (old_id,),
                    ).fetchall()
                    for _, scope_id, rejected_at, reason in rejected_rows:
                        conn.execute(
                            "INSERT OR IGNORE INTO permission_rejected "
                            "(command_shape_id, scope_id, rejected_at, reason) "
                            "VALUES (?, ?, ?, ?)",
                            (collapsed_id, scope_id, rejected_at, reason),
                        )
                    stats["permission_rejected_migrated"] += len(rejected_rows)

                    # Migrate permission_candidates.
                    cand_rows = conn.execute(
                        "SELECT command_shape_id, observations, distinct_sessions, first_seen, last_seen "
                        "FROM permission_candidates WHERE command_shape_id = ?",
                        (old_id,),
                    ).fetchall()
                    for _, obs, sessions, first_seen, last_seen in cand_rows:
                        conn.execute(
                            "INSERT OR IGNORE INTO permission_candidates "
                            "(command_shape_id, observations, distinct_sessions, first_seen, last_seen) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (collapsed_id, obs, sessions, first_seen, last_seen),
                        )
                    stats["permission_candidates_migrated"] += len(cand_rows)

                    # Migrate permission_candidate_sessions.
                    sess_rows = conn.execute(
                        "SELECT command_shape_id, session_id, last_seen "
                        "FROM permission_candidate_sessions WHERE command_shape_id = ?",
                        (old_id,),
                    ).fetchall()
                    for _, session_id, last_seen in sess_rows:
                        conn.execute(
                            "INSERT OR IGNORE INTO permission_candidate_sessions "
                            "(command_shape_id, session_id, last_seen) "
                            "VALUES (?, ?, ?)",
                            (collapsed_id, session_id, last_seen),
                        )
                    stats["permission_candidate_sessions_migrated"] += len(sess_rows)

                    # Migrate permission_ask_pending.
                    ask_rows = conn.execute(
                        "SELECT tool_use_id, leaf_index, session_id, scope_id, asked_at "
                        "FROM permission_ask_pending WHERE command_shape_id = ?",
                        (old_id,),
                    ).fetchall()
                    for (
                        tool_use_id,
                        leaf_index,
                        session_id,
                        scope_id,
                        asked_at,
                    ) in ask_rows:
                        conn.execute(
                            "INSERT OR IGNORE INTO permission_ask_pending "
                            "(tool_use_id, leaf_index, session_id, command_shape_id, scope_id, asked_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                tool_use_id,
                                leaf_index,
                                session_id,
                                collapsed_id,
                                scope_id,
                                asked_at,
                            ),
                        )
                    stats["permission_ask_pending_migrated"] += len(ask_rows)

                    # Migrate permission_session_approvals.
                    psa_rows = conn.execute(
                        "SELECT session_id, scope_id, approved_at "
                        "FROM permission_session_approvals WHERE command_shape_id = ?",
                        (old_id,),
                    ).fetchall()
                    for session_id, scope_id, approved_at in psa_rows:
                        conn.execute(
                            "INSERT OR IGNORE INTO permission_session_approvals "
                            "(session_id, command_shape_id, scope_id, approved_at) "
                            "VALUES (?, ?, ?, ?)",
                            (session_id, collapsed_id, scope_id, approved_at),
                        )
                    stats["permission_session_approvals_migrated"] += len(psa_rows)

                    # Drop old-id rows from every permission table before deleting
                    # the shape. FKs on these tables do not ON DELETE CASCADE, so
                    # dangling references would block the final DELETE.
                    for table in (
                        "permission_active",
                        "permission_rejected",
                        "permission_candidates",
                        "permission_candidate_sessions",
                        "permission_ask_pending",
                        "permission_session_approvals",
                    ):
                        conn.execute(
                            f"DELETE FROM {table} WHERE command_shape_id = ?",
                            (old_id,),
                        )

                    # Delete old shape.
                    conn.execute(
                        "DELETE FROM command_shapes WHERE id = ?",
                        (old_id,),
                    )
                    stats["old_shapes_deleted"] += 1
                else:
                    # Dry-run: count what would be migrated by querying.
                    jct_count = conn.execute(
                        "SELECT COUNT(*) FROM tool_call_shapes WHERE command_shape_id = ?",
                        (old_id,),
                    ).fetchone()[0]
                    stats["junction_repointed"] += jct_count

                    active_rows = conn.execute(
                        "SELECT COUNT(*) FROM permission_active WHERE command_shape_id = ?",
                        (old_id,),
                    ).fetchone()[0]
                    stats["permission_active_migrated"] += active_rows

                    rejected_rows = conn.execute(
                        "SELECT COUNT(*) FROM permission_rejected WHERE command_shape_id = ?",
                        (old_id,),
                    ).fetchone()[0]
                    stats["permission_rejected_migrated"] += rejected_rows

                    cand_rows = conn.execute(
                        "SELECT COUNT(*) FROM permission_candidates WHERE command_shape_id = ?",
                        (old_id,),
                    ).fetchone()[0]
                    stats["permission_candidates_migrated"] += cand_rows

                    sess_rows = conn.execute(
                        "SELECT COUNT(*) FROM permission_candidate_sessions WHERE command_shape_id = ?",
                        (old_id,),
                    ).fetchone()[0]
                    stats["permission_candidate_sessions_migrated"] += sess_rows

                    ask_rows = conn.execute(
                        "SELECT COUNT(*) FROM permission_ask_pending WHERE command_shape_id = ?",
                        (old_id,),
                    ).fetchone()[0]
                    stats["permission_ask_pending_migrated"] += ask_rows

                    psa_rows = conn.execute(
                        "SELECT COUNT(*) FROM permission_session_approvals WHERE command_shape_id = ?",
                        (old_id,),
                    ).fetchone()[0]
                    stats["permission_session_approvals_migrated"] += psa_rows

                    print(f"    Repoint {jct_count} junction rows")
                    print(
                        f"    Migrate {active_rows} active, "
                        f"{rejected_rows} rejected, "
                        f"{cand_rows} candidates, "
                        f"{sess_rows} candidate_sessions, "
                        f"{ask_rows} ask_pending, "
                        f"{psa_rows} session_approvals"
                    )
                    print(f"    DELETE old shape {old_id}\n")
                    stats["old_shapes_deleted"] += 1

            if not dry_run:
                conn.commit()
                print("Migration committed.\n")

        except Exception as e:
            if not dry_run:
                conn.rollback()
            raise e

        print("=== Migration Summary ===")
        print(f"Shapes matched:                         {stats['matched']}")
        print(f"Existing targets (merged into):        {stats['existing_targets']}")
        print(f"New targets created:                   {stats['new_targets']}")
        print(f"Junction rows re-pointed:              {stats['junction_repointed']}")
        print(
            f"permission_active rows migrated:       {stats['permission_active_migrated']}"
        )
        print(
            f"permission_rejected rows migrated:     {stats['permission_rejected_migrated']}"
        )
        print(
            f"permission_candidates rows migrated:   {stats['permission_candidates_migrated']}"
        )
        print(
            f"permission_candidate_sessions rows:    {stats['permission_candidate_sessions_migrated']}"
        )
        print(
            f"permission_ask_pending rows migrated:  {stats['permission_ask_pending_migrated']}"
        )
        print(
            f"permission_session_approvals migrated: {stats['permission_session_approvals_migrated']}"
        )
        print(f"Old shapes deleted:                    {stats['old_shapes_deleted']}")

    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collapse command_shapes rows with numeric flags into a sentinel shape."
    )
    parser.add_argument(
        "--db",
        default="/home/steve/.cache/claude/observability/observations.db",
        help="Path to the observability database",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned operations without writing",
    )
    args = parser.parse_args()

    run_migration(args.db, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
