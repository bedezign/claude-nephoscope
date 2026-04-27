"""Tests for learners.permission.learner — Phase 8 rewrite.

Tests for: scan_candidates, propose_promotions, promote/reject/unpermit flows,
cursor management, deny-filter application at propose time, and the new
per-axis review helpers (pattern-variants, context-ids,
count-concrete-siblings, subsume-siblings).

No references to command_shapes, tool_call_shapes, permission_active,
permission_rejected, permission_session_approvals, scope_id, or tool_call_scopes.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Generator
from unittest import mock

import pytest

# lib.db is importable because conftest.py adds the sandbox root to sys.path.
import nephoscope.lib.db as db
from nephoscope.learners.permission.learner import (
    Candidate,
    _candidate_leaf,
    _describe_rule,
    _get_cursor,
    _parse_flags_arg,
    _resolve_tier_ids,
    _set_cursor,
    _tier_phrase,
    main as learner_main,
    propose_promotions,
    scan_candidates,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_db) -> Generator[sqlite3.Connection, None, None]:
    """Yield the tmp_db connection with all seeded lookups present.

    The tmp_db fixture (from conftest.py) creates an isolated SQLite DB
    with the Phase 8 schema applied and permission_modes / call_statuses
    seeded.  We add the Bash tool row here so scan tests can reference it.
    """
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


def _insert_project(conn: sqlite3.Connection, cwd: str = "/home/test/project") -> int:
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO projects(cwd, name, root, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?)",
        (cwd, "project", cwd, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM projects WHERE cwd=?", (cwd,)).fetchone()
    return int(row[0])


def _insert_session(
    conn: sqlite3.Connection,
    uuid: str,
    project_id: int | None = None,
) -> int:
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO sessions"
        "(session_uuid, project_id, started_at, last_activity)"
        " VALUES (?, ?, ?, ?)",
        (uuid, project_id, now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM sessions WHERE session_uuid=?", (uuid,)
    ).fetchone()
    return int(row[0])


def _insert_tool_call(
    conn: sqlite3.Connection,
    session_id: int | None,
    command: str,
    status: str = "ok",
    mode: str | None = None,
) -> int:
    now = _now()
    bash_tool_id = conn.execute("SELECT id FROM tools WHERE name='Bash'").fetchone()[0]
    status_id = conn.execute(
        "SELECT id FROM call_statuses WHERE name=?", (status,)
    ).fetchone()[0]
    mode_id = None
    if mode is not None:
        row = conn.execute(
            "SELECT id FROM permission_modes WHERE name=?", (mode,)
        ).fetchone()
        mode_id = row[0] if row else None

    cur = conn.execute(
        "INSERT INTO tool_calls"
        "(ts, session_id, tool_id, status_id, permission_mode_id, command)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (now, session_id, bash_tool_id, status_id, mode_id, command),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def _seed_candidate(
    conn: sqlite3.Connection,
    verb: str,
    subcommand: str | None,
    flags_json: str,
    observations: int,
    distinct_sessions: int,
) -> int:
    """Directly insert a permission_candidates row for propose tests."""
    now = _now()
    cur = conn.execute(
        "INSERT INTO permission_candidates"
        "(verb, subcommand, flags, observations, distinct_sessions, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (verb, subcommand, flags_json, observations, distinct_sessions, now, now),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def _seed_global_permission(
    conn: sqlite3.Connection,
    verb: str,
    subcommand: str | None,
    flags_json: str,
    decision: str,
    path_spec: str | None = None,
) -> None:
    """Insert a global-tier permission row for a given (verb, sub, flags) shape."""
    now = _now()
    shape_id = db.upsert_rule_shape(conn, verb, subcommand, flags_json, path_spec, now)
    db.insert_permission(conn, shape_id, None, None, decision, "seed", now)
    conn.commit()


# ===========================================================================
# scan_candidates
# ===========================================================================


class TestScanCandidatesEmpty:
    def test_returns_zero_when_no_rows(self, conn):
        assert scan_candidates(conn) == 0

    def test_cursor_is_zero_initially(self, conn):
        assert _get_cursor(conn) == 0

    def test_cursor_not_advanced_when_nothing_processed(self, conn):
        scan_candidates(conn)
        assert _get_cursor(conn) == 0


class TestScanCandidatesBasic:
    def test_inserts_candidate_for_bash_ok_row(self, conn):
        session_id = _insert_session(conn, "sess-1")
        _insert_tool_call(conn, session_id, "git status")

        processed = scan_candidates(conn)

        assert processed == 1
        row = conn.execute(
            "SELECT verb, subcommand, flags FROM permission_candidates"
        ).fetchone()
        assert row is not None
        assert row[0] == "git"
        assert row[1] == "status"

    def test_cursor_advances_after_scan(self, conn):
        session_id = _insert_session(conn, "sess-1")
        tc_id = _insert_tool_call(conn, session_id, "ls -la")

        scan_candidates(conn)

        assert _get_cursor(conn) == tc_id

    def test_second_scan_does_not_reprocess(self, conn):
        session_id = _insert_session(conn, "sess-1")
        _insert_tool_call(conn, session_id, "git status")
        scan_candidates(conn)

        processed = scan_candidates(conn)

        assert processed == 0
        # Observations still 1.
        obs = conn.execute("SELECT observations FROM permission_candidates").fetchone()[
            0
        ]
        assert obs == 1

    def test_multiple_leaves_from_one_command(self, conn):
        session_id = _insert_session(conn, "sess-1")
        _insert_tool_call(conn, session_id, "git status; ls -la")

        scan_candidates(conn)

        # Two distinct shapes: (git, status, []) and (ls, None, [-a,-l])
        count = conn.execute("SELECT COUNT(*) FROM permission_candidates").fetchone()[0]
        assert count == 2

    def test_same_command_twice_bumps_observations(self, conn):
        session_id = _insert_session(conn, "sess-1")
        _insert_tool_call(conn, session_id, "git status")
        _insert_tool_call(conn, session_id, "git status")

        scan_candidates(conn)

        obs = conn.execute("SELECT observations FROM permission_candidates").fetchone()[
            0
        ]
        assert obs == 2


class TestScanCandidatesDistinctSessions:
    def test_two_sessions_same_command_gives_distinct_sessions_two(self, conn):
        sess1 = _insert_session(conn, "sess-A")
        sess2 = _insert_session(conn, "sess-B")
        _insert_tool_call(conn, sess1, "git status")
        _insert_tool_call(conn, sess2, "git status")

        scan_candidates(conn)

        row = conn.execute(
            "SELECT distinct_sessions FROM permission_candidates WHERE verb='git'"
        ).fetchone()
        assert row[0] == 2

    def test_same_session_twice_does_not_double_count(self, conn):
        sess = _insert_session(conn, "sess-1")
        _insert_tool_call(conn, sess, "git status")
        _insert_tool_call(conn, sess, "git status")

        scan_candidates(conn)

        row = conn.execute(
            "SELECT distinct_sessions FROM permission_candidates WHERE verb='git'"
        ).fetchone()
        assert row[0] == 1

    def test_session_junction_row_created(self, conn):
        sess = _insert_session(conn, "sess-1")
        _insert_tool_call(conn, sess, "git status")

        scan_candidates(conn)

        cand_id = conn.execute(
            "SELECT id FROM permission_candidates WHERE verb='git'"
        ).fetchone()[0]
        row = conn.execute(
            "SELECT session_id FROM permission_candidate_sessions WHERE candidate_id=?",
            (cand_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == sess


class TestScanCandidatesFiltering:
    def test_skips_bypass_permissions_mode(self, conn):
        session_id = _insert_session(conn, "sess-1")
        _insert_tool_call(conn, session_id, "git status", mode="bypassPermissions")

        scan_candidates(conn)

        count = conn.execute("SELECT COUNT(*) FROM permission_candidates").fetchone()[0]
        assert count == 0

    def test_skips_auto_mode(self, conn):
        session_id = _insert_session(conn, "sess-1")
        _insert_tool_call(conn, session_id, "git status", mode="auto")

        scan_candidates(conn)

        count = conn.execute("SELECT COUNT(*) FROM permission_candidates").fetchone()[0]
        assert count == 0

    def test_includes_default_permission_mode(self, conn):
        session_id = _insert_session(conn, "sess-1")
        _insert_tool_call(conn, session_id, "git status", mode="default")

        scan_candidates(conn)

        count = conn.execute("SELECT COUNT(*) FROM permission_candidates").fetchone()[0]
        assert count == 1

    def test_includes_null_permission_mode(self, conn):
        session_id = _insert_session(conn, "sess-1")
        _insert_tool_call(conn, session_id, "git status", mode=None)

        scan_candidates(conn)

        count = conn.execute("SELECT COUNT(*) FROM permission_candidates").fetchone()[0]
        assert count == 1

    def test_skips_non_ok_status(self, conn):
        session_id = _insert_session(conn, "sess-1")
        _insert_tool_call(conn, session_id, "git status", status="err")

        scan_candidates(conn)

        count = conn.execute("SELECT COUNT(*) FROM permission_candidates").fetchone()[0]
        assert count == 0

    def test_skips_null_session_id(self, conn):
        # NULL session_id — cannot track distinct sessions; row is skipped.
        _insert_tool_call(conn, None, "git status")

        scan_candidates(conn)

        count = conn.execute("SELECT COUNT(*) FROM permission_candidates").fetchone()[0]
        assert count == 0

    def test_cursor_still_advances_past_null_session_row(self, conn):
        tc_id = _insert_tool_call(conn, None, "git status")

        scan_candidates(conn)

        # Cursor advances so we don't keep reprocessing null-session rows.
        assert _get_cursor(conn) == tc_id

    def test_unparseable_command_is_skipped_gracefully(self, conn):
        session_id = _insert_session(conn, "sess-1")
        _insert_tool_call(conn, session_id, "malformed (((")

        # Must not raise.
        processed = scan_candidates(conn)
        assert processed == 1
        count = conn.execute("SELECT COUNT(*) FROM permission_candidates").fetchone()[0]
        assert count == 0

    def test_empty_command_is_skipped(self, conn):
        session_id = _insert_session(conn, "sess-1")
        _insert_tool_call(conn, session_id, "")

        processed = scan_candidates(conn)
        # Processed but no candidate.
        assert processed == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM permission_candidates").fetchone()[0]
            == 0
        )


# ===========================================================================
# propose_promotions
# ===========================================================================


class TestProposeEmpty:
    def test_returns_empty_when_no_candidates(self, conn):
        assert propose_promotions(conn) == []


class TestProposeThresholds:
    def test_candidate_meeting_both_thresholds_is_proposed(self, conn):
        _seed_candidate(
            conn, "git", "status", "[]", observations=5, distinct_sessions=2
        )

        proposals = propose_promotions(conn)

        assert len(proposals) == 1
        assert proposals[0].verb == "git"
        assert proposals[0].subcommand == "status"

    def test_below_min_observations_excluded(self, conn):
        # min_observations=5; seed with 4.
        _seed_candidate(
            conn, "git", "status", "[]", observations=4, distinct_sessions=2
        )

        assert propose_promotions(conn) == []

    def test_below_min_distinct_sessions_excluded(self, conn):
        # min_distinct_sessions=2; seed with 1.
        _seed_candidate(
            conn, "git", "status", "[]", observations=5, distinct_sessions=1
        )

        assert propose_promotions(conn) == []

    def test_exactly_at_thresholds_is_included(self, conn):
        _seed_candidate(
            conn, "git", "status", "[]", observations=5, distinct_sessions=2
        )

        proposals = propose_promotions(conn)
        assert len(proposals) == 1

    def test_candidate_fields_populated_correctly(self, conn):
        _seed_candidate(
            conn, "git", "commit", '["-m"]', observations=7, distinct_sessions=3
        )

        proposals = propose_promotions(conn)
        assert len(proposals) == 1
        c = proposals[0]
        assert c.verb == "git"
        assert c.subcommand == "commit"
        assert c.flags == frozenset({"-m"})
        assert c.observations == 7
        assert c.distinct_sessions == 3

    def test_returns_candidate_dataclass_instances(self, conn):
        _seed_candidate(conn, "ls", None, "[]", observations=5, distinct_sessions=2)

        proposals = propose_promotions(conn)
        assert all(isinstance(c, Candidate) for c in proposals)

    def test_ordered_by_observations_descending(self, conn):
        _seed_candidate(
            conn, "git", "status", "[]", observations=10, distinct_sessions=2
        )
        _seed_candidate(conn, "ls", None, "[]", observations=5, distinct_sessions=2)

        proposals = propose_promotions(conn)
        obs_values = [c.observations for c in proposals]
        assert obs_values == sorted(obs_values, reverse=True)


class TestProposeAlreadyPermitted:
    def test_already_globally_approved_is_excluded(self, conn):
        _seed_candidate(
            conn, "git", "status", "[]", observations=5, distinct_sessions=2
        )
        _seed_global_permission(conn, "git", "status", "[]", "approved")

        assert propose_promotions(conn) == []

    def test_already_globally_rejected_is_excluded(self, conn):
        _seed_candidate(
            conn, "git", "status", "[]", observations=5, distinct_sessions=2
        )
        _seed_global_permission(conn, "git", "status", "[]", "rejected")

        assert propose_promotions(conn) == []

    def test_project_tier_permission_does_not_block_proposal(self, conn):
        """Project-scoped permission does not exclude candidate from global proposal."""
        proj_id = _insert_project(conn)
        _seed_candidate(
            conn, "git", "status", "[]", observations=5, distinct_sessions=2
        )
        # Insert a project-tier permission (not global).
        now = _now()
        shape_id = db.upsert_rule_shape(conn, "git", "status", "[]", None, now)
        db.insert_permission(conn, shape_id, None, proj_id, "approved", "manual", now)
        conn.commit()

        proposals = propose_promotions(conn)
        assert len(proposals) == 1

    def test_session_tier_permission_does_not_block_proposal(self, conn):
        """Session-scoped permission does not exclude candidate from global proposal."""
        sess_id = _insert_session(conn, "sess-1")
        _seed_candidate(
            conn, "git", "status", "[]", observations=5, distinct_sessions=2
        )
        now = _now()
        shape_id = db.upsert_rule_shape(conn, "git", "status", "[]", None, now)
        db.insert_permission(conn, shape_id, sess_id, None, "approved", "manual", now)
        conn.commit()

        proposals = propose_promotions(conn)
        assert len(proposals) == 1

    def test_global_permission_for_different_flags_does_not_exclude(self, conn):
        _seed_candidate(
            conn, "git", "commit", '["-m"]', observations=5, distinct_sessions=2
        )
        # Global permission for git commit with no flags — different shape.
        _seed_global_permission(conn, "git", "commit", "[]", "approved")

        proposals = propose_promotions(conn)
        assert len(proposals) == 1


class TestProposeDenyFilter:
    def test_sudo_command_excluded_by_deny_filter(self, conn):
        """sudo is on the hard-deny list and should be excluded from proposals."""
        _seed_candidate(conn, "sudo", "rm", "[]", observations=10, distinct_sessions=5)

        # sudo is always hard-denied — deny filter removes it from proposals.
        proposals = propose_promotions(conn)
        assert all(c.verb != "sudo" for c in proposals)

    def test_safe_command_not_excluded_by_deny_filter(self, conn):
        """A safe command (git status) passes through the deny filter."""
        _seed_candidate(
            conn, "git", "status", "[]", observations=5, distinct_sessions=2
        )

        proposals = propose_promotions(conn)
        assert any(c.verb == "git" for c in proposals)


# ===========================================================================
# _candidate_leaf
# ===========================================================================


class TestCandidateLeaf:
    def test_reconstructs_flags_correctly(self):
        leaf = _candidate_leaf("git", "commit", '["-m","--amend"]')
        assert leaf.flags == frozenset({"-m", "--amend"})

    def test_handles_empty_flags(self):
        leaf = _candidate_leaf("ls", None, "[]")
        assert leaf.flags == frozenset()

    def test_malformed_flags_json_produces_empty_flags(self):
        leaf = _candidate_leaf("git", "status", "not-valid-json")
        assert leaf.flags == frozenset()

    def test_verb_and_subcommand_preserved(self):
        leaf = _candidate_leaf("git", "status", "[]")
        assert leaf.verb == "git"
        assert leaf.subcommand == "status"

    def test_none_subcommand_preserved(self):
        leaf = _candidate_leaf("ls", None, "[]")
        assert leaf.subcommand is None


# ===========================================================================
# _resolve_tier_ids
# ===========================================================================


class TestResolveTierIds:
    def test_global_tier_returns_none_none(self):
        assert _resolve_tier_ids("global", None, None) == (None, None)

    def test_session_tier_returns_session_id(self):
        assert _resolve_tier_ids("session", 42, None) == (42, None)

    def test_project_tier_returns_project_id(self):
        assert _resolve_tier_ids("project", None, 7) == (None, 7)

    def test_session_tier_without_session_id_raises(self):
        with pytest.raises(SystemExit, match="--session-id"):
            _resolve_tier_ids("session", None, None)

    def test_project_tier_without_project_id_raises(self):
        with pytest.raises(SystemExit, match="--project-id"):
            _resolve_tier_ids("project", None, None)

    def test_unknown_tier_raises(self):
        with pytest.raises(SystemExit, match="unknown tier"):
            _resolve_tier_ids("superuser", None, None)


# ===========================================================================
# _parse_flags_arg
# ===========================================================================


class TestParseFlagsArg:
    def test_none_input_returns_empty_array_json(self):
        result = _parse_flags_arg(None)
        assert json.loads(result) == []

    def test_empty_string_returns_empty_array_json(self):
        result = _parse_flags_arg("")
        assert json.loads(result) == []

    def test_json_array_is_sorted_and_minified(self):
        result = _parse_flags_arg('["-z", "-a"]')
        assert result == '["-a","-z"]'

    def test_single_flag(self):
        result = _parse_flags_arg('["-q"]')
        assert result == '["-q"]'

    def test_invalid_json_raises_system_exit(self):
        with pytest.raises(SystemExit, match="JSON array"):
            _parse_flags_arg("not-json")

    def test_non_list_json_raises_system_exit(self):
        with pytest.raises(SystemExit, match="JSON array"):
            _parse_flags_arg('{"flag": true}')


# ===========================================================================
# Promote / reject / unpermit via direct DB calls
# (exercises the DB helpers used by the CLI commands)
# ===========================================================================


class TestPromoteRejectUnpermit:
    def test_promote_inserts_rule_shape_and_approved_permission(self, conn):
        now = _now()
        shape_id = db.upsert_rule_shape(conn, "git", "status", "[]", None, now)
        db.insert_permission(conn, shape_id, None, None, "approved", "manual", now)
        conn.commit()

        row = conn.execute(
            "SELECT decision, source FROM permissions WHERE rule_shape_id=?",
            (shape_id,),
        ).fetchone()
        assert row[0] == "approved"
        assert row[1] == "manual"

    def test_reject_inserts_rejected_permission(self, conn):
        now = _now()
        shape_id = db.upsert_rule_shape(conn, "rm", None, '["-rf"]', None, now)
        db.insert_permission(
            conn, shape_id, None, None, "rejected", "manual", now, "dangerous"
        )
        conn.commit()

        row = conn.execute(
            "SELECT decision, reason FROM permissions WHERE rule_shape_id=?",
            (shape_id,),
        ).fetchone()
        assert row[0] == "rejected"
        assert row[1] == "dangerous"

    def test_promote_then_propose_excludes_candidate(self, conn):
        """After global promotion, the candidate no longer appears in proposals."""
        _seed_candidate(
            conn, "git", "status", "[]", observations=5, distinct_sessions=2
        )
        _seed_global_permission(conn, "git", "status", "[]", "approved")

        assert propose_promotions(conn) == []

    def test_session_tier_promote(self, conn):
        sess_id = _insert_session(conn, "sess-x")
        now = _now()
        shape_id = db.upsert_rule_shape(conn, "git", "status", "[]", None, now)
        db.insert_permission(conn, shape_id, sess_id, None, "approved", "manual", now)
        conn.commit()

        perm = conn.execute(
            "SELECT session_id, project_id, decision FROM permissions"
            " WHERE rule_shape_id=?",
            (shape_id,),
        ).fetchone()
        assert perm[0] == sess_id
        assert perm[1] is None
        assert perm[2] == "approved"

    def test_project_tier_promote(self, conn):
        proj_id = _insert_project(conn)
        now = _now()
        shape_id = db.upsert_rule_shape(conn, "git", "status", "[]", None, now)
        db.insert_permission(conn, shape_id, None, proj_id, "approved", "manual", now)
        conn.commit()

        perm = conn.execute(
            "SELECT session_id, project_id, decision FROM permissions"
            " WHERE rule_shape_id=?",
            (shape_id,),
        ).fetchone()
        assert perm[0] is None
        assert perm[1] == proj_id
        assert perm[2] == "approved"

    def test_unpermit_deletes_global_permission(self, conn):
        now = _now()
        shape_id = db.upsert_rule_shape(conn, "git", "status", "[]", None, now)
        db.insert_permission(conn, shape_id, None, None, "approved", "manual", now)
        conn.commit()

        deleted = conn.execute(
            "DELETE FROM permissions WHERE rule_shape_id=? AND session_id IS NULL"
            " AND project_id IS NULL",
            (shape_id,),
        ).rowcount
        conn.commit()

        assert deleted == 1
        remaining = conn.execute(
            "SELECT COUNT(*) FROM permissions WHERE rule_shape_id=?", (shape_id,)
        ).fetchone()[0]
        assert remaining == 0

    def test_unpermit_only_removes_matching_tier(self, conn):
        """Unpermitting session tier leaves global permission intact."""
        sess_id = _insert_session(conn, "sess-y")
        now = _now()
        shape_id = db.upsert_rule_shape(conn, "git", "status", "[]", None, now)
        # Both global and session permissions.
        db.insert_permission(conn, shape_id, None, None, "approved", "manual", now)
        db.insert_permission(conn, shape_id, sess_id, None, "approved", "manual", now)
        conn.commit()

        # Delete only session-tier row.
        conn.execute(
            "DELETE FROM permissions WHERE rule_shape_id=? AND session_id IS NOT NULL",
            (shape_id,),
        )
        conn.commit()

        remaining = conn.execute(
            "SELECT COUNT(*) FROM permissions WHERE rule_shape_id=?", (shape_id,)
        ).fetchone()[0]
        assert remaining == 1  # Global row intact.

    def test_rule_shape_upsert_is_idempotent(self, conn):
        now = _now()
        id1 = db.upsert_rule_shape(conn, "git", "status", "[]", None, now)
        id2 = db.upsert_rule_shape(conn, "git", "status", "[]", None, now)
        assert id1 == id2
        count = conn.execute("SELECT COUNT(*) FROM rule_shapes").fetchone()[0]
        assert count == 1

    def test_path_spec_stored_on_rule_shape(self, conn):
        now = _now()
        shape_id = db.upsert_rule_shape(conn, "rm", None, "[]", "$PROJECT_ROOT/**", now)
        conn.commit()
        row = conn.execute(
            "SELECT path_spec FROM rule_shapes WHERE id=?", (shape_id,)
        ).fetchone()
        assert row[0] == "$PROJECT_ROOT/**"

    def test_flags_wildcard_stored_on_rule_shape(self, conn):
        now = _now()
        shape_id = db.upsert_rule_shape(conn, "git", "status", "*", None, now)
        conn.commit()
        row = conn.execute(
            "SELECT flags FROM rule_shapes WHERE id=?", (shape_id,)
        ).fetchone()
        assert row[0] == "*"


# ===========================================================================
# v_candidates and v_permissions views
# ===========================================================================


class TestViews:
    def test_v_candidates_shows_inserted_candidate(self, conn):
        _seed_candidate(
            conn, "git", "status", "[]", observations=3, distinct_sessions=1
        )

        row = conn.execute("SELECT verb, subcommand FROM v_candidates").fetchone()
        assert row == ("git", "status")

    def test_v_permissions_shows_permission_with_tier(self, conn):
        now = _now()
        shape_id = db.upsert_rule_shape(conn, "git", "status", "[]", None, now)
        db.insert_permission(conn, shape_id, None, None, "approved", "seed", now)
        conn.commit()

        row = conn.execute("SELECT verb, decision, tier FROM v_permissions").fetchone()
        assert row[0] == "git"
        assert row[1] == "approved"
        assert row[2] == "global"

    def test_v_permissions_session_tier_label(self, conn):
        sess_id = _insert_session(conn, "sess-z")
        now = _now()
        shape_id = db.upsert_rule_shape(conn, "ls", None, "[]", None, now)
        db.insert_permission(conn, shape_id, sess_id, None, "approved", "manual", now)
        conn.commit()

        row = conn.execute("SELECT tier FROM v_permissions WHERE verb='ls'").fetchone()
        assert row[0] == "session"

    def test_v_permissions_project_tier_label(self, conn):
        proj_id = _insert_project(conn)
        now = _now()
        shape_id = db.upsert_rule_shape(conn, "ls", None, "[]", None, now)
        db.insert_permission(conn, shape_id, None, proj_id, "rejected", "manual", now)
        conn.commit()

        row = conn.execute(
            "SELECT tier, decision FROM v_permissions WHERE verb='ls'"
        ).fetchone()
        assert row[0] == "project"
        assert row[1] == "rejected"


# ===========================================================================
# Cursor management (doom-path: zero / off-by-one)
# ===========================================================================


class TestCursorManagement:
    def test_set_cursor_creates_row(self, conn):
        _set_cursor(conn, 42)
        assert _get_cursor(conn) == 42

    def test_set_cursor_is_idempotent_on_retry(self, conn):
        _set_cursor(conn, 10)
        _set_cursor(conn, 10)
        assert _get_cursor(conn) == 10

    def test_set_cursor_advances_monotonically(self, conn):
        _set_cursor(conn, 5)
        _set_cursor(conn, 20)
        assert _get_cursor(conn) == 20

    def test_cursor_per_consumer_namespace(self, conn):
        """consumer_cursors is keyed by consumer name — our name should be isolated."""
        _set_cursor(conn, 99)
        # A different consumer should not affect ours.
        conn.execute(
            "INSERT INTO consumer_cursors(consumer, last_processed_id, updated_at)"
            " VALUES ('other-consumer', 1, datetime('now'))"
        )
        conn.commit()
        assert _get_cursor(conn) == 99


# ===========================================================================
# _parse_flags_arg — wildcard extension
# ===========================================================================


class TestParseFlagsArgWildcard:
    def test_star_returns_sentinel(self):
        result = _parse_flags_arg("*")
        assert result == "*"

    def test_empty_returns_empty_array(self):
        result = _parse_flags_arg("")
        assert result == "[]"

    def test_none_returns_empty_array(self):
        result = _parse_flags_arg(None)
        assert result == "[]"

    def test_json_array_parsed_normally(self):
        result = _parse_flags_arg('["-r","-f"]')
        assert json.loads(result) == ["-f", "-r"]  # sorted


# ===========================================================================
# pattern-variants subcommand
# ===========================================================================


class TestPatternVariantsCommand:
    def test_literal_verb_no_pattern(self, tmp_db, capsys):
        """Non-absolute verb produces no verb_pattern."""
        learner_main(["pattern-variants", "--verb", "git", "--flags", "[]"])
        out = json.loads(capsys.readouterr().out)
        assert out["verb_pattern"] is None
        assert out["path_specs"] == []
        assert out["flags_literal"] == "[]"

    def test_abs_verb_under_home_gives_pattern(self, tmp_db, capsys):
        """Verb = $HOME/bin/tool → verb_pattern = $HOME/bin/tool."""
        import os

        home = os.path.expanduser("~")
        tool = f"{home}/bin/mytool"
        learner_main(
            [
                "pattern-variants",
                "--verb",
                tool,
                "--flags",
                "[]",
                "--home",
                home,
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert out["verb_pattern"] == "$HOME/bin/mytool"

    def test_abs_verb_under_project_root_preferred_over_home(self, tmp_db, capsys):
        """PROJECT_ROOT beats HOME when both match (longer prefix wins)."""
        import os

        home = os.path.expanduser("~")
        project = f"{home}/data/project"
        tool = f"{project}/scripts/deploy.sh"
        learner_main(
            [
                "pattern-variants",
                "--verb",
                tool,
                "--flags",
                "[]",
                "--home",
                home,
                "--project-root",
                project,
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert out["verb_pattern"] == "$PROJECT_ROOT/scripts/deploy.sh"

    def test_wildcard_flags_passes_through(self, tmp_db, capsys):
        """flags=* is not altered by pattern-variants."""
        learner_main(["pattern-variants", "--verb", "git", "--flags", "*"])
        out = json.loads(capsys.readouterr().out)
        assert out["flags_literal"] == "*"

    def test_subcommand_carried_in_output(self, tmp_db, capsys):
        """Subcommand does not affect verb_pattern logic."""
        learner_main(
            [
                "pattern-variants",
                "--verb",
                "git",
                "--subcommand",
                "status",
                "--flags",
                "[]",
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert out["verb_pattern"] is None


# ===========================================================================
# context-ids subcommand
# ===========================================================================


def _mock_connect(conn: sqlite3.Connection):
    """Context manager that patches ``_connect()`` to return ``conn``.

    Suppresses the ``close()`` call so the test fixture connection stays open.
    """
    return mock.patch(
        "nephoscope.learners.permission.learner._connect",
        side_effect=lambda: _NonClosingConn(conn),
    )


class _NonClosingConn:
    """Thin wrapper that forwards all attribute access to the real connection
    but turns ``close()`` into a no-op so test fixtures stay alive."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def close(self) -> None:  # swallow
        pass

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


class TestContextIdsCommand:
    def test_unknown_cwd_returns_empty(self, tmp_db, capsys):
        with _mock_connect(tmp_db):
            learner_main(["context-ids", "--cwd", "/nonexistent/path"])
        out = capsys.readouterr().out
        assert "project_id=" in out
        assert "session_id=" in out
        # Both values should be empty (not found).
        lines = {k: v for k, v in (ln.split("=", 1) for ln in out.strip().splitlines())}
        assert lines["project_id"] == ""
        assert lines["session_id"] == ""

    def test_known_cwd_returns_project_id(self, tmp_db, capsys):
        now = _now()
        tmp_db.execute(
            "INSERT INTO projects(cwd, name, root, first_seen, last_seen)"
            " VALUES ('/work/proj', 'proj', '/work/proj', ?, ?)",
            (now, now),
        )
        tmp_db.commit()
        with _mock_connect(tmp_db):
            learner_main(["context-ids", "--cwd", "/work/proj"])
        out = capsys.readouterr().out
        lines = {k: v for k, v in (ln.split("=", 1) for ln in out.strip().splitlines())}
        assert lines["project_id"].isdigit()

    def test_known_project_with_session_returns_session_id(self, tmp_db, capsys):
        now = _now()
        tmp_db.execute(
            "INSERT INTO projects(cwd, name, root, first_seen, last_seen)"
            " VALUES ('/work/proj2', 'proj2', '/work/proj2', ?, ?)",
            (now, now),
        )
        tmp_db.commit()
        proj_id = tmp_db.execute(
            "SELECT id FROM projects WHERE cwd='/work/proj2'"
        ).fetchone()[0]
        tmp_db.execute(
            "INSERT INTO sessions(session_uuid, project_id, started_at, last_activity)"
            " VALUES ('test-uuid-ctx', ?, ?, ?)",
            (proj_id, now, now),
        )
        tmp_db.commit()
        with _mock_connect(tmp_db):
            learner_main(["context-ids", "--cwd", "/work/proj2"])
        out = capsys.readouterr().out
        lines = {k: v for k, v in (ln.split("=", 1) for ln in out.strip().splitlines())}
        assert lines["session_id"].isdigit()


# ===========================================================================
# count-concrete-siblings subcommand
# ===========================================================================


class TestCountConcreteSiblings:
    def test_zero_when_no_permissions(self, tmp_db, capsys):
        with _mock_connect(tmp_db):
            learner_main(
                [
                    "count-concrete-siblings",
                    "--verb",
                    "git",
                    "--subcommand",
                    "push",
                ]
            )
        out = capsys.readouterr().out.strip()
        assert out == "0"

    def test_counts_concrete_permissions(self, tmp_db, capsys):
        """Concrete (non-wildcard) sibling permissions are counted."""
        now = _now()
        for flags in ("[]", '["-f"]'):
            shape_id = db.upsert_rule_shape(tmp_db, "git", "push", flags, None, now)
            db.insert_permission(tmp_db, shape_id, None, None, "approved", "seed", now)
        tmp_db.commit()

        with _mock_connect(tmp_db):
            learner_main(
                [
                    "count-concrete-siblings",
                    "--verb",
                    "git",
                    "--subcommand",
                    "push",
                ]
            )
        out = capsys.readouterr().out.strip()
        assert out == "2"

    def test_wildcard_rule_not_counted(self, tmp_db, capsys):
        """A flags=* permission for the same verb+sub is excluded from count."""
        now = _now()
        shape_id = db.upsert_rule_shape(tmp_db, "git", "push", "*", None, now)
        db.insert_permission(tmp_db, shape_id, None, None, "approved", "seed", now)
        tmp_db.commit()

        with _mock_connect(tmp_db):
            learner_main(
                [
                    "count-concrete-siblings",
                    "--verb",
                    "git",
                    "--subcommand",
                    "push",
                ]
            )
        out = capsys.readouterr().out.strip()
        assert out == "0"

    def test_different_verb_not_counted(self, tmp_db, capsys):
        """Permissions for a different verb do not pollute the count."""
        now = _now()
        shape_id = db.upsert_rule_shape(tmp_db, "ls", None, "[]", None, now)
        db.insert_permission(tmp_db, shape_id, None, None, "approved", "seed", now)
        tmp_db.commit()

        with _mock_connect(tmp_db):
            learner_main(
                [
                    "count-concrete-siblings",
                    "--verb",
                    "git",
                    "--subcommand",
                    "push",
                ]
            )
        out = capsys.readouterr().out.strip()
        assert out == "0"

    def test_counts_only_at_matching_tier(self, tmp_db, capsys):
        """Permissions at a different tier are not counted for the requested tier."""
        now = _now()
        proj_id = _insert_project(tmp_db)
        shape_id = db.upsert_rule_shape(tmp_db, "git", "push", "[]", None, now)
        # Project-tier permission only.
        db.insert_permission(tmp_db, shape_id, None, proj_id, "approved", "seed", now)
        tmp_db.commit()

        # Query global tier — should be 0.
        with _mock_connect(tmp_db):
            learner_main(
                [
                    "count-concrete-siblings",
                    "--verb",
                    "git",
                    "--subcommand",
                    "push",
                    "--tier",
                    "global",
                ]
            )
        out = capsys.readouterr().out.strip()
        assert out == "0"


# ===========================================================================
# subsume-siblings subcommand
# ===========================================================================


class TestSubsumeSiblings:
    def test_deletes_concrete_siblings_globally(self, tmp_db, capsys):
        """subsume-siblings removes all concrete global permissions for verb+sub."""
        now = _now()
        for flags in ("[]", '["-f"]', '["-u"]'):
            shape_id = db.upsert_rule_shape(tmp_db, "git", "push", flags, None, now)
            db.insert_permission(tmp_db, shape_id, None, None, "approved", "seed", now)
        tmp_db.commit()

        with (
            _mock_connect(tmp_db),
            mock.patch("nephoscope.lib.mirror.writer.sync_global", return_value=None),
        ):
            learner_main(
                [
                    "subsume-siblings",
                    "--verb",
                    "git",
                    "--subcommand",
                    "push",
                ]
            )
        out = capsys.readouterr().out
        assert "3" in out

        remaining = tmp_db.execute("SELECT COUNT(*) FROM permissions").fetchone()[0]
        assert remaining == 0

    def test_does_not_delete_wildcard_sibling(self, tmp_db, capsys):
        """The flags=* rule itself is not deleted by subsume-siblings."""
        now = _now()
        # Concrete + wildcard.
        shape_concrete = db.upsert_rule_shape(tmp_db, "git", "push", "[]", None, now)
        shape_wild = db.upsert_rule_shape(tmp_db, "git", "push", "*", None, now)
        db.insert_permission(
            tmp_db, shape_concrete, None, None, "approved", "seed", now
        )
        db.insert_permission(tmp_db, shape_wild, None, None, "approved", "seed", now)
        tmp_db.commit()

        with (
            _mock_connect(tmp_db),
            mock.patch("nephoscope.lib.mirror.writer.sync_global", return_value=None),
        ):
            learner_main(
                [
                    "subsume-siblings",
                    "--verb",
                    "git",
                    "--subcommand",
                    "push",
                ]
            )
        capsys.readouterr()  # discard output

        # Only the wildcard permission should remain.
        remaining = tmp_db.execute("SELECT COUNT(*) FROM permissions").fetchone()[0]
        assert remaining == 1
        row = tmp_db.execute(
            "SELECT rs.flags FROM permissions p"
            " JOIN rule_shapes rs ON rs.id = p.rule_shape_id"
        ).fetchone()
        assert row[0] == "*"

    def test_does_not_delete_different_verb(self, tmp_db, capsys):
        """Permissions for a different verb are unaffected."""
        now = _now()
        shape_id = db.upsert_rule_shape(tmp_db, "ls", None, "[]", None, now)
        db.insert_permission(tmp_db, shape_id, None, None, "approved", "seed", now)
        tmp_db.commit()

        with _mock_connect(tmp_db):
            learner_main(
                [
                    "subsume-siblings",
                    "--verb",
                    "git",
                    "--subcommand",
                    "push",
                ]
            )
        capsys.readouterr()

        remaining = tmp_db.execute("SELECT COUNT(*) FROM permissions").fetchone()[0]
        assert remaining == 1

    def test_subsume_zero_siblings_is_idempotent(self, tmp_db, capsys):
        """subsume-siblings with no concrete siblings prints 0 and is a no-op."""
        with _mock_connect(tmp_db):
            learner_main(
                [
                    "subsume-siblings",
                    "--verb",
                    "git",
                    "--subcommand",
                    "push",
                ]
            )
        out = capsys.readouterr().out
        assert "0" in out

    def test_tier_isolation(self, tmp_db, capsys):
        """subsume-siblings at project tier does not delete global permissions."""
        now = _now()
        proj_id = _insert_project(tmp_db)
        shape_id = db.upsert_rule_shape(tmp_db, "git", "push", "[]", None, now)
        # Global tier only.
        db.insert_permission(tmp_db, shape_id, None, None, "approved", "seed", now)
        tmp_db.commit()

        with _mock_connect(tmp_db):
            learner_main(
                [
                    "subsume-siblings",
                    "--verb",
                    "git",
                    "--subcommand",
                    "push",
                    "--tier",
                    "project",
                    "--project-id",
                    str(proj_id),
                ]
            )
        capsys.readouterr()

        # Global permission should still be there.
        remaining = tmp_db.execute("SELECT COUNT(*) FROM permissions").fetchone()[0]
        assert remaining == 1


# ---------------------------------------------------------------------------
# _describe_rule / _tier_phrase — pure-function output formatters
# ---------------------------------------------------------------------------


class TestTierPhrase:
    """_tier_phrase decodes tier tokens into plain-English phrases."""

    def test_known_tiers_humanize(self):
        assert _tier_phrase("global") == "everywhere"
        assert _tier_phrase("project") == "in this project"
        assert _tier_phrase("session") == "in this session"

    def test_unknown_tier_falls_back_to_token(self):
        # Defensive: an unrecognized tier value should not crash, just echo.
        assert _tier_phrase("rogue") == "rogue"


class TestDescribeRule:
    """_describe_rule renders a rule shape as a plain-English description.

    flags_json axis: literal sentinel "*" / valid JSON array / malformed string / empty list.
    subcommand axis: None / non-empty string / empty string (falsy → no sub-part).
    path_spec axis:  None / "" / arbitrary glob.
    """

    # ---- flags_json axis -----------------------------------------------

    def test_wildcard_flags_sentinel(self):
        out = _describe_rule("ls", None, "*", None)
        assert out == "ls with any options"

    def test_concrete_flags_array(self):
        out = _describe_rule("git", "commit", '["--amend"]', None)
        assert out == "git commit with options --amend"

    def test_multiple_concrete_flags_preserve_order(self):
        # JSON array entries are joined in their stored order — no sorting,
        # so tests pin the exact rendering for stability.
        out = _describe_rule("ls", None, '["-a","-l"]', None)
        assert out == "ls with options -a -l"

    def test_empty_flags_array_renders_no_options(self):
        out = _describe_rule("ls", None, "[]", None)
        assert out == "ls (no options)"

    def test_malformed_flags_json_falls_back_to_no_options(self):
        # Malformed JSON should not crash; the helper degrades to "(no options)".
        out = _describe_rule("ls", None, "not-json", None)
        assert out == "ls (no options)"

    def test_none_flags_json_renders_no_options(self):
        # None is an explicit valid input — early-exits to the no-options branch
        # before json.loads is reached.
        out = _describe_rule("ls", None, None, None)
        assert out == "ls (no options)"

    # ---- subcommand axis -----------------------------------------------

    def test_subcommand_inserts_with_leading_space(self):
        out = _describe_rule("git", "status", "[]", None)
        assert out == "git status (no options)"

    def test_none_subcommand_omits_sub_part(self):
        out = _describe_rule("ls", None, "[]", None)
        # No double space between verb and flags-part.
        assert "  " not in out
        assert out == "ls (no options)"

    def test_empty_string_subcommand_omits_sub_part(self):
        # Falsy subcommand should be treated like None — no stray space.
        out = _describe_rule("ls", "", "[]", None)
        assert out == "ls (no options)"
        assert "  " not in out

    # ---- path_spec axis ------------------------------------------------

    def test_none_path_spec_renders_no_path_clause(self):
        out = _describe_rule("ls", None, "[]", None)
        assert "paths" not in out
        assert out == "ls (no options)"

    def test_empty_path_spec_renders_no_paths_clause(self):
        out = _describe_rule("ls", None, "[]", "")
        assert out == "ls (no options) (only when no paths are given)"

    def test_glob_path_spec_renders_matching_clause(self):
        out = _describe_rule("rm", None, "*", "$PROJECT_ROOT/**")
        assert out == "rm with any options on paths matching $PROJECT_ROOT/**"

    # ---- combined -------------------------------------------------------

    def test_full_rule_combines_all_parts(self):
        out = _describe_rule("git", "push", '["--force"]', "$HOME/projects/**")
        assert (
            out == "git push with options --force on paths matching $HOME/projects/**"
        )
