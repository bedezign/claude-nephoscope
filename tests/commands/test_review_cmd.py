"""Tests for cli.review_cmd — ``nephoscope-review`` console script.

Covers:
  - argparse happy path (default invocation, no candidates: exit 0, print message)
  - stdin-driven prompt walk for a review session (per-axis prompts)
  - MirrorHashMismatch path: monkeypatched promote raises MirrorHashMismatch → exit 1

Test isolation: all DB access goes through ``tmp_db`` / ``monkeypatch.setenv``
so no live DB is touched.  Prompt input is injected via ``monkeypatch.setattr``
on ``sys.stdin``.
"""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path
from collections.abc import Generator
from unittest import mock

import pytest

from nephoscope.cli.review_cmd import main, _read_line


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_PATH = PROJECT_ROOT / "src" / "nephoscope" / "lib" / "schema.sql"


@pytest.fixture
def conn(tmp_db) -> Generator[sqlite3.Connection, None, None]:
    tmp_db.execute("INSERT OR IGNORE INTO tools(name) VALUES ('Bash')")
    tmp_db.execute(
        "INSERT OR IGNORE INTO permission_modes(name) VALUES"
        " ('default'),('acceptEdits'),('bypassPermissions'),('plan'),('auto')"
    )
    tmp_db.execute(
        "INSERT OR IGNORE INTO call_statuses(name) VALUES"
        " ('pending'),('ok'),('err'),('denied'),('orphan')"
    )
    tmp_db.commit()
    yield tmp_db


# ---------------------------------------------------------------------------
# _read_line — stdin helper
# ---------------------------------------------------------------------------


def test_read_line_from_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\n"))
    assert _read_line() == "hello"


def test_read_line_returns_empty_on_eof(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert _read_line() == ""


# ---------------------------------------------------------------------------
# main() — no candidates: exits 0, prints informative message
# ---------------------------------------------------------------------------


def test_main_no_candidates_exits_zero(conn, capsys):
    # No candidates seeded → propose_promotions returns [].
    rc = main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "no promotion candidates" in captured.out.lower()


# ---------------------------------------------------------------------------
# main() — with one candidate: stdin drives per-axis prompts
# ---------------------------------------------------------------------------


def _seed_candidate(conn: sqlite3.Connection, verb: str, flags_json: str) -> None:
    """Seed a single permission_candidates row that exceeds default thresholds."""
    now = "2024-01-01T00:00:00Z"
    conn.execute(
        "INSERT OR REPLACE INTO projects(cwd, name, root, first_seen, last_seen)"
        " VALUES ('/proj', 'proj', '/proj', ?, ?)",
        (now, now),
    )
    proj_id = conn.execute("SELECT id FROM projects WHERE cwd='/proj'").fetchone()[0]

    for i in range(6):
        uuid = f"sess-{i}"
        conn.execute(
            "INSERT OR IGNORE INTO sessions(session_uuid, project_id, transcript_path,"
            " started_at, last_activity)"
            " VALUES (?, ?, '/t', ?, ?)",
            (uuid, proj_id, now, now),
        )

    # Insert one candidate row with 6 observations across 6 sessions.
    conn.execute(
        "INSERT OR IGNORE INTO permission_candidates"
        " (verb, subcommand, flags, observations, distinct_sessions,"
        "  first_seen, last_seen)"
        " VALUES (?, NULL, ?, 6, 6, ?, ?)",
        (verb, flags_json, now, now),
    )
    conn.commit()


def test_main_skip_on_flags_axis(conn, monkeypatch, capsys):
    """'s' at Flags axis → candidate skipped; exit 0."""
    _seed_candidate(conn, "ls", "[]")

    # Provide answers: no verb pattern (skip axis 1), paths=a(any), flags=s(skip)
    responses = iter(["a", "s"])  # paths=a, flags=s
    monkeypatch.setattr("nephoscope.cli.review_cmd._read_line", lambda: next(responses))

    # Mock scan_candidates to return 0 (already scanned) and propose_promotions
    # to return a realistic Candidate.
    from nephoscope.learners.permission.learner import Candidate

    candidate = Candidate(
        id=1,
        verb="ls",
        subcommand=None,
        flags=frozenset(),
        observations=6,
        distinct_sessions=6,
    )

    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch(
            "nephoscope.cli.review_cmd.propose_promotions", return_value=[candidate]
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
    ):
        rc = main([])

    assert rc == 0
    out = capsys.readouterr().out
    assert "skipped" in out


def test_main_promote_global_tier(conn, monkeypatch, capsys):
    """Global tier selection calls promote and reports promoted count."""
    from nephoscope.learners.permission.learner import Candidate

    candidate = Candidate(
        id=1,
        verb="git",
        subcommand="status",
        flags=frozenset(),
        observations=6,
        distinct_sessions=6,
    )

    # Per-axis answers: paths=a, flags=l (literal), tier=g (global)
    responses = iter(["a", "l", "g"])
    monkeypatch.setattr("nephoscope.cli.review_cmd._read_line", lambda: next(responses))

    mock_promote = mock.MagicMock(return_value=0)

    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch(
            "nephoscope.cli.review_cmd.propose_promotions", return_value=[candidate]
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
        mock.patch("nephoscope.cli.review_cmd._do_promote", mock_promote),
    ):
        rc = main([])

    assert rc == 0
    assert mock_promote.called
    out = capsys.readouterr().out
    assert "promoted" in out


# ---------------------------------------------------------------------------
# MirrorHashMismatch propagation → exit 1
# ---------------------------------------------------------------------------


def test_main_mirror_hash_mismatch_exits_one(conn, monkeypatch, capsys):
    """When promote raises MirrorHashMismatch, review exits 1 with the expected message."""
    from nephoscope.learners.permission.learner import Candidate
    from nephoscope.lib.mirror.writer import MirrorHashMismatch

    candidate = Candidate(
        id=1,
        verb="git",
        subcommand="status",
        flags=frozenset(),
        observations=6,
        distinct_sessions=6,
    )

    # paths=a, flags=l, tier=g
    responses = iter(["a", "l", "g"])
    monkeypatch.setattr("nephoscope.cli.review_cmd._read_line", lambda: next(responses))

    def raise_mismatch(*_a, **_kw):
        raise MirrorHashMismatch("/path/to/settings.json: hash mismatch")

    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch(
            "nephoscope.cli.review_cmd.propose_promotions", return_value=[candidate]
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
        mock.patch("nephoscope.cli.review_cmd._do_promote", raise_mismatch),
    ):
        rc = main([])

    assert rc == 1
    out_err = capsys.readouterr()
    combined = out_err.out + out_err.err
    assert "edited externally" in combined or "reconcile" in combined


# ---------------------------------------------------------------------------
# quit at tier prompt stops the loop early
# ---------------------------------------------------------------------------


def test_main_quit_at_tier_exits_loop(conn, monkeypatch, capsys):
    from nephoscope.learners.permission.learner import Candidate

    candidate = Candidate(
        id=1,
        verb="rm",
        subcommand=None,
        flags=frozenset(["-rf"]),
        observations=6,
        distinct_sessions=6,
    )

    # paths=a, flags=l, tier=q (quit)
    responses = iter(["a", "l", "q"])
    monkeypatch.setattr("nephoscope.cli.review_cmd._read_line", lambda: next(responses))

    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch(
            "nephoscope.cli.review_cmd.propose_promotions", return_value=[candidate]
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
    ):
        rc = main([])

    assert rc == 0
    out = capsys.readouterr().out
    assert "quitting" in out.lower() or "quit" in out.lower()
