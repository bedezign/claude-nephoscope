"""Tests for path-aware permission_candidates (Finding 10).

Covers:
- upsert_candidate stores and merges positional_paths
- Cap at _MAX_POSITIONAL_PATHS (20)
- NULL when no paths provided
- propose_promotions returns Candidate with paths from DB
- end-to-end scan_candidates persists positional_paths
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator

import argparse

import pytest

import nephoscope.lib.db as db
from nephoscope.lib.db import _merge_paths
from nephoscope.learners.permission.learner import (
    _cmd_candidates,
    _cmd_scan,
    _format_paths_preview,
    _get_cursor,
    propose_promotions,
    scan_candidates,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_db) -> Generator[sqlite3.Connection, None, None]:
    """Yield the tmp_db connection with Bash tool row seeded."""
    tmp_db.execute("INSERT OR IGNORE INTO tools(name) VALUES ('Bash')")
    tmp_db.commit()
    yield tmp_db


def _now() -> str:
    import datetime as _dt

    return (
        _dt.datetime.now(tz=_dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _make_session(conn: sqlite3.Connection, uuid: str = "sess-paths-1") -> int:
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO projects(cwd, name, root, first_seen, last_seen)"
        " VALUES ('/proj', 'p', '/proj', ?, ?)",
        (now, now),
    )
    conn.commit()
    proj_id = conn.execute("SELECT id FROM projects WHERE cwd='/proj'").fetchone()[0]
    conn.execute(
        "INSERT OR IGNORE INTO sessions"
        "(session_uuid, project_id, started_at, last_activity)"
        " VALUES (?, ?, ?, ?)",
        (uuid, proj_id, now, now),
    )
    conn.commit()
    return int(
        conn.execute(
            "SELECT id FROM sessions WHERE session_uuid=?", (uuid,)
        ).fetchone()[0]
    )


def _seed_candidate_with_paths(
    conn: sqlite3.Connection,
    verb: str,
    subcommand: str | None,
    flags_json: str,
    observations: int,
    distinct_sessions: int,
    paths_json: str | None = None,
) -> int:
    """Insert a permission_candidates row with optional positional_paths."""
    now = _now()
    cur = conn.execute(
        "INSERT INTO permission_candidates"
        "(verb, subcommand, flags, observations, distinct_sessions,"
        " first_seen, last_seen, positional_paths)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            verb,
            subcommand,
            flags_json,
            observations,
            distinct_sessions,
            now,
            now,
            paths_json,
        ),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# upsert_candidate: stores positional_paths on insert
# ---------------------------------------------------------------------------


def test_upsert_candidate_stores_positional_paths(conn):
    """Insert with paths — JSON is stored in positional_paths column."""
    sess_id = _make_session(conn)
    now = _now()
    paths = ("/home/user/.claude/hooks/foo.py", "/tmp/bar.py")
    flags_json = db.minify_json([])

    cand_id = db.upsert_candidate(
        conn, "python3", None, flags_json, sess_id, now, paths
    )

    row = conn.execute(
        "SELECT positional_paths FROM permission_candidates WHERE id = ?;",
        (cand_id,),
    ).fetchone()
    assert row is not None
    stored = json.loads(row[0])
    assert set(stored) == set(paths)


# ---------------------------------------------------------------------------
# upsert_candidate: merges paths on update
# ---------------------------------------------------------------------------


def test_upsert_candidate_merges_paths_on_update(conn):
    """Two calls with different paths produce a merged set."""
    sess_id = _make_session(conn)
    now = _now()
    flags_json = db.minify_json([])

    paths_first = ("/tmp/a.py",)
    cand_id = db.upsert_candidate(
        conn, "python3", None, flags_json, sess_id, now, paths_first
    )

    sess_id2 = _make_session(conn, "sess-paths-2")
    paths_second = ("/tmp/b.py",)
    cand_id2 = db.upsert_candidate(
        conn, "python3", None, flags_json, sess_id2, now, paths_second
    )

    assert cand_id == cand_id2  # same candidate
    row = conn.execute(
        "SELECT positional_paths FROM permission_candidates WHERE id = ?;",
        (cand_id,),
    ).fetchone()
    stored = set(json.loads(row[0]))
    assert stored == {"/tmp/a.py", "/tmp/b.py"}


# ---------------------------------------------------------------------------
# upsert_candidate: caps at _MAX_POSITIONAL_PATHS
# ---------------------------------------------------------------------------


def test_upsert_candidate_caps_at_max_paths(conn):
    """Progressive upserts with 25 distinct paths cap at 20."""
    now = _now()
    flags_json = db.minify_json([])

    # Each call adds one new path via a distinct session to avoid same-session dedup.
    for i in range(25):
        si = _make_session(conn, f"sess-cap-{i}")
        db.upsert_candidate(
            conn, "python3", None, flags_json, si, now, (f"/tmp/script_{i:02d}.py",)
        )

    row = conn.execute(
        "SELECT positional_paths FROM permission_candidates WHERE verb = ?;",
        ("python3",),
    ).fetchone()
    stored = json.loads(row[0])
    assert len(stored) == db._MAX_POSITIONAL_PATHS


# ---------------------------------------------------------------------------
# upsert_candidate: no paths → NULL
# ---------------------------------------------------------------------------


def test_upsert_candidate_without_paths_stores_null(conn):
    """NULL is stored in positional_paths when no paths argument is given."""
    sess_id = _make_session(conn)
    now = _now()
    flags_json = db.minify_json([])

    cand_id = db.upsert_candidate(conn, "git", "status", flags_json, sess_id, now)

    row = conn.execute(
        "SELECT positional_paths FROM permission_candidates WHERE id = ?;",
        (cand_id,),
    ).fetchone()
    assert row[0] is None


# ---------------------------------------------------------------------------
# propose_promotions: returns Candidate with positional_paths from DB
# ---------------------------------------------------------------------------


def test_propose_promotions_returns_positional_paths(conn):
    """Positional paths stored in DB appear on the returned Candidate."""
    paths_json = json.dumps(["/home/user/.claude/hooks/foo.py"])
    _seed_candidate_with_paths(
        conn,
        "python3",
        None,
        "[]",
        observations=10,
        distinct_sessions=3,
        paths_json=paths_json,
    )

    proposals = propose_promotions(conn)

    assert len(proposals) == 1
    c = proposals[0]
    assert c.positional_paths == ("/home/user/.claude/hooks/foo.py",)


# ---------------------------------------------------------------------------
# scan_candidates: end-to-end — paths from a script runner command
# ---------------------------------------------------------------------------


def test_scan_candidates_persists_positional_paths(conn):
    """scan_candidates stores positional_paths for a script runner command."""
    sess_id = _make_session(conn)
    now = _now()

    # Insert a synthetic tool_call row: Bash, ok status, session_id set.
    bash_id = conn.execute("SELECT id FROM tools WHERE name='Bash'").fetchone()[0]
    ok_id = conn.execute("SELECT id FROM call_statuses WHERE name='ok'").fetchone()[0]
    conn.execute(
        "INSERT INTO tool_calls(ts, session_id, tool_id, status_id, command)"
        " VALUES (?, ?, ?, ?, ?)",
        (now, sess_id, bash_id, ok_id, "python3 /tmp/test_script.py"),
    )
    conn.commit()

    scan_candidates(conn)

    row = conn.execute(
        "SELECT positional_paths FROM permission_candidates WHERE verb = ?;",
        ("python3",),
    ).fetchone()
    assert row is not None, "Candidate row not created"
    assert row[0] is not None, "positional_paths should not be NULL"
    stored = json.loads(row[0])
    assert "/tmp/test_script.py" in stored


# ---------------------------------------------------------------------------
# _merge_paths: corrupted JSON treated as empty set
# ---------------------------------------------------------------------------


def test_merge_paths_with_corrupted_json_treats_as_empty_set():
    """_merge_paths with corrupted existing JSON falls back to an empty set.

    The corrupted value is discarded; new_paths become the entire stored set.
    """
    result = _merge_paths("not-json", ("/tmp/path.py",))
    assert result is not None
    assert json.loads(result) == ["/tmp/path.py"]


def test_merge_paths_with_type_error_treats_as_empty_set():
    """_merge_paths with a non-string existing_json (e.g. numeric) treats it as empty."""
    # json.loads(123) raises TypeError — exercise that branch too.
    result = _merge_paths(123, ("/tmp/path.py",))  # type: ignore[arg-type]
    assert result is not None
    assert json.loads(result) == ["/tmp/path.py"]


def test_merge_paths_cap_boundary_path_sorted_after_all_existing():
    """New path that sorts after all 20 existing paths is dropped by the cap.

    When the stored set is already at the cap (20 entries) and the new path
    sorts after all of them, merging grows the set to 21 but the slice back to
    20 drops the new path entirely.  The result equals the original stored set,
    so _merge_paths must return None (no spurious UPDATE).
    """
    existing_paths = [f"/p{i:02d}.py" for i in range(20)]  # /p00.py … /p19.py
    existing_json = json.dumps(existing_paths)

    # '/zzz.py' sorts after '/p19.py', so it falls off the cap.
    result = _merge_paths(existing_json, ("/zzz.py",))

    assert result is None, (
        "Expected None (no update needed) but got a new JSON string; "
        "the cap-boundary guard is not working correctly."
    )


# ---------------------------------------------------------------------------
# _cmd_scan path display: boundary at exactly 3 and 4 paths
# ---------------------------------------------------------------------------


def _seed_promotable_candidate(
    conn: sqlite3.Connection,
    verb: str,
    paths_json: str | None = None,
) -> None:
    """Insert a permission_candidates row that meets default promotion thresholds.

    Uses observations=10 and distinct_sessions=3, which satisfy the default
    min_observations=5 / min_distinct_sessions=2 thresholds.
    """
    _seed_candidate_with_paths(
        conn,
        verb=verb,
        subcommand=None,
        flags_json="[]",
        observations=10,
        distinct_sessions=3,
        paths_json=paths_json,
    )


def test_cmd_scan_path_display_exactly_3_paths(conn, capsys, monkeypatch):
    """No 'more' suffix when positional_paths has exactly 3 entries."""
    paths = ["/tmp/a.py", "/tmp/b.py", "/tmp/c.py"]
    _seed_promotable_candidate(conn, "python3", json.dumps(paths))

    monkeypatch.setattr("nephoscope.learners.permission.learner.connect", lambda: conn)
    _cmd_scan(argparse.Namespace())
    out = capsys.readouterr().out

    assert "/tmp/a.py, /tmp/b.py, /tmp/c.py" in out
    assert "more" not in out


def test_cmd_scan_path_display_exactly_4_paths(conn, capsys, monkeypatch):
    """'... and 1 more' suffix when positional_paths has exactly 4 entries."""
    paths = ["/tmp/a.py", "/tmp/b.py", "/tmp/c.py", "/tmp/d.py"]
    _seed_promotable_candidate(conn, "python3", json.dumps(paths))

    monkeypatch.setattr("nephoscope.learners.permission.learner.connect", lambda: conn)
    _cmd_scan(argparse.Namespace())
    out = capsys.readouterr().out

    assert "/tmp/a.py, /tmp/b.py, /tmp/c.py" in out
    assert "... and 1 more" in out


# ---------------------------------------------------------------------------
# _cmd_candidates path display: boundary at exactly 3 and 4 paths
# ---------------------------------------------------------------------------


def test_cmd_candidates_path_display_exactly_3_paths(conn, capsys, monkeypatch):
    """No 'more' suffix when positional_paths has exactly 3 entries in candidates view."""
    paths = ["/tmp/a.py", "/tmp/b.py", "/tmp/c.py"]
    _seed_candidate_with_paths(
        conn,
        "python3",
        None,
        "[]",
        observations=1,
        distinct_sessions=1,
        paths_json=json.dumps(paths),
    )

    monkeypatch.setattr("nephoscope.learners.permission.learner.connect", lambda: conn)
    _cmd_candidates(argparse.Namespace())
    out = capsys.readouterr().out

    assert "/tmp/a.py, /tmp/b.py, /tmp/c.py" in out
    assert "more" not in out


def test_cmd_candidates_path_display_exactly_4_paths(conn, capsys, monkeypatch):
    """'... and 1 more' suffix when positional_paths has exactly 4 entries in candidates view."""
    paths = ["/tmp/a.py", "/tmp/b.py", "/tmp/c.py", "/tmp/d.py"]
    _seed_candidate_with_paths(
        conn,
        "python3",
        None,
        "[]",
        observations=1,
        distinct_sessions=1,
        paths_json=json.dumps(paths),
    )

    monkeypatch.setattr("nephoscope.learners.permission.learner.connect", lambda: conn)
    _cmd_candidates(argparse.Namespace())
    out = capsys.readouterr().out

    assert "/tmp/a.py, /tmp/b.py, /tmp/c.py" in out
    assert "... and 1 more" in out


# ---------------------------------------------------------------------------
# Unicode paths: round-trip through upsert_candidate + propose_promotions
# ---------------------------------------------------------------------------


def test_unicode_path_round_trips_through_propose_promotions(conn):
    """A unicode path survives upsert_candidate and appears unchanged in propose_promotions."""
    unicode_path = "/home/user/café/script.py"
    now = _now()
    flags_json = db.minify_json([])

    for i in range(5):
        sess_id = _make_session(conn, f"sess-unicode-{i}")
        db.upsert_candidate(
            conn, "python3", None, flags_json, sess_id, now, (unicode_path,)
        )

    proposals = propose_promotions(conn)

    assert len(proposals) == 1
    assert unicode_path in proposals[0].positional_paths


# ---------------------------------------------------------------------------
# scan_candidates: cursor does NOT advance when upsert_candidate raises mid-loop
# ---------------------------------------------------------------------------


def test_scan_candidates_cursor_does_not_advance_on_upsert_exception(conn, monkeypatch):
    """If upsert_candidate raises mid-loop, _set_cursor is never reached.

    scan_candidates has no try/except around the upsert call — an exception
    propagates out of the function and leaves the cursor unchanged at whatever
    value it held before the scan.

    This test documents the behaviour: the cursor stays at 0 when the scan
    fails on every row.
    """
    sess_id = _make_session(conn)
    now = _now()
    bash_id = conn.execute("SELECT id FROM tools WHERE name='Bash'").fetchone()[0]
    ok_id = conn.execute("SELECT id FROM call_statuses WHERE name='ok'").fetchone()[0]

    for i in range(2):
        conn.execute(
            "INSERT INTO tool_calls(ts, session_id, tool_id, status_id, command)"
            " VALUES (?, ?, ?, ?, ?)",
            (now, sess_id, bash_id, ok_id, f"python3 /tmp/script_{i}.py"),
        )
    conn.commit()

    def _raise(*_a: object, **_kw: object) -> None:
        raise RuntimeError("injected")

    monkeypatch.setattr(db, "upsert_candidate", _raise)

    with pytest.raises(RuntimeError, match="injected"):
        scan_candidates(conn)

    assert _get_cursor(conn) == 0


# ---------------------------------------------------------------------------
# _format_paths_preview: empty sequence returns empty string
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("paths", [(), []])
def test_format_paths_preview_empty_sequence_returns_empty_string(paths):
    assert _format_paths_preview(paths) == ""


# ---------------------------------------------------------------------------
# propose_promotions: NULL positional_paths column returns Candidate with ()
# ---------------------------------------------------------------------------


def test_propose_promotions_null_paths_returns_empty_tuple(conn):
    """Candidate with NULL positional_paths produces Candidate.positional_paths == ().

    Uses upsert_candidate without the paths argument to ensure NULL is stored,
    exercising the `if raw_paths else []` branch in propose_promotions.
    """
    now = _now()
    flags_json = db.minify_json([])

    for i in range(5):  # 5 to meet min_observations=5 threshold
        sess_id = _make_session(conn, f"sess-nullpaths-{i}")
        db.upsert_candidate(conn, "rg", None, flags_json, sess_id, now)

    proposals = propose_promotions(conn)

    assert len(proposals) == 1
    assert proposals[0].positional_paths == ()


# ---------------------------------------------------------------------------
# propose_promotions: below-threshold candidate is excluded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "obs,sess,label",
    [
        (
            1,
            3,
            "observations_only",
        ),  # meets min_distinct_sessions=2, fails min_observations=5
        (
            6,
            1,
            "sessions_only",
        ),  # meets min_observations=5, fails min_distinct_sessions=2
        (1, 1, "both_below"),  # fails both thresholds
    ],
)
def test_propose_promotions_excludes_below_threshold_candidate(conn, obs, sess, label):
    """Candidate that fails one or both promotion thresholds is not returned.

    Default thresholds: min_observations=5, min_distinct_sessions=2.
    Each parametrized case isolates a distinct failure mode.
    """
    _seed_candidate_with_paths(
        conn,
        verb=f"below_thresh_{label}",
        subcommand=None,
        flags_json="[]",
        observations=obs,
        distinct_sessions=sess,
    )

    proposals = propose_promotions(conn)

    assert proposals == []


# ---------------------------------------------------------------------------
# propose_promotions: suggested_path_spec wiring
# ---------------------------------------------------------------------------


def _seed_project_with_root(conn: sqlite3.Connection, root: str) -> int:
    """Insert a projects row with the given root; return its id."""
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO projects(cwd, name, root, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?)",
        (root, root, root, now, now),
    )
    conn.commit()
    return int(
        conn.execute("SELECT id FROM projects WHERE root=?", (root,)).fetchone()[0]
    )


def _seed_promotable_candidate_with_paths(
    conn: sqlite3.Connection,
    verb: str,
    paths: list[str],
) -> None:
    """Insert a promotable candidate (10 obs, 3 sessions) with specified paths."""
    _seed_candidate_with_paths(
        conn,
        verb=verb,
        subcommand=None,
        flags_json="[]",
        observations=10,
        distinct_sessions=3,
        paths_json=json.dumps(paths),
    )


def test_propose_promotions_suggests_project_root_when_paths_qualify(conn, monkeypatch):
    """Candidate with paths all under a known project root gets '$PROJECT_ROOT/**'."""
    root = "/work/my-project"
    _seed_project_with_root(conn, root)
    paths = [f"{root}/src/file_{i}.py" for i in range(5)]
    _seed_promotable_candidate_with_paths(conn, "grep", paths)
    monkeypatch.setenv("HOME", "/home/user")

    proposals = propose_promotions(conn)

    assert len(proposals) == 1
    assert proposals[0].suggested_path_spec == "$PROJECT_ROOT/**"


def test_propose_promotions_suggests_home_when_paths_under_home_only(conn, monkeypatch):
    """Candidate with paths all under home (no project root) gets '$HOME/**'."""
    home = "/home/user"
    monkeypatch.setenv("HOME", home)
    paths = [f"{home}/scripts/tool_{i}.sh" for i in range(5)]
    _seed_promotable_candidate_with_paths(conn, "bash", paths)

    proposals = propose_promotions(conn)

    assert len(proposals) == 1
    assert proposals[0].suggested_path_spec == "$HOME/**"


def test_propose_promotions_no_suggestion_when_paths_are_null(conn, monkeypatch):
    """Candidate without positional_paths gets suggested_path_spec=None."""
    monkeypatch.setenv("HOME", "/home/user")
    now = _now()
    flags_json = db.minify_json([])
    for i in range(5):
        sess_id = _make_session(conn, f"sess-nosugg-{i}")
        db.upsert_candidate(conn, "wc", None, flags_json, sess_id, now)

    proposals = propose_promotions(conn)

    assert len(proposals) == 1
    assert proposals[0].suggested_path_spec is None
