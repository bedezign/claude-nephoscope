"""GC for session-scoped permission data (Phase 8).

Drops rows whose usefulness has expired:

- ``permissions`` rows where session_id is set and the session hasn't been
  touched in ``session_idle_days`` days (default 7) — the user can
  re-confirm if they come back.
- ``permission_ask_pending``: orphans from ask'd calls where the user
  chose "Deny" (PostToolUse never fires), or from calls where the
  recorder's Post phase crashed before promotion. Older than
  ``ask_pending_hours`` (default 1) is considered lost.

Both sweeps are idempotent. Run via ``python -m lib.gc_sessions`` — the
entry-point prints the delete counts for eyeballing.
"""

from __future__ import annotations

import argparse
import datetime as _dt
from typing import Any

from nephoscope.lib.db import _open


def _cutoff_iso(delta: _dt.timedelta) -> str:
    """Return an ISO-8601 timestamp this much in the past (UTC, millis, `Z`)."""
    ts = _dt.datetime.now(tz=_dt.timezone.utc) - delta
    return ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sweep(
    conn: Any,
    session_idle_days: int = 7,
    ask_pending_hours: int = 1,
) -> dict[str, int]:
    """Run both sweeps. Returns a dict of table-name → rows deleted."""
    session_cutoff = _cutoff_iso(_dt.timedelta(days=session_idle_days))
    pending_cutoff = _cutoff_iso(_dt.timedelta(hours=ask_pending_hours))

    cur = conn.execute(
        """
        DELETE FROM permissions
         WHERE session_id IS NOT NULL
           AND session_id IN (
             SELECT id FROM sessions WHERE last_activity < ?
           );
        """,
        (session_cutoff,),
    )
    approvals_dropped = cur.rowcount

    cur = conn.execute(
        "DELETE FROM permission_ask_pending WHERE asked_at < ?;",
        (pending_cutoff,),
    )
    pending_dropped = cur.rowcount

    conn.commit()
    return {
        "permissions (session-tier)": max(approvals_dropped, 0),
        "permission_ask_pending": max(pending_dropped, 0),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--session-idle-days",
        type=int,
        default=7,
        help="Drop session-tier permissions for sessions idle this many days (default 7).",
    )
    ap.add_argument(
        "--ask-pending-hours",
        type=int,
        default=1,
        help="Drop ask_pending rows older than this many hours (default 1).",
    )
    args = ap.parse_args()

    conn = _open()
    try:
        counts = sweep(
            conn,
            session_idle_days=args.session_idle_days,
            ask_pending_hours=args.ask_pending_hours,
        )
    finally:
        conn.close()

    for table, n in counts.items():
        print(f"{table}: {n} rows deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
