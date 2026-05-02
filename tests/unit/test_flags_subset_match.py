"""Unit tests for the subset-semantics flags matching in match/bash.py.

Seeds a minimal in-memory rule_shapes table and verifies that
_lookup_rule_shape_id returns the correct (most-specific) match.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nephoscope.learners.permission.match.bash import _lookup_rule_shape_id
from nephoscope.learners.permission.canonicalize import PatternVariant


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCHEMA = (
    Path(__file__).resolve().parent.parent.parent
    / "src"
    / "nephoscope"
    / "lib"
    / "schema.sql"
)


def _flags(flags: list[str]) -> str:
    return json.dumps(sorted(flags), separators=(",", ":"))


def _variant(
    verb: str, flags: str, subcommand: str | None = None, path_spec: str | None = None
) -> PatternVariant:
    return PatternVariant(
        verb=verb,
        subcommand=subcommand,
        flags=flags,
        path_spec=path_spec,
        context="toplevel",
    )


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(_SCHEMA.read_text(encoding="utf-8"))
    return c


def _insert_shape(
    conn: sqlite3.Connection, verb: str, flags: str, shape_id: int | None = None
) -> int:
    if shape_id is not None:
        conn.execute(
            "INSERT INTO rule_shapes(id, verb, subcommand, flags, path_spec, context, tool, first_seen, last_seen)"
            " VALUES (?,?,NULL,?,NULL,'any','Bash','2025-01-01','2025-01-01')",
            (shape_id, verb, flags),
        )
        return shape_id
    conn.execute(
        "INSERT INTO rule_shapes(verb, subcommand, flags, path_spec, context, tool, first_seen, last_seen)"
        " VALUES (?,NULL,?,NULL,'any','Bash','2025-01-01','2025-01-01')",
        (verb, flags),
    )
    row = conn.execute("SELECT last_insert_rowid()").fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# Basic subset semantics
# ---------------------------------------------------------------------------


class TestExactMatch:
    def test_exact_flags_match(self, conn: sqlite3.Connection) -> None:
        sid = _insert_shape(conn, "rm", _flags(["-r", "-f"]))
        result = _lookup_rule_shape_id(conn, _variant("rm", _flags(["-r", "-f"])))
        assert result == sid

    def test_no_match_for_different_verb(self, conn: sqlite3.Connection) -> None:
        _insert_shape(conn, "rm", _flags(["-r", "-f"]))
        result = _lookup_rule_shape_id(conn, _variant("ls", _flags(["-r", "-f"])))
        assert result is None

    def test_empty_flags_matches_empty_rule(self, conn: sqlite3.Connection) -> None:
        sid = _insert_shape(conn, "git", _flags([]))
        result = _lookup_rule_shape_id(conn, _variant("git", _flags([])))
        assert result == sid


class TestSubsetSemantics:
    def test_subset_of_rule_flags_matches(self, conn: sqlite3.Connection) -> None:
        """Invocation using only -r matches rule with [-r, -f]."""
        sid = _insert_shape(conn, "rm", _flags(["-r", "-f"]))
        result = _lookup_rule_shape_id(conn, _variant("rm", _flags(["-r"])))
        assert result == sid

    def test_subset_empty_matches_any_rule(self, conn: sqlite3.Connection) -> None:
        """Invocation with no flags matches rule with flag allowlist."""
        sid = _insert_shape(conn, "rm", _flags(["-r", "-f"]))
        result = _lookup_rule_shape_id(conn, _variant("rm", _flags([])))
        assert result == sid

    def test_superset_does_not_match(self, conn: sqlite3.Connection) -> None:
        """Invocation with extra flags not in rule is rejected."""
        _insert_shape(conn, "rm", _flags(["-r"]))
        result = _lookup_rule_shape_id(conn, _variant("rm", _flags(["-r", "-f"])))
        assert result is None


class TestMostSpecificWins:
    def test_prefers_exact_over_superset(self, conn: sqlite3.Connection) -> None:
        """When both [-r,-f] and [-r,-f,-v] match, the tighter rule wins."""
        sid_tight = _insert_shape(conn, "rm", _flags(["-r", "-f"]))
        _insert_shape(conn, "rm", _flags(["-r", "-f", "-v"]))
        result = _lookup_rule_shape_id(conn, _variant("rm", _flags(["-r", "-f"])))
        assert result == sid_tight

    def test_prefers_smallest_superset(self, conn: sqlite3.Connection) -> None:
        """Among two matching rules pick the one with fewer extra flags."""
        sid_tight = _insert_shape(conn, "rm", _flags(["-r", "-f", "-i"]))
        _insert_shape(conn, "rm", _flags(["-r", "-f", "-i", "-v", "-n"]))
        result = _lookup_rule_shape_id(conn, _variant("rm", _flags(["-r", "-f"])))
        assert result == sid_tight


class TestWildcardRule:
    def test_wildcard_rule_matches_any_flags(self, conn: sqlite3.Connection) -> None:
        sid = _insert_shape(conn, "ls", "*")
        result = _lookup_rule_shape_id(conn, _variant("ls", _flags(["-l", "-a", "-h"])))
        assert result == sid

    def test_wildcard_rule_matches_empty_flags(self, conn: sqlite3.Connection) -> None:
        sid = _insert_shape(conn, "ls", "*")
        result = _lookup_rule_shape_id(conn, _variant("ls", _flags([])))
        assert result == sid

    def test_specific_rule_preferred_over_wildcard(
        self, conn: sqlite3.Connection
    ) -> None:
        sid_specific = _insert_shape(conn, "ls", _flags(["-l", "-a"]))
        _insert_shape(conn, "ls", "*")
        result = _lookup_rule_shape_id(conn, _variant("ls", _flags(["-l", "-a"])))
        assert result == sid_specific

    def test_wildcard_fallback_when_no_specific_matches(
        self, conn: sqlite3.Connection
    ) -> None:
        _insert_shape(conn, "ls", _flags(["-l"]))
        sid_wild = _insert_shape(conn, "ls", "*")
        # -l -a -h → not a subset of [-l], falls back to wildcard
        result = _lookup_rule_shape_id(conn, _variant("ls", _flags(["-l", "-a", "-h"])))
        assert result == sid_wild


class TestWildcardLookupVariant:
    def test_wildcard_variant_finds_wildcard_rule(
        self, conn: sqlite3.Connection
    ) -> None:
        """variant.flags='*' only matches rules stored with flags='*'."""
        sid = _insert_shape(conn, "ls", "*")
        result = _lookup_rule_shape_id(conn, _variant("ls", "*"))
        assert result == sid

    def test_wildcard_variant_ignores_specific_rule(
        self, conn: sqlite3.Connection
    ) -> None:
        """variant.flags='*' does not match specific flag rules."""
        _insert_shape(conn, "ls", _flags(["-l"]))
        result = _lookup_rule_shape_id(conn, _variant("ls", "*"))
        assert result is None


class TestContextFilter:
    def test_any_context_matches_toplevel(self, conn: sqlite3.Connection) -> None:
        sid = _insert_shape(conn, "git", _flags([]))
        result = _lookup_rule_shape_id(conn, _variant("git", _flags([])))
        assert result == sid

    def test_no_match_wrong_context(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO rule_shapes(verb, subcommand, flags, path_spec, context, tool, first_seen, last_seen)"
            " VALUES ('git', NULL, '[]', NULL, 'substitution', 'Bash', '2025-01-01', '2025-01-01')"
        )
        result = _lookup_rule_shape_id(conn, _variant("git", _flags([])))
        assert result is None
