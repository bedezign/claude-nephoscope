"""Unit tests for learners.permission.match.file — uncovered branches.

Covers helper function edge cases and the match() function paths not
exercised by the broader matcher-dispatch integration tests:

- _glob_match: empty pattern/path early return, exception swallowing
- _path_spec_matches: no file_path when path_spec is a glob
- match(): non-dict tool_input → empty file_path
- match(): shape row with no matching permissions → continue loop
- match(): Deny verdict from a rejected permission
"""

from __future__ import annotations

import sqlite3


from nephoscope.learners.permission.match.file import (
    _glob_match,
    _path_spec_matches,
    match,
)
from nephoscope.learners.permission.match._types import Verdict


# ---------------------------------------------------------------------------
# Helpers (mirrors test_matcher_dispatch.py pattern)
# ---------------------------------------------------------------------------


def _insert_rule_shape(
    conn: sqlite3.Connection,
    verb: str,
    path_spec: str | None = None,
) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO rule_shapes"
        " (verb, subcommand, flags, path_spec, first_seen, last_seen)"
        " VALUES (?, NULL, '[]', ?, '2025-01-01Z', '2025-01-01Z');",
        (verb, path_spec),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM rule_shapes WHERE verb=? AND IFNULL(path_spec,'')=IFNULL(?,'');",
        (verb, path_spec),
    ).fetchone()
    return int(row[0]) if row else 0


def _insert_permission(
    conn: sqlite3.Connection,
    rule_shape_id: int,
    decision: str,
    session_id: int | None = None,
    project_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO permissions"
        " (rule_shape_id, session_id, project_id, decision, source, decided_at)"
        " VALUES (?, ?, ?, ?, 'seed', '2025-01-01Z');",
        (rule_shape_id, session_id, project_id, decision),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


_CTX: dict[str, str] = {
    "home": "/home/user",
    "cwd": "/home/user/project",
    "project_root": "/home/user/project",
}


# ---------------------------------------------------------------------------
# _glob_match — edge cases
# ---------------------------------------------------------------------------


class TestGlobMatch:
    def test_empty_pattern_returns_false(self):
        assert _glob_match("", "/some/path") is False

    def test_empty_path_returns_false(self):
        assert _glob_match("/some/**", "") is False

    def test_both_empty_returns_false(self):
        assert _glob_match("", "") is False

    def test_exception_in_posixpath_match_falls_back_to_fnmatch(self):
        # Feed a pattern that causes PurePosixPath.match to raise; the
        # function must catch the exception and fall back to fnmatch.
        from unittest import mock
        from pathlib import PurePosixPath

        with mock.patch.object(PurePosixPath, "match", side_effect=ValueError("boom")):
            # With fallback fnmatch: /home/user/file.txt matches /home/user/*
            result = _glob_match("/home/user/*", "/home/user/file.txt")
        # fnmatch fallback: pattern collapses to /home/user/* which does match.
        assert result is True

    def test_normal_glob_matches(self):
        assert _glob_match("/home/user/*.py", "/home/user/script.py") is True

    def test_double_star_glob_matches_nested(self):
        assert _glob_match("/home/**/*.py", "/home/user/deep/script.py") is True

    def test_double_slash_prefix_matches_nested_path(self):
        # Claude Code emits absolute patterns with a leading //; they must match
        # as if the extra slash were absent.
        assert (
            _glob_match("//home/steve/.claude/**", "/home/steve/.claude/foo.md") is True
        )

    def test_double_slash_prefix_matches_deep_client_path(self):
        assert (
            _glob_match(
                "//home/steve/data/clients/**",
                "/home/steve/data/clients/bedezign/x.py",
            )
            is True
        )

    def test_double_slash_prefix_does_not_match_unrelated_path(self):
        # The // normalisation must not introduce false positives.
        assert (
            _glob_match("//home/steve/.claude/**", "/home/steve/other/foo.md") is False
        )

    def test_single_slash_prefix_baseline_unaffected(self):
        # The single-slash form must continue to work after the fix.
        assert (
            _glob_match("/home/steve/.claude/**", "/home/steve/.claude/foo.md") is True
        )

    def test_double_slash_prefix_matches_directory_itself(self):
        # The directory path (trailing slash dropped by PurePosixPath) must match.
        assert _glob_match("//home/steve/.claude/**", "/home/steve/.claude/") is True

    def test_double_slash_exact_match_no_glob(self):
        # A double-slash path_spec with no wildcard must match the exact path.
        assert (
            _glob_match("//home/steve/.claude/foo.md", "/home/steve/.claude/foo.md")
            is True
        )

    def test_double_slash_exact_match_does_not_match_different_file(self):
        # Exact path_spec must not match a sibling file.
        assert (
            _glob_match("//home/steve/.claude/foo.md", "/home/steve/.claude/bar.md")
            is False
        )


# ---------------------------------------------------------------------------
# _path_spec_matches — no file_path with glob pattern
# ---------------------------------------------------------------------------


class TestPathSpecMatches:
    def test_none_path_spec_matches_any_path(self):
        assert _path_spec_matches(None, "/any/path", _CTX) is True

    def test_none_path_spec_matches_empty_path(self):
        assert _path_spec_matches(None, "", _CTX) is True

    def test_empty_path_spec_matches_empty_file_path(self):
        assert _path_spec_matches("", "", _CTX) is True

    def test_empty_path_spec_does_not_match_nonempty_file_path(self):
        assert _path_spec_matches("", "/some/file", _CTX) is False

    def test_glob_path_spec_with_empty_file_path_returns_false(self):
        # When path_spec is a glob but file_path is empty, should return False.
        assert _path_spec_matches("/home/**", "", _CTX) is False

    def test_glob_path_spec_matches_file(self):
        assert (
            _path_spec_matches("/home/user/*.py", "/home/user/script.py", _CTX) is True
        )

    def test_double_slash_path_spec_full_call_chain(self):
        # Verify that a //-prefixed path_spec with no $VAR tokens reaches
        # _glob_match correctly and resolves to True end-to-end.
        assert (
            _path_spec_matches(
                "//home/steve/.claude/**",
                "/home/steve/.claude/settings.json",
                ctx={},
            )
            is True
        )

    def test_double_slash_exact_no_glob_matches_target_file(self):
        # path_spec is an exact path (no wildcard) with double-slash prefix;
        # must match only the corresponding single-slash real path.
        assert (
            _path_spec_matches(
                "//home/steve/.claude/foo.md",
                "/home/steve/.claude/foo.md",
                ctx={},
            )
            is True
        )

    def test_double_slash_exact_no_glob_does_not_match_nested_path(self):
        # An exact path_spec must NOT match a sub-path beneath that location.
        assert (
            _path_spec_matches(
                "//home/steve/.claude/foo.md",
                "/home/steve/.claude/rules/something.md",
                ctx={},
            )
            is False
        )


# ---------------------------------------------------------------------------
# match() — non-dict tool_input
# ---------------------------------------------------------------------------


class TestFileMatchNonDictInput:
    def test_non_dict_tool_input_treated_as_empty_file_path(self, tmp_db):
        # When tool_input is a string (not a dict), file_path defaults to ''
        # and a NULL path_spec rule should still match.
        shape_id = _insert_rule_shape(tmp_db, "Read", path_spec=None)
        _insert_permission(tmp_db, shape_id, "approved")

        result, _ = match(
            tool_name="Read",
            tool_input="not_a_dict",  # type: ignore[arg-type]
            conn=tmp_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
        )
        assert result == Verdict.Allow

    def test_non_str_file_path_in_dict_defaults_to_empty(self, tmp_db):
        # tool_input dict with a non-str file_path value.
        shape_id = _insert_rule_shape(tmp_db, "Read", path_spec=None)
        _insert_permission(tmp_db, shape_id, "approved")

        result, _ = match(
            tool_name="Read",
            tool_input={"file_path": 12345},  # not a string
            conn=tmp_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
        )
        assert result == Verdict.Allow


# ---------------------------------------------------------------------------
# match() — shape row present but no permissions → skip to next shape
# ---------------------------------------------------------------------------


class TestFileMatchNoPermissions:
    def test_shape_without_permission_row_is_skipped(self, tmp_db):
        # A rule_shape row exists but has no matching permission → NoOpinion.
        _insert_rule_shape(tmp_db, "Read", path_spec=None)
        # No permission inserted.

        result, _ = match(
            tool_name="Read",
            tool_input={"file_path": "/home/user/file.txt"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
        )
        assert result == Verdict.NoOpinion

    def test_second_shape_matched_after_first_skipped(self, tmp_db):
        # First shape has no permissions; second shape has a permission.
        # Verifies the continue branch is hit and loop proceeds.
        _insert_rule_shape(tmp_db, "Read", path_spec="/no/match/*")
        shape2 = _insert_rule_shape(tmp_db, "Read", path_spec=None)
        _insert_permission(tmp_db, shape2, "approved")

        result, _ = match(
            tool_name="Read",
            tool_input={"file_path": "/home/user/file.txt"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
        )
        assert result == Verdict.Allow


# ---------------------------------------------------------------------------
# match() — Deny verdict
# ---------------------------------------------------------------------------


class TestFileMatchDenyVerdict:
    def test_rejected_permission_returns_deny(self, tmp_db):
        shape_id = _insert_rule_shape(tmp_db, "Write", path_spec=None)
        _insert_permission(tmp_db, shape_id, "rejected")

        result, _ = match(
            tool_name="Write",
            tool_input={"file_path": "/home/user/output.txt"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
        )
        assert result == Verdict.Deny


# ---------------------------------------------------------------------------
# match() — no rows → NoOpinion
# ---------------------------------------------------------------------------


class TestFileMatchNoRows:
    def test_no_rule_shapes_returns_noopinion(self, tmp_db):
        result, _ = match(
            tool_name="Read",
            tool_input={"file_path": "/home/user/file.txt"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
            ctx=_CTX,
        )
        assert result == Verdict.NoOpinion
