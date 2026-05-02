"""Tests for the ``stats`` permissions subcommand and its formatting helpers.

Covers three behaviours that landed in P4:

- ``_format_time_saved`` — pure formatter for the "saved you Xm Ys of life"
  line. Tested in isolation so the boundary at 60 seconds is pinned.
- ``_cmd_stats`` printout includes the hit-count split (approved vs rejected)
  and the "Saved you" line, derived from approved hits.
- ``_cmd_stats`` includes a Redactions section sourced from
  ``redaction_events``. When that table is missing (older DB), prints an
  upgrade hint instead of crashing.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# _format_time_saved — pure formatter
# ---------------------------------------------------------------------------


class TestFormatTimeSaved:
    """Edge cases pinned: 0s, sub-60, exactly 60, multi-minute, default rate."""

    @pytest.mark.parametrize(
        "approved_hits,seconds_per_popup,expected",
        [
            (0, 5, "0s"),
            (1, 5, "5s"),
            (9, 5, "45s"),
            (11, 5, "55s"),
            (12, 5, "1m 0s"),  # exactly 60 seconds
            (13, 5, "1m 5s"),
            (25, 5, "2m 5s"),
            (1000, 5, "83m 20s"),
            # Default seconds_per_popup is 5 — call without it to verify.
        ],
    )
    def test_format(self, approved_hits, seconds_per_popup, expected):
        from nephoscope.cli.permissions_cmd import _format_time_saved

        assert _format_time_saved(approved_hits, seconds_per_popup) == expected

    def test_default_seconds_per_popup_is_five(self):
        from nephoscope.cli.permissions_cmd import _format_time_saved

        # 9 approved hits * default 5s = 45s
        assert _format_time_saved(9) == "45s"


# ---------------------------------------------------------------------------
# Helpers for the integration tests
# ---------------------------------------------------------------------------


def _bootstrap_db(db_path: Path) -> sqlite3.Connection:
    """Open an isolated DB with the current schema applied.

    Mirrors what ``conftest.py``'s ``tmp_db`` does, but returns the live
    connection so individual tests can seed their own rows without relying
    on the fixture's autouse path.
    """
    src_root = Path(__file__).resolve().parents[2] / "src"
    schema_sql = (src_root / "nephoscope" / "lib" / "schema.sql").read_text()
    conn = sqlite3.connect(str(db_path))
    conn.executescript(schema_sql)
    return conn


def _seed_rule_with_hits(
    conn: sqlite3.Connection,
    *,
    verb: str,
    decision: str,
    hit_count: int,
    last_hit_at: str = "2026-05-02T00:00:00Z",
) -> None:
    """Seed one rule_shapes + permissions row at global tier."""
    conn.execute(
        "INSERT INTO rule_shapes(verb, subcommand, flags, path_spec, context, tool,"
        "  first_seen, last_seen)"
        " VALUES (?, NULL, '*', NULL, 'any', 'Bash',"
        "         '2026-05-01T00:00:00Z', '2026-05-01T00:00:00Z');",
        (verb,),
    )
    rs_id = conn.execute("SELECT last_insert_rowid();").fetchone()[0]
    conn.execute(
        "INSERT INTO permissions"
        " (rule_shape_id, session_id, project_id, decision, source,"
        "  decided_at, hit_count, last_hit_at)"
        " VALUES (?, NULL, NULL, ?, 'manual',"
        "         '2026-05-01T00:00:00Z', ?, ?);",
        (rs_id, decision, hit_count, last_hit_at),
    )
    conn.commit()


def _seed_redaction_event(
    conn: sqlite3.Connection, pattern_name: str, *, tool_name: str = "Bash"
) -> None:
    conn.execute(
        "INSERT INTO redaction_events(session_id, pattern_name, tool_name, ts)"
        " VALUES (NULL, ?, ?, '2026-05-02T00:00:00Z');",
        (pattern_name, tool_name),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# _cmd_stats — hit-count split + time-saved
# ---------------------------------------------------------------------------


class TestStatsHitCountSplit:
    """The Total hits line shows N total + (A approved, R rejected) breakdown."""

    def test_split_line_present_with_breakdown(self, tmp_path, monkeypatch, capsys):
        from nephoscope.cli.permissions_cmd import _cmd_stats

        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))
        conn = _bootstrap_db(db_path)
        _seed_rule_with_hits(conn, verb="ls", decision="approved", hit_count=7)
        _seed_rule_with_hits(conn, verb="rm", decision="rejected", hit_count=3)
        conn.close()

        args = argparse.Namespace(db=str(db_path), show_unused=False)
        rc = _cmd_stats(args)
        assert rc == 0

        out = capsys.readouterr().out
        assert "Total hits:" in out
        assert "10" in out  # combined total
        assert "7 approved" in out
        assert "3 rejected" in out

    def test_time_saved_line_uses_approved_only(self, tmp_path, monkeypatch, capsys):
        from nephoscope.cli.permissions_cmd import _cmd_stats

        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))
        conn = _bootstrap_db(db_path)
        # 25 approved → 25 * 5s = 125s = 2m 5s
        _seed_rule_with_hits(conn, verb="ls", decision="approved", hit_count=25)
        # 100 rejected — must NOT count toward saved time.
        _seed_rule_with_hits(conn, verb="rm", decision="rejected", hit_count=100)
        conn.close()

        args = argparse.Namespace(db=str(db_path), show_unused=False)
        _cmd_stats(args)

        out = capsys.readouterr().out
        assert "Saved you:" in out
        assert "2m 5s" in out

    def test_time_saved_zero_when_no_approved_hits(self, tmp_path, monkeypatch, capsys):
        from nephoscope.cli.permissions_cmd import _cmd_stats

        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))
        conn = _bootstrap_db(db_path)
        _seed_rule_with_hits(conn, verb="rm", decision="rejected", hit_count=4)
        conn.close()

        args = argparse.Namespace(db=str(db_path), show_unused=False)
        _cmd_stats(args)

        out = capsys.readouterr().out
        assert "Saved you:" in out
        assert "0s" in out


# ---------------------------------------------------------------------------
# _cmd_stats — Redactions section
# ---------------------------------------------------------------------------


class TestStatsRedactionSection:
    """A new Redactions section sources from redaction_events."""

    def test_redaction_total_and_top_patterns_printed(
        self, tmp_path, monkeypatch, capsys
    ):
        from nephoscope.cli.permissions_cmd import _cmd_stats

        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))
        conn = _bootstrap_db(db_path)
        for _ in range(3):
            _seed_redaction_event(conn, "anthropic_api_key")
        _seed_redaction_event(conn, "aws_access_key_id")
        conn.close()

        args = argparse.Namespace(db=str(db_path), show_unused=False)
        _cmd_stats(args)

        out = capsys.readouterr().out
        assert "Redactions:" in out
        assert "4 total" in out
        assert "Top patterns:" in out

        # anthropic_api_key (3) must precede aws_access_key_id (1).
        antho_idx = out.index("anthropic_api_key")
        aws_idx = out.index("aws_access_key_id")
        assert antho_idx < aws_idx

    def test_redactions_section_handles_zero(self, tmp_path, monkeypatch, capsys):
        from nephoscope.cli.permissions_cmd import _cmd_stats

        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))
        _bootstrap_db(db_path).close()

        args = argparse.Namespace(db=str(db_path), show_unused=False)
        _cmd_stats(args)

        out = capsys.readouterr().out
        assert "Redactions:" in out
        assert "0 total" in out

    def test_old_db_without_redaction_events_table_falls_back_gracefully(
        self, tmp_path, monkeypatch, capsys
    ):
        """If the redaction_events table doesn't exist (old DB), the command
        prints a hint instead of raising."""
        from nephoscope.cli.permissions_cmd import _cmd_stats

        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))
        conn = _bootstrap_db(db_path)
        conn.execute("DROP TABLE redaction_events;")
        conn.commit()
        conn.close()

        args = argparse.Namespace(db=str(db_path), show_unused=False)
        rc = _cmd_stats(args)
        assert rc == 0

        out = capsys.readouterr().out
        assert "Redactions:" in out
        assert "nephoscope-init" in out
