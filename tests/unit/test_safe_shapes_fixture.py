"""Tests for the safe_shapes.yaml Phase 2 extension.

Verifies that the curated set of read/inspect shell verbs is present in the
fixture and that the sed -i rejection entry overrides any bare sed approval
when matched through the Bash matcher.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nephoscope.learners.permission.seed import apply_fixtures
from nephoscope.learners.permission.match import dispatch, Verdict

FIXTURE_PATH = Path(__file__).resolve().parents[2] / (
    "src/nephoscope/learners/permission/config/fixtures/safe_shapes.yaml"
)

ALL_PLANNED_VERBS = [
    "awk",
    "cat",
    "cut",
    "diff",
    "echo",
    "file",
    "find",
    "grep",
    "head",
    "ln",
    "ls",
    "mkdir",
    "readlink",
    "sort",
    "stat",
    "tail",
    "tr",
    "wc",
    "which",
    "jq",
    "column",
    "uniq",
    "paste",
    "join",
    "comm",
    "od",
    "xxd",
    "du",
    "df",
    "ldd",
    "nm",
    "lsof",
    "strings",
    "ps",
    "pgrep",
    "pstree",
    "pidof",
    "uname",
    "hostname",
    "date",
    "uptime",
    "id",
    "whoami",
    "env",
    "printenv",
    "locale",
    "nproc",
    "pwd",
    "tty",
    "realpath",
    "test",
    "true",
    "false",
    "printf",
    "type",
    "sqlite3",
    "sha256sum",
    "sha1sum",
    "md5sum",
    "sha512sum",
    "b2sum",
    "cksum",
]

SPOT_CHECK_VERBS = [
    "cat",
    "ls",
    "find",
    "jq",
    "sqlite3",
    "sha256sum",
    "ps",
    "env",
    "date",
    "realpath",
]


def _approved_verbs(conn) -> set[str]:
    """Return the set of verbs with an approved permission in the DB."""
    rows = conn.execute(
        """
        SELECT rs.verb
          FROM rule_shapes rs
          JOIN permissions p ON p.rule_shape_id = rs.id
         WHERE p.decision = 'approved'
        """
    ).fetchall()
    return {row[0] for row in rows}


class TestSafeShapesFixtureLoads:
    """Smoke test: the fixture file applies cleanly to a fresh DB."""

    def test_fixture_loads_without_error(self, tmp_db):
        """apply_fixtures on safe_shapes.yaml must not raise."""
        apply_fixtures(tmp_db, FIXTURE_PATH)


class TestSpotCheckVerbs:
    """Ten representative verbs must appear as approved entries."""

    @pytest.fixture(autouse=True)
    def _load(self, tmp_db):
        apply_fixtures(tmp_db, FIXTURE_PATH)
        self._conn = tmp_db

    @pytest.mark.parametrize("verb", SPOT_CHECK_VERBS)
    def test_spot_check_verb_approved(self, verb):
        """Verb from spot-check list must have an approved permission entry."""
        approved = _approved_verbs(self._conn)
        assert verb in approved, (
            f"verb {verb!r} not found in approved entries; "
            f"known approved verbs: {sorted(approved)}"
        )


class TestAllPlannedVerbs:
    """Every verb in the ~60-verb planned list must have an approved entry."""

    @pytest.fixture(autouse=True)
    def _load(self, tmp_db):
        apply_fixtures(tmp_db, FIXTURE_PATH)
        self._conn = tmp_db

    @pytest.mark.parametrize("verb", ALL_PLANNED_VERBS)
    def test_planned_verb_approved(self, verb):
        """Each planned verb must appear as an approved permission entry."""
        approved = _approved_verbs(self._conn)
        assert verb in approved, (
            f"verb {verb!r} not found in approved entries after fixture load"
        )


class TestSedIRejection:
    """sed -i must have a rejected permission entry."""

    def test_sed_i_has_rejected_entry(self, tmp_db):
        """A rule_shape for sed with flags=["-i"] must exist with decision=rejected."""
        apply_fixtures(tmp_db, FIXTURE_PATH)

        flags_json = json.dumps(["-i"])
        row = tmp_db.execute(
            """
            SELECT p.decision
              FROM rule_shapes rs
              JOIN permissions p ON p.rule_shape_id = rs.id
             WHERE rs.verb = 'sed'
               AND rs.flags = ?
               AND p.decision = 'rejected'
            """,
            (flags_json,),
        ).fetchone()

        assert row is not None, (
            "No rejected permission found for verb='sed' flags=['-i']; "
            "the sed -i rejection entry is missing from the fixture"
        )
        assert row[0] == "rejected"


class TestSedIPriority:
    """sed -i beats any bare sed approved entry via the Bash matcher."""

    def test_sed_i_returns_deny_when_bare_sed_also_approved(self, tmp_db):
        """Matching 'sed -i file' returns Deny even when sed (no flags) is approved.

        The test inserts a bare sed approval first, then loads the fixture
        (which adds the sed -i rejection).  The Bash matcher must return Deny
        because the more-specific flags variant matches the rejection first.
        """
        import yaml

        from nephoscope.learners.permission.seed import apply_fixtures as _af

        # Insert a bare sed approval (no flags) via a minimal temp fixture.
        import tempfile
        import os

        bare_sed_yaml = yaml.dump(
            [
                {
                    "verb": "sed",
                    "flags": [],
                    "decision": "approved",
                    "reason": "bare sed for priority test",
                },
            ]
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(bare_sed_yaml)
            tmp_fixture = f.name

        try:
            _af(tmp_db, tmp_fixture)
        finally:
            os.unlink(tmp_fixture)

        # Now load the full safe_shapes.yaml which adds the sed -i rejection.
        apply_fixtures(tmp_db, FIXTURE_PATH)

        # Match 'sed -i /tmp/testfile' through the real Bash dispatcher.
        verdict, _ = dispatch(
            tool_name="Bash",
            tool_input={"command": "sed -i s/foo/bar/ /tmp/testfile"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
            cwd=None,
        )

        assert verdict == Verdict.Deny, (
            f'Expected Verdict.Deny for "sed -i ..." but got {verdict!r}; '
            "the sed -i rejection entry is not overriding the bare sed approval"
        )
