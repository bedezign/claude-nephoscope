"""Tests for ``lib.sweep.sweep_orphans``.

The sweeper's job is narrow but easy to get subtly wrong: relabel only
``pending`` rows older than the threshold, leave younger pending rows
alone, and never touch rows in other terminal states. Each test here
pins one of those axes.
"""
from __future__ import annotations

import datetime as _dt

from lib.sweep import sweep_orphans


def _ts(hours_ago: float) -> str:
    return (
        (
            _dt.datetime.now(tz=_dt.timezone.utc)
            - _dt.timedelta(hours=hours_ago)
        )
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _status_id(conn, name: str) -> int:
    return int(
        conn.execute(
            "SELECT id FROM call_statuses WHERE name=?;", (name,)
        ).fetchone()[0]
    )


def _insert_pending(conn, ts: str, tool_use_id: str) -> int:
    pending_id = _status_id(conn, "pending")
    cur = conn.execute(
        """
        INSERT INTO tool_calls (ts, tool_use_id, status_id)
        VALUES (?, ?, ?) RETURNING id;
        """,
        (ts, tool_use_id, pending_id),
    )
    return int(cur.fetchone()[0])


def _insert_terminal(conn, ts: str, tool_use_id: str, status: str) -> int:
    status_id = _status_id(conn, status)
    cur = conn.execute(
        """
        INSERT INTO tool_calls (ts, tool_use_id, status_id, completed_ts)
        VALUES (?, ?, ?, ?) RETURNING id;
        """,
        (ts, tool_use_id, status_id, ts),
    )
    return int(cur.fetchone()[0])


def _row_status(conn, tool_call_id: int) -> tuple[str, str | None]:
    row = conn.execute(
        """
        SELECT cs.name, tc.completed_ts
          FROM tool_calls tc
          JOIN call_statuses cs ON cs.id = tc.status_id
         WHERE tc.id = ?;
        """,
        (tool_call_id,),
    ).fetchone()
    return row[0], row[1]


def test_sweeps_old_pending_only(tmp_db):
    old = _insert_pending(tmp_db, _ts(2.0), "old-pending")
    young = _insert_pending(tmp_db, _ts(0.1), "young-pending")

    n = sweep_orphans(tmp_db, threshold_hours=1.0)
    assert n == 1

    assert _row_status(tmp_db, old)[0] == "orphan"
    assert _row_status(tmp_db, old)[1] is not None
    assert _row_status(tmp_db, young)[0] == "pending"
    assert _row_status(tmp_db, young)[1] is None


def test_does_not_touch_terminal_rows(tmp_db):
    ok = _insert_terminal(tmp_db, _ts(5.0), "ok-row", "ok")
    err = _insert_terminal(tmp_db, _ts(5.0), "err-row", "err")
    denied = _insert_terminal(tmp_db, _ts(5.0), "denied-row", "denied")

    sweep_orphans(tmp_db, threshold_hours=1.0)

    assert _row_status(tmp_db, ok)[0] == "ok"
    assert _row_status(tmp_db, err)[0] == "err"
    assert _row_status(tmp_db, denied)[0] == "denied"


def test_idempotent(tmp_db):
    _insert_pending(tmp_db, _ts(3.0), "x")
    first = sweep_orphans(tmp_db, threshold_hours=1.0)
    second = sweep_orphans(tmp_db, threshold_hours=1.0)
    assert first == 1
    assert second == 0


def test_threshold_respected(tmp_db):
    row = _insert_pending(tmp_db, _ts(0.5), "half-hour-old")
    n = sweep_orphans(tmp_db, threshold_hours=1.0)
    assert n == 0
    assert _row_status(tmp_db, row)[0] == "pending"

    n2 = sweep_orphans(tmp_db, threshold_hours=0.1)
    assert n2 == 1
    assert _row_status(tmp_db, row)[0] == "orphan"
