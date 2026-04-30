"""Tests for rule hit counters.

Covers:
- permissions table has hit_count and last_hit_at columns
- v_permissions view exposes hit_count and last_hit_at
- Hook increments hit_count on matched allow/deny
- Counter stays 0 for unmatched rules
- list --sort hits orders correctly
- stats subcommand returns correct output
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from nephoscope.lib.db import _now, _open, insert_permission, upsert_rule_shape


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """Open an isolated test DB."""
    db = tmp_path / "test.db"
    monkeypatch.setenv("OBSERVABILITY_DB", str(db))
    return _open()


def _insert_permission_direct(
    conn: sqlite3.Connection,
    shape_id: int,
    decision: str = "approved",
) -> int:
    """Insert a permission via insert_permission and return its id."""
    return insert_permission(conn, shape_id, None, None, decision, "seed", _now())


# ---------------------------------------------------------------------------
# Schema: hit_count + last_hit_at columns
# ---------------------------------------------------------------------------


class TestHitCounterSchema:
    def test_permissions_has_hit_count_column(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """permissions table must have a hit_count column."""
        conn = _make_db(tmp_path, monkeypatch)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(permissions);")}
        assert "hit_count" in cols, f"hit_count column missing; columns: {cols}"

    def test_permissions_has_last_hit_at_column(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """permissions table must have a last_hit_at column."""
        conn = _make_db(tmp_path, monkeypatch)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(permissions);")}
        assert "last_hit_at" in cols, f"last_hit_at column missing; columns: {cols}"

    def test_hit_count_defaults_to_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """New permission rows must have hit_count=0."""
        conn = _make_db(tmp_path, monkeypatch)
        shape_id = upsert_rule_shape(conn, "rm", None, "[]", None, _now())
        perm_id = _insert_permission_direct(conn, shape_id)
        row = conn.execute(
            "SELECT hit_count FROM permissions WHERE id = ?;", (perm_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == 0, f"Expected hit_count=0, got {row[0]}"

    def test_last_hit_at_defaults_to_null(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """New permission rows must have last_hit_at=NULL."""
        conn = _make_db(tmp_path, monkeypatch)
        shape_id = upsert_rule_shape(conn, "rm", None, "[]", None, _now())
        perm_id = _insert_permission_direct(conn, shape_id)
        row = conn.execute(
            "SELECT last_hit_at FROM permissions WHERE id = ?;", (perm_id,)
        ).fetchone()
        assert row is not None
        assert row[0] is None, f"Expected last_hit_at=NULL, got {row[0]!r}"

    def test_v_permissions_view_exposes_hit_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v_permissions view must include hit_count column."""
        conn = _make_db(tmp_path, monkeypatch)
        conn.execute("SELECT * FROM v_permissions LIMIT 0;")
        # PRAGMA on a view isn't available; use cursor description instead
        conn.execute(
            "INSERT INTO rule_shapes(verb, subcommand, flags, path_spec, context, first_seen, last_seen) VALUES ('ls', NULL, '[]', NULL, 'any', '2025Z', '2025Z');"
        )
        shape_id = conn.execute(
            "SELECT id FROM rule_shapes WHERE verb='ls';"
        ).fetchone()[0]
        _insert_permission_direct(conn, shape_id)
        cursor = conn.execute("SELECT * FROM v_permissions;")
        col_names = {d[0] for d in cursor.description}
        assert "hit_count" in col_names, (
            f"hit_count missing from v_permissions; columns: {col_names}"
        )

    def test_v_permissions_view_exposes_last_hit_at(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v_permissions view must include last_hit_at column."""
        conn = _make_db(tmp_path, monkeypatch)
        conn.execute(
            "INSERT INTO rule_shapes(verb, subcommand, flags, path_spec, context, first_seen, last_seen) VALUES ('ls', NULL, '[]', NULL, 'any', '2025Z', '2025Z');"
        )
        shape_id = conn.execute(
            "SELECT id FROM rule_shapes WHERE verb='ls';"
        ).fetchone()[0]
        _insert_permission_direct(conn, shape_id)
        cursor = conn.execute("SELECT * FROM v_permissions;")
        col_names = {d[0] for d in cursor.description}
        assert "last_hit_at" in col_names, (
            f"last_hit_at missing from v_permissions; columns: {col_names}"
        )


# ---------------------------------------------------------------------------
# Hook increments hit_count on match
# ---------------------------------------------------------------------------


class TestHookIncrementsHitCount:
    def test_increment_hit_count_on_allow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Calling increment_hit (the hook-side helper) increments hit_count."""
        conn = _make_db(tmp_path, monkeypatch)
        shape_id = upsert_rule_shape(conn, "git", None, "[]", None, _now())
        perm_id = _insert_permission_direct(conn, shape_id, "approved")

        from nephoscope.learners.permission.hook import _increment_hit

        _increment_hit(conn, perm_id)

        row = conn.execute(
            "SELECT hit_count, last_hit_at FROM permissions WHERE id = ?;", (perm_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == 1, f"Expected hit_count=1, got {row[0]}"
        assert row[1] is not None, "Expected last_hit_at to be set"

    def test_increment_hit_count_on_deny(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rejected rules also get hit_count incremented when matched."""
        conn = _make_db(tmp_path, monkeypatch)
        shape_id = upsert_rule_shape(conn, "rm", None, "[]", None, _now())
        perm_id = _insert_permission_direct(conn, shape_id, "rejected")

        from nephoscope.learners.permission.hook import _increment_hit

        _increment_hit(conn, perm_id)

        row = conn.execute(
            "SELECT hit_count FROM permissions WHERE id = ?;", (perm_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == 1, f"Expected hit_count=1 for rejected rule, got {row[0]}"

    def test_increment_hit_count_accumulates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated increments accumulate."""
        conn = _make_db(tmp_path, monkeypatch)
        shape_id = upsert_rule_shape(conn, "ls", None, "[]", None, _now())
        perm_id = _insert_permission_direct(conn, shape_id)

        from nephoscope.learners.permission.hook import _increment_hit

        for _ in range(5):
            _increment_hit(conn, perm_id)

        row = conn.execute(
            "SELECT hit_count FROM permissions WHERE id = ?;", (perm_id,)
        ).fetchone()
        assert row[0] == 5, f"Expected hit_count=5, got {row[0]}"

    def test_unmatched_rule_hit_count_stays_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rule that is never matched keeps hit_count=0."""
        conn = _make_db(tmp_path, monkeypatch)
        shape_id = upsert_rule_shape(conn, "curl", None, "[]", None, _now())
        perm_id = _insert_permission_direct(conn, shape_id)

        # No increment called — hit_count must stay 0
        row = conn.execute(
            "SELECT hit_count FROM permissions WHERE id = ?;", (perm_id,)
        ).fetchone()
        assert row[0] == 0, f"Expected hit_count=0 for unmatched rule, got {row[0]}"

    def test_increment_hit_none_permission_id_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_increment_hit with None permission_id is a silent no-op."""
        conn = _make_db(tmp_path, monkeypatch)
        from nephoscope.learners.permission.hook import _increment_hit

        # Must not raise
        try:
            _increment_hit(conn, None)
        except Exception as exc:
            pytest.fail(f"_increment_hit(conn, None) raised: {exc}")


# ---------------------------------------------------------------------------
# list --sort hits
# ---------------------------------------------------------------------------


class TestListSortHits:
    def test_list_sort_hits_orders_by_hit_count_desc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--sort hits orders rules by hit_count descending."""
        conn = _make_db(tmp_path, monkeypatch)
        now = _now()

        # Insert three rules with different hit counts
        for verb, hits in [("git", 10), ("rm", 3), ("ls", 50)]:
            shape_id = upsert_rule_shape(conn, verb, None, "[]", None, now)
            perm_id = _insert_permission_direct(conn, shape_id)
            conn.execute(
                "UPDATE permissions SET hit_count = ? WHERE id = ?;",
                (hits, perm_id),
            )

        # Query via v_permissions ordered by hit_count desc
        rows = conn.execute(
            "SELECT verb, hit_count FROM v_permissions ORDER BY hit_count DESC;"
        ).fetchall()
        verbs = [r[0] for r in rows]
        assert verbs == ["ls", "git", "rm"], (
            f"Expected ls, git, rm (by hits desc), got {verbs}"
        )


# ---------------------------------------------------------------------------
# stats subcommand
# ---------------------------------------------------------------------------


class TestStatsSubcommand:
    def test_stats_returns_zero_counts_on_empty_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stats subcommand reports 0 rules on an empty DB."""
        from nephoscope.cli.permissions_cmd import _cmd_stats

        _make_db(tmp_path, monkeypatch)
        args = argparse.Namespace(show_unused=False)
        result = _cmd_stats(args)
        assert result == 0, f"Expected exit 0, got {result}"

    def test_stats_reflects_hit_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stats subcommand correctly counts total hits."""
        conn = _make_db(tmp_path, monkeypatch)
        now = _now()
        shape_id = upsert_rule_shape(conn, "git", None, "[]", None, now)
        perm_id = _insert_permission_direct(conn, shape_id)
        conn.execute("UPDATE permissions SET hit_count = 42 WHERE id = ?;", (perm_id,))

        from nephoscope.cli.permissions_cmd import _cmd_stats
        import io
        import sys

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            args = argparse.Namespace(show_unused=False)
            _cmd_stats(args)
        finally:
            sys.stdout = old_stdout

        output = captured.getvalue()
        assert "42" in output, (
            f"Expected hit count '42' in stats output; got: {output!r}"
        )

    def test_stats_never_used_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stats subcommand counts rules with 0 hits as never-used."""
        conn = _make_db(tmp_path, monkeypatch)
        now = _now()
        for verb in ("git", "rm", "ls"):
            shape_id = upsert_rule_shape(conn, verb, None, "[]", None, now)
            _insert_permission_direct(conn, shape_id)
        # Only bump one
        conn.execute("UPDATE permissions SET hit_count = 5 WHERE id = 1;")

        from nephoscope.cli.permissions_cmd import _cmd_stats
        import io
        import sys

        captured = io.StringIO()
        sys.stdout = captured
        try:
            args = argparse.Namespace(show_unused=False)
            _cmd_stats(args)
        finally:
            sys.stdout = sys.__stdout__

        output = captured.getvalue()
        # 2 of 3 rules have 0 hits
        assert "2" in output, (
            f"Expected never-used count '2' in output; got: {output!r}"
        )
