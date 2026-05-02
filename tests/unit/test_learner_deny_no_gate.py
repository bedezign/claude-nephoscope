"""Tests that deny promotion bypasses the DANGER gate.

When a user explicitly rejects (decision='rejected') a command pattern that
would trigger a DANGER finding on promotion, the gate should not block — a
deny rule for a dangerous pattern is the correct defensive posture.

The `if decision != "rejected":` guard in learner.py:_cmd_write_permission
implements this bypass. These tests will fail if that guard is ever removed.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

import nephoscope.learners.permission.learner as _learner_mod
from nephoscope.learners.permission.evaluate import Finding
from nephoscope.learners.permission.learner import _cmd_write_permission

_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent.parent / "src/nephoscope/lib/schema.sql"
)


def _make_db(db_path: Path) -> int:
    """Bootstrap a minimal observations DB at db_path and return a sessions row id."""
    conn = sqlite3.connect(str(db_path))
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    cur = conn.execute(
        "INSERT INTO sessions(session_uuid, started_at, last_activity)"
        " VALUES ('deny-gate-test-session', '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')"
    )
    session_id = int(cur.lastrowid or 0)
    conn.close()
    return session_id


def _danger_finding() -> Finding:
    return Finding(
        severity="DANGER",
        code="transparent_wrapper_wildcard",
        message='flags: "*" on env with no subcommand — approves everything.',
        guide_anchor="docs/auto-approve-evaluation-guide.md#transparent-wrappers",
    )


def _make_args(
    *,
    verb: str,
    session_id: int,
    decision: str,
    flags: str = "*",
    subcommand: str | None = None,
    path_spec: str | None = None,
    accept_dangerous: str | None = None,
) -> argparse.Namespace:
    """Build a minimal args Namespace suitable for _cmd_write_permission."""
    return argparse.Namespace(
        verb=verb,
        subcommand=subcommand,
        flags=flags,
        path_spec=path_spec,
        tier="session",
        session_id=session_id,
        project_id=None,
        reason=None,
        accept_dangerous=accept_dangerous,
    )


class TestDenyPromotionBypassesDangerGate:
    def test_reject_danger_verb_succeeds_without_accept_dangerous(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Rejecting a DANGER-triggering verb does not require --accept-dangerous.

        The DANGER gate in _cmd_write_permission is appropriate for 'approve'
        decisions: blindly approving e.g. 'env *' would let the AI run any
        command it likes. But for 'rejected' decisions the pattern is being
        explicitly blocked — denying a dangerous pattern is a safe, defensive
        action and the gate must not block it.

        This test is RED until the source fix makes _cmd_write_permission skip
        (or ignore) DANGER findings when decision='rejected'. The test patches
        evaluate_safety to always return a DANGER finding so it is independent
        of whatever form the wildcard sentinel takes in the internal API.
        """
        db_path = tmp_path / "obs.db"
        session_id = _make_db(db_path)
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        # Inject a DANGER finding unconditionally so the gate path is exercised
        # regardless of how flags_json is serialized internally.
        monkeypatch.setattr(
            _learner_mod, "evaluate_safety", lambda *_a, **_kw: [_danger_finding()]
        )

        args = _make_args(verb="env", session_id=session_id, decision="rejected")
        rc = _cmd_write_permission(args, "rejected")

        assert rc == 0, (
            "Expected deny promotion to succeed (rc=0) without --accept-dangerous, "
            "but the DANGER gate blocked it (rc=1). This test is RED until the fix "
            "makes _cmd_write_permission skip DANGER findings for rejected decisions."
        )

    def test_promote_danger_verb_still_blocked_without_accept_dangerous(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Promoting a DANGER-triggering pattern still requires --accept-dangerous.

        The gate bypass must only apply to 'rejected' decisions. Promoting a
        DANGER-triggering pattern without --accept-dangerous must still return 1.
        This test should be GREEN both before and after the deny-gate fix.
        """
        db_path = tmp_path / "obs_promote.db"
        session_id = _make_db(db_path)
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        monkeypatch.setattr(
            _learner_mod, "evaluate_safety", lambda *_a, **_kw: [_danger_finding()]
        )

        args = _make_args(verb="env", session_id=session_id, decision="approved")
        rc = _cmd_write_permission(args, "approved")

        assert rc == 1, (
            "Expected promote on a DANGER verb to be blocked (rc=1) when "
            "--accept-dangerous is absent, but it succeeded (rc=0). "
            "The DANGER gate must only be bypassed for rejected decisions."
        )

    def test_reject_non_danger_verb_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Rejecting a non-DANGER verb succeeds regardless of the deny-gate fix.

        Sanity check: GREEN both before and after the fix.
        """
        db_path = tmp_path / "obs_clean.db"
        session_id = _make_db(db_path)
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        args = _make_args(
            verb="ls", session_id=session_id, decision="rejected", flags="[]"
        )
        rc = _cmd_write_permission(args, "rejected")

        assert rc == 0
