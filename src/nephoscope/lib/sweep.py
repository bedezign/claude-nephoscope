"""Sweep stale ``pending`` ``tool_calls`` rows to ``orphan``.

A row stays ``pending`` between the recorder's pre-hook INSERT and the
matching post-hook UPDATE. In practice a row gets stuck in ``pending``
when the post never fires: crashed session, hook error, or (until today)
a denied Bash call that short-circuited the tool invocation.

With the permission hook now writing ``denied`` directly, the remaining
``pending`` residue is genuine orphans. This sweeper relabels any row
older than ``threshold_hours`` and still ``pending`` to ``orphan`` and
stamps ``completed_ts`` so "how long pending" queries see a terminal
state.

Threshold default of 1 hour is conservative — a live Agent subtask can
easily run for minutes, so we don't want to sweep while it's still in
flight. Override via ``--hours`` on the CLI if the workload needs it.

Safe to re-run. Idempotent: a row that's already been swept has a
terminal ``status_id`` and the WHERE clause filters it out.
"""

from __future__ import annotations

import argparse
import datetime as _dt

from nephoscope.lib.db import _now, _open


def sweep_orphans(conn, threshold_hours: float = 1.0) -> int:
    """Relabel old ``pending`` rows to ``orphan``. Return number relabeled."""
    cutoff = (
        (_dt.datetime.now(tz=_dt.timezone.utc) - _dt.timedelta(hours=threshold_hours))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    now = _now()
    cur = conn.execute(
        """
        UPDATE tool_calls
           SET status_id = (SELECT id FROM call_statuses WHERE name='orphan'),
               completed_ts = ?
         WHERE status_id = (SELECT id FROM call_statuses WHERE name='pending')
           AND ts < ?;
        """,
        (now, cutoff),
    )
    conn.commit()
    return cur.rowcount or 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Relabel stale pending tool_calls rows to orphan."
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=1.0,
        help="Age threshold in hours (default: 1.0).",
    )
    args = parser.parse_args(argv)

    conn = _open()
    try:
        n = sweep_orphans(conn, threshold_hours=args.hours)
    finally:
        conn.close()
    print(f"relabeled {n} stale pending row(s) to orphan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
