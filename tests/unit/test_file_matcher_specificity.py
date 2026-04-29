"""Unit and integration tests for specificity-first conflict resolution in match/file.py.

Specificity metric: fewer wildcard-containing path components = more specific.
On wildcard-count tie, deny-on-tie.
None path_spec (any-path) = least specific (sys.maxsize wildcards).
"""

from __future__ import annotations

import sys


from nephoscope.learners.permission.match.file import _wildcard_count, match
from nephoscope.learners.permission.match._types import Verdict
from nephoscope.lib.db import insert_permission, upsert_rule_shape


# ---------------------------------------------------------------------------
# _wildcard_count — pure function tests
# ---------------------------------------------------------------------------


class TestWildcardCount:
    def test_none_returns_sys_maxsize(self) -> None:
        assert _wildcard_count(None) == sys.maxsize

    def test_literal_path_no_wildcards_returns_zero(self) -> None:
        assert _wildcard_count("/home/user/.env") == 0

    def test_single_double_star_component_returns_one(self) -> None:
        assert _wildcard_count("$TRUSTED_DIR/**") == 1

    def test_two_wildcard_components_returns_two(self) -> None:
        assert _wildcard_count("**/*.env") == 2

    def test_trusted_dir_exact_file_returns_zero(self) -> None:
        assert _wildcard_count("$TRUSTED_DIR/.env") == 0

    def test_trusted_dir_with_wildcard_subdir_returns_one(self) -> None:
        assert _wildcard_count("$TRUSTED_DIR/**/.env") == 1


# ---------------------------------------------------------------------------
# match() — specificity-first conflict resolution (integration via tmp_db)
# ---------------------------------------------------------------------------


def _seed(
    conn, path_spec: str, decision: str, ts: str = "2024-01-01T00:00:00Z"
) -> None:
    """Seed a global Read permission rule with the given path_spec and decision."""
    shape_id = upsert_rule_shape(conn, "Read", None, "[]", path_spec, ts)
    insert_permission(conn, shape_id, None, None, decision, "seed", ts)
    conn.commit()


class TestSpecificityResolution:
    """match() applies specificity-first: most specific path_spec wins.
    Deny-on-tie when wildcard counts are equal and decisions conflict.
    """

    def test_narrow_allow_beats_broad_deny(self, tmp_db) -> None:
        """0-wildcard Allow wins over 1-wildcard Deny → Verdict.Allow."""
        _seed(tmp_db, "$TRUSTED_DIR/**", "rejected")  # wildcard count 1
        _seed(tmp_db, "$TRUSTED_DIR/.env", "approved")  # wildcard count 0

        verdict = match(
            tool_name="Read",
            tool_input={"file_path": "/tmp/trusted/.env"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
            ctx={},
            trusted_dirs=["/tmp/trusted"],
        )
        assert verdict == Verdict.Allow

    def test_narrow_deny_beats_broad_allow(self, tmp_db) -> None:
        """0-wildcard Deny wins over 1-wildcard Allow → Verdict.Deny."""
        _seed(tmp_db, "$TRUSTED_DIR/**", "approved")  # wildcard count 1
        _seed(tmp_db, "$TRUSTED_DIR/.env", "rejected")  # wildcard count 0

        verdict = match(
            tool_name="Read",
            tool_input={"file_path": "/tmp/trusted/.env"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
            ctx={},
            trusted_dirs=["/tmp/trusted"],
        )
        assert verdict == Verdict.Deny

    def test_tie_with_conflicting_decisions_returns_deny(self, tmp_db) -> None:
        """Equal wildcard counts, conflict between Allow and Deny → Verdict.Deny (deny-on-tie)."""
        _seed(tmp_db, "$TRUSTED_DIR/**", "approved")  # wildcard count 1
        _seed(
            tmp_db, "$TRUSTED_DIR/**", "rejected"
        )  # wildcard count 1 (duplicate shape upsert)
        # NOTE: the upsert logic means two rows with same shape key merge; seed a second
        # shape at the same specificity with a different path_spec to create a true tie
        _seed(tmp_db, "/tmp/trusted/**", "approved")  # wildcard count 1
        _seed(tmp_db, "/tmp/trusted/**", "rejected")  # wildcard count 1

        # To create a genuine tie with conflict, use two distinct path_specs at the same wildcard count
        _seed(tmp_db, "$TRUSTED_DIR/sub/**", "rejected")  # wildcard count 1

        verdict = match(
            tool_name="Read",
            tool_input={"file_path": "/tmp/trusted/sub/file.py"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
            ctx={},
            trusted_dirs=["/tmp/trusted"],
        )
        assert verdict == Verdict.Deny

    def test_tie_all_allow_returns_allow(self, tmp_db) -> None:
        """Equal wildcard counts, all Allow → Verdict.Allow."""
        _seed(tmp_db, "$TRUSTED_DIR/**", "approved")  # wildcard count 1
        _seed(tmp_db, "$TRUSTED_DIR/sub/**", "approved")  # wildcard count 1

        verdict = match(
            tool_name="Read",
            tool_input={"file_path": "/tmp/trusted/sub/file.py"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
            ctx={},
            trusted_dirs=["/tmp/trusted"],
        )
        assert verdict == Verdict.Allow

    def test_no_matching_rules_returns_no_opinion(self, tmp_db) -> None:
        """No rules in DB → Verdict.NoOpinion."""
        verdict = match(
            tool_name="Read",
            tool_input={"file_path": "/tmp/trusted/file.py"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
            ctx={},
            trusted_dirs=["/tmp/trusted"],
        )
        assert verdict == Verdict.NoOpinion

    def test_single_allow_rule_returns_allow(self, tmp_db) -> None:
        """Single matching Allow rule → Verdict.Allow."""
        _seed(tmp_db, "$TRUSTED_DIR/**", "approved")

        verdict = match(
            tool_name="Read",
            tool_input={"file_path": "/tmp/trusted/file.py"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
            ctx={},
            trusted_dirs=["/tmp/trusted"],
        )
        assert verdict == Verdict.Allow

    def test_single_deny_rule_returns_deny(self, tmp_db) -> None:
        """Single matching Deny rule → Verdict.Deny."""
        _seed(tmp_db, "$TRUSTED_DIR/**", "rejected")

        verdict = match(
            tool_name="Read",
            tool_input={"file_path": "/tmp/trusted/file.py"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
            ctx={},
            trusted_dirs=["/tmp/trusted"],
        )
        assert verdict == Verdict.Deny
