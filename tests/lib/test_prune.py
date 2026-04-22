"""Tests for lib/prune.py."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from observability.lib.prune import prune_candidates


@pytest.fixture
def db_conn(tmp_path: Path) -> sqlite3.Connection:
    """Create an in-memory DB with the required schema."""
    db_file = tmp_path / "test.db"
    conn = sqlite3.connect(db_file, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON;")

    # Minimal schema for prune tests.
    conn.execute(
        """
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_uuid TEXT UNIQUE NOT NULL,
            project_id INTEGER,
            started_at TEXT NOT NULL,
            last_activity TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE permission_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            verb TEXT NOT NULL,
            subcommand TEXT,
            flags TEXT NOT NULL,
            observations INTEGER NOT NULL DEFAULT 0,
            distinct_sessions INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE UNIQUE INDEX idx_permission_candidates_unique
        ON permission_candidates(verb, IFNULL(subcommand, ''), flags)
        """
    )

    conn.execute(
        """
        CREATE TABLE permission_candidate_sessions (
            candidate_id INTEGER NOT NULL REFERENCES permission_candidates(id) ON DELETE CASCADE,
            session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            last_seen TEXT NOT NULL,
            PRIMARY KEY (candidate_id, session_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE permission_ask_pending (
            tool_use_id TEXT PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            verb TEXT NOT NULL,
            subcommand TEXT,
            flags TEXT NOT NULL,
            asked_at TEXT NOT NULL
        )
        """
    )

    conn.commit()
    return conn


def _iso_ts(days_ago: int = 0) -> str:
    """Generate an ISO timestamp from days ago."""
    ts = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=days_ago)
    return ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")


_session_counter = 0


def _insert_session(conn: sqlite3.Connection) -> int:
    """Insert a test session and return its id."""
    global _session_counter
    _session_counter += 1
    now = _iso_ts()
    cursor = conn.execute(
        """
        INSERT INTO sessions (session_uuid, project_id, started_at, last_activity)
        VALUES (?, ?, ?, ?)
        """,
        (f"test-{now}-{_session_counter}", None, now, now),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _insert_candidate(
    conn: sqlite3.Connection,
    verb: str,
    subcommand: str | None = None,
    flags: str = "[]",
    first_seen: str | None = None,
    last_seen: str | None = None,
) -> int:
    """Insert a candidate and return its id."""
    first_seen = first_seen or _iso_ts()
    last_seen = last_seen or _iso_ts()
    cursor = conn.execute(
        """
        INSERT INTO permission_candidates (verb, subcommand, flags, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?)
        """,
        (verb, subcommand, flags, first_seen, last_seen),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _insert_candidate_session(
    conn: sqlite3.Connection,
    candidate_id: int,
    session_id: int,
    last_seen: str | None = None,
) -> None:
    """Insert a candidate-session link."""
    last_seen = last_seen or _iso_ts()
    conn.execute(
        """
        INSERT INTO permission_candidate_sessions (candidate_id, session_id, last_seen)
        VALUES (?, ?, ?)
        """,
        (candidate_id, session_id, last_seen),
    )
    conn.commit()


def _insert_pending_ask(
    conn: sqlite3.Connection,
    tool_use_id: str,
    session_id: int,
    verb: str,
    subcommand: str | None = None,
    flags: str = "[]",
    asked_at: str | None = None,
) -> None:
    """Insert a pending ask."""
    asked_at = asked_at or _iso_ts()
    conn.execute(
        """
        INSERT INTO permission_ask_pending (tool_use_id, session_id, verb, subcommand, flags, asked_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (tool_use_id, session_id, verb, subcommand, flags, asked_at),
    )
    conn.commit()


def test_prune_no_stale_candidates(db_conn: sqlite3.Connection) -> None:
    """Pruning with no stale candidates returns zero counts."""
    _insert_candidate(db_conn, "Read", last_seen=_iso_ts())
    result = prune_candidates(db_conn, stale_days=30)
    assert result == {"candidates_deleted": 0, "candidate_sessions_deleted": 0}


def test_prune_single_stale_candidate_no_pending(db_conn: sqlite3.Connection) -> None:
    """Pruning removes a stale candidate with no pending ask."""
    _insert_candidate(db_conn, "Read", last_seen=_iso_ts(days_ago=31))
    result = prune_candidates(db_conn, stale_days=30)
    assert result == {"candidates_deleted": 1, "candidate_sessions_deleted": 0}

    count = db_conn.execute("SELECT COUNT(*) FROM permission_candidates").fetchone()[0]
    assert count == 0


def test_prune_protects_pending_ask(db_conn: sqlite3.Connection) -> None:
    """Pruning does NOT remove a stale candidate with a matching pending ask."""
    session_id = _insert_session(db_conn)
    _insert_candidate(db_conn, "Read", last_seen=_iso_ts(days_ago=31))
    _insert_pending_ask(db_conn, "tool-123", session_id, "Read", asked_at=_iso_ts())

    result = prune_candidates(db_conn, stale_days=30)
    assert result == {"candidates_deleted": 0, "candidate_sessions_deleted": 0}

    count = db_conn.execute("SELECT COUNT(*) FROM permission_candidates").fetchone()[0]
    assert count == 1


def test_prune_cascade_deletes_sessions(db_conn: sqlite3.Connection) -> None:
    """Pruning cascade-deletes candidate-session links."""
    session_id = _insert_session(db_conn)
    cand_id = _insert_candidate(db_conn, "Read", last_seen=_iso_ts(days_ago=31))
    _insert_candidate_session(db_conn, cand_id, session_id)

    result = prune_candidates(db_conn, stale_days=30)
    assert result == {"candidates_deleted": 1, "candidate_sessions_deleted": 1}

    count = db_conn.execute(
        "SELECT COUNT(*) FROM permission_candidate_sessions"
    ).fetchone()[0]
    assert count == 0


def test_prune_multiple_candidates_mixed(db_conn: sqlite3.Connection) -> None:
    """Pruning removes some candidates, keeps others (partial prune)."""
    session_id = _insert_session(db_conn)

    # Stale, no pending ask -> DELETE
    _insert_candidate(db_conn, "Read", last_seen=_iso_ts(days_ago=31))

    # Recent -> KEEP
    _insert_candidate(db_conn, "Write", last_seen=_iso_ts())

    # Stale but has pending ask -> KEEP
    _insert_candidate(db_conn, "Edit", last_seen=_iso_ts(days_ago=31))
    _insert_pending_ask(db_conn, "tool-123", session_id, "Edit", asked_at=_iso_ts())

    result = prune_candidates(db_conn, stale_days=30)
    assert result == {"candidates_deleted": 1, "candidate_sessions_deleted": 0}

    remaining = db_conn.execute(
        "SELECT verb FROM permission_candidates ORDER BY verb"
    ).fetchall()
    remaining_verbs = [r[0] for r in remaining]
    assert remaining_verbs == ["Edit", "Write"]


def test_prune_with_subcommand_null(db_conn: sqlite3.Connection) -> None:
    """Pruning correctly handles candidates with NULL subcommand."""
    _insert_session(db_conn)

    # Stale with NULL subcommand, no pending ask
    _insert_candidate(db_conn, "Read", subcommand=None, last_seen=_iso_ts(days_ago=31))

    result = prune_candidates(db_conn, stale_days=30)
    assert result == {"candidates_deleted": 1, "candidate_sessions_deleted": 0}


def test_prune_pending_ask_matches_subcommand_null(db_conn: sqlite3.Connection) -> None:
    """Pending ask with NULL subcommand protects matching candidate."""
    session_id = _insert_session(db_conn)

    # Stale candidate with NULL subcommand
    _insert_candidate(db_conn, "Read", subcommand=None, last_seen=_iso_ts(days_ago=31))

    # Pending ask with NULL subcommand (matches candidate)
    _insert_pending_ask(db_conn, "tool-123", session_id, "Read", subcommand=None)

    result = prune_candidates(db_conn, stale_days=30)
    assert result == {"candidates_deleted": 0, "candidate_sessions_deleted": 0}

    count = db_conn.execute("SELECT COUNT(*) FROM permission_candidates").fetchone()[0]
    assert count == 1


def test_prune_pending_ask_different_verb_does_not_match(
    db_conn: sqlite3.Connection,
) -> None:
    """Pending ask with different verb does not protect candidate."""
    session_id = _insert_session(db_conn)

    # Stale candidate
    _insert_candidate(db_conn, "Read", last_seen=_iso_ts(days_ago=31))

    # Pending ask with different verb
    _insert_pending_ask(db_conn, "tool-123", session_id, "Write")

    result = prune_candidates(db_conn, stale_days=30)
    assert result == {"candidates_deleted": 1, "candidate_sessions_deleted": 0}


def test_prune_idempotent(db_conn: sqlite3.Connection) -> None:
    """Pruning is idempotent (second run has same effect as first)."""
    _insert_candidate(db_conn, "Read", last_seen=_iso_ts(days_ago=31))
    _insert_candidate(db_conn, "Write", last_seen=_iso_ts())

    result1 = prune_candidates(db_conn, stale_days=30)
    result2 = prune_candidates(db_conn, stale_days=30)

    assert result1 == {"candidates_deleted": 1, "candidate_sessions_deleted": 0}
    assert result2 == {"candidates_deleted": 0, "candidate_sessions_deleted": 0}


def test_prune_cutoff_boundary(db_conn: sqlite3.Connection) -> None:
    """Candidates at the cutoff boundary are not deleted."""
    cutoff_ts = _iso_ts(days_ago=30)
    _insert_candidate(db_conn, "Read", last_seen=cutoff_ts)

    result = prune_candidates(db_conn, stale_days=30)
    assert result == {"candidates_deleted": 0, "candidate_sessions_deleted": 0}


def test_prune_custom_stale_days(db_conn: sqlite3.Connection) -> None:
    """Pruning respects custom stale_days parameter."""
    _insert_candidate(db_conn, "Read", last_seen=_iso_ts(days_ago=5))
    _insert_candidate(db_conn, "Write", last_seen=_iso_ts(days_ago=15))

    result = prune_candidates(db_conn, stale_days=10)
    assert result == {"candidates_deleted": 1, "candidate_sessions_deleted": 0}

    remaining = db_conn.execute("SELECT verb FROM permission_candidates").fetchall()
    # Read is 5 days old (kept), Write is 15 days old (deleted)
    assert remaining[0][0] == "Read"


def test_prune_multiple_sessions_per_candidate(db_conn: sqlite3.Connection) -> None:
    """Cascade delete removes all candidate-session links."""
    session_id1 = _insert_session(db_conn)
    session_id2 = _insert_session(db_conn)
    cand_id = _insert_candidate(db_conn, "Read", last_seen=_iso_ts(days_ago=31))

    _insert_candidate_session(db_conn, cand_id, session_id1)
    _insert_candidate_session(db_conn, cand_id, session_id2)

    result = prune_candidates(db_conn, stale_days=30)
    assert result == {"candidates_deleted": 1, "candidate_sessions_deleted": 2}


def test_prune_preserves_recent_with_sessions(db_conn: sqlite3.Connection) -> None:
    """Recent candidates with sessions are preserved."""
    session_id = _insert_session(db_conn)
    cand_id = _insert_candidate(db_conn, "Read", last_seen=_iso_ts(days_ago=5))
    _insert_candidate_session(db_conn, cand_id, session_id)

    result = prune_candidates(db_conn, stale_days=30)
    assert result == {"candidates_deleted": 0, "candidate_sessions_deleted": 0}

    count = db_conn.execute(
        "SELECT COUNT(*) FROM permission_candidate_sessions"
    ).fetchone()[0]
    assert count == 1
