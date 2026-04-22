"""Pruning utilities for permission candidates.

Removes stale candidates that are not currently awaiting decision.

Run via ``python -m lib.prune [--stale-days N] [--db PATH]`` — the entry point
prints the delete counts for eyeballing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3

from lib.db import _open


def prune_candidates(
    conn: sqlite3.Connection, *, stale_days: int = 30
) -> dict[str, int]:
    """Remove stale permission_candidates rows not awaiting decision.

    A candidate is pruned if:
    1. Its last_seen is older than stale_days (default 30 days ago)
    2. No matching row exists in permission_ask_pending with the same
       (verb, subcommand, flags)

    Cascade-deletes permission_candidate_sessions via FK.

    Args:
        conn: Database connection.
        stale_days: Number of days before a candidate is considered stale.

    Returns:
        A dict with keys 'candidates_deleted' and 'candidate_sessions_deleted'
        and their counts.
    """
    cutoff = (
        (dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=stale_days))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )

    # Find candidates that are stale (last_seen < cutoff) and have no
    # pending ask with the same (verb, subcommand, flags).
    cursor = conn.execute(
        """
        SELECT pc.id, pc.verb, pc.subcommand, pc.flags
        FROM permission_candidates pc
        WHERE pc.last_seen < ?
          AND NOT EXISTS (
            SELECT 1 FROM permission_ask_pending pap
            WHERE pap.verb = pc.verb
              AND IFNULL(pap.subcommand, '') = IFNULL(pc.subcommand, '')
              AND pap.flags = pc.flags
          )
        """,
        (cutoff,),
    )

    stale_candidates = cursor.fetchall()
    candidate_ids_to_delete = [row[0] for row in stale_candidates]

    # Before deletion, count the sessions that will be cascade-deleted.
    sessions_count = 0
    if candidate_ids_to_delete:
        placeholders = ",".join("?" * len(candidate_ids_to_delete))
        sessions_count = conn.execute(
            f"SELECT COUNT(*) FROM permission_candidate_sessions WHERE candidate_id IN ({placeholders})",
            candidate_ids_to_delete,
        ).fetchone()[0]

    # Delete the candidates (cascade deletes sessions via FK).
    if candidate_ids_to_delete:
        placeholders = ",".join("?" * len(candidate_ids_to_delete))
        conn.execute(
            f"DELETE FROM permission_candidates WHERE id IN ({placeholders})",
            candidate_ids_to_delete,
        )
        conn.commit()

    return {
        "candidates_deleted": len(candidate_ids_to_delete),
        "candidate_sessions_deleted": sessions_count,
    }


def main() -> int:
    """CLI entry point for pruning."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--stale-days",
        type=int,
        default=30,
        help="Drop candidates with last_seen older than this many days (default 30).",
    )
    ap.add_argument(
        "--db",
        type=str,
        help="Path to observations.db (default: OBSERVABILITY_DB env or ~/.cache/claude/observability/observations.db).",
    )
    args = ap.parse_args()

    if args.db:
        # Override the default DB path
        import os

        os.environ["OBSERVABILITY_DB"] = args.db

    conn = _open()
    try:
        counts = prune_candidates(conn, stale_days=args.stale_days)
    finally:
        conn.close()

    for key, n in counts.items():
        print(f"{key}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
