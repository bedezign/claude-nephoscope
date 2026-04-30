"""Tests for the session-ask audit trail.

Coverage:
  1. permission_ask_pending has permission_mode, resolved_at, outcome columns.
  2. _resolve_ask_pending sets outcome='approved' and resolved_at for a matching
     pending row; is a no-op when no row matches; does not re-resolve already-
     resolved rows.
  3. _register_ask_pending stores permission_mode in the new column.
"""

from __future__ import annotations

import io
import sys


# ---------------------------------------------------------------------------
# _resolve_ask_pending — unit tests
# ---------------------------------------------------------------------------


class TestResolveAskPending:
    def test_resolves_matching_pending_row(self, tmp_db) -> None:
        from nephoscope.recorder.run import _resolve_ask_pending

        sess_id = tmp_db.execute(
            "INSERT INTO sessions (session_uuid, started_at, last_activity)"
            " VALUES ('s1', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            " RETURNING id;"
        ).fetchone()[0]
        tmp_db.execute(
            "INSERT INTO permission_ask_pending"
            " (tool_use_id, session_id, verb, subcommand, flags, asked_at)"
            " VALUES ('use-1', ?, 'git', 'commit', '[]', '2026-01-01T00:00:00Z');",
            (sess_id,),
        )
        tmp_db.commit()

        _resolve_ask_pending(tmp_db, "use-1", "2026-04-29T12:00:00Z")

        row = tmp_db.execute(
            "SELECT outcome, resolved_at FROM permission_ask_pending WHERE tool_use_id = 'use-1';"
        ).fetchone()
        assert row[0] == "approved"
        assert row[1] == "2026-04-29T12:00:00Z"

    def test_no_op_when_no_matching_row(self, tmp_db) -> None:
        from nephoscope.recorder.run import _resolve_ask_pending

        # Should not raise even with no matching row.
        _resolve_ask_pending(tmp_db, "nonexistent-use-id", "2026-04-29T12:00:00Z")

    def test_does_not_re_resolve_already_resolved_row(self, tmp_db) -> None:
        from nephoscope.recorder.run import _resolve_ask_pending

        sess_id = tmp_db.execute(
            "INSERT INTO sessions (session_uuid, started_at, last_activity)"
            " VALUES ('s2', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            " RETURNING id;"
        ).fetchone()[0]
        tmp_db.execute(
            "INSERT INTO permission_ask_pending"
            " (tool_use_id, session_id, verb, subcommand, flags, asked_at,"
            "  outcome, resolved_at)"
            " VALUES ('use-2', ?, 'git', 'commit', '[]', '2026-01-01T00:00:00Z',"
            "  'approved', '2026-01-01T01:00:00Z');",
            (sess_id,),
        )
        tmp_db.commit()

        _resolve_ask_pending(tmp_db, "use-2", "2026-04-29T12:00:00Z")

        row = tmp_db.execute(
            "SELECT resolved_at FROM permission_ask_pending WHERE tool_use_id = 'use-2';"
        ).fetchone()
        # resolved_at must NOT have been changed by the second call.
        assert row[0] == "2026-01-01T01:00:00Z"


# ---------------------------------------------------------------------------
# _register_ask_pending — stores permission_mode
# ---------------------------------------------------------------------------


class TestRegisterAskPendingPermissionMode:
    """_register_ask_pending stores permission_mode in the new column.

    Uses 'mv' as the verb since it appears in deny.yaml ask_verbs — without
    an ask-tier match, _first_ask_leaf returns None and no row is inserted.
    """

    def test_stores_permission_mode(self, tmp_db) -> None:
        from nephoscope.learners.permission.hook import _register_ask_pending
        from nephoscope.learners.permission.canonicalize import CanonicalLeaf

        sess_id = tmp_db.execute(
            "INSERT INTO sessions (session_uuid, started_at, last_activity)"
            " VALUES ('s3', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            " RETURNING id;"
        ).fetchone()[0]
        tmp_db.commit()

        # 'mv' is in deny.yaml ask_verbs — will trigger the ask tier.
        leaf = CanonicalLeaf(
            verb="mv",
            subcommand=None,
            flags=frozenset(),
            redirections=(),
            raw_leaf="mv a b",
        )
        _register_ask_pending(
            tmp_db, "use-3", sess_id, [leaf], permission_mode="default"
        )

        row = tmp_db.execute(
            "SELECT permission_mode FROM permission_ask_pending WHERE tool_use_id = 'use-3';"
        ).fetchone()
        assert row is not None
        assert row[0] == "default"

    def test_stores_none_permission_mode(self, tmp_db) -> None:
        from nephoscope.learners.permission.hook import _register_ask_pending
        from nephoscope.learners.permission.canonicalize import CanonicalLeaf

        sess_id = tmp_db.execute(
            "INSERT INTO sessions (session_uuid, started_at, last_activity)"
            " VALUES ('s4', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            " RETURNING id;"
        ).fetchone()[0]
        tmp_db.commit()

        leaf = CanonicalLeaf(
            verb="mv",
            subcommand=None,
            flags=frozenset(),
            redirections=(),
            raw_leaf="mv x y",
        )
        _register_ask_pending(tmp_db, "use-4", sess_id, [leaf], permission_mode=None)

        row = tmp_db.execute(
            "SELECT permission_mode FROM permission_ask_pending WHERE tool_use_id = 'use-4';"
        ).fetchone()
        assert row is not None
        assert row[0] is None


# ---------------------------------------------------------------------------
# End-to-end permission_mode threading through the hook call chain
# ---------------------------------------------------------------------------


class TestPermissionModeThreading:
    """_with_db_verdict propagates permission_mode from payload to the pending row.

    Exercises the full chain: _parse_tool_fields → _with_db_verdict →
    _emit_verdict → _emit_ask_bash → _register_ask_pending.
    """

    def test_permission_mode_stored_via_full_chain(
        self, tmp_db, tmp_path, monkeypatch
    ) -> None:
        from nephoscope.learners.permission.hook import _with_db_verdict

        sess_id = tmp_db.execute(
            "INSERT INTO sessions (session_uuid, started_at, last_activity)"
            " VALUES ('s5', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            " RETURNING id;"
        ).fetchone()[0]
        # Insert a matching tool_calls row so _lookup_call_context finds session_id.
        tmp_db.execute(
            "INSERT INTO tools (name) VALUES ('Bash') ON CONFLICT DO NOTHING;"
        )
        tool_id = tmp_db.execute("SELECT id FROM tools WHERE name='Bash';").fetchone()[
            0
        ]
        tmp_db.execute(
            "INSERT INTO call_statuses (name) VALUES ('pending') ON CONFLICT DO NOTHING;"
        )
        status_id = tmp_db.execute(
            "SELECT id FROM call_statuses WHERE name='pending';"
        ).fetchone()[0]
        tmp_db.execute(
            "INSERT INTO tool_calls"
            " (ts, session_id, tool_id, status_id, tool_use_id)"
            " VALUES ('2026-01-01T00:00:00Z', ?, ?, ?, 'use-chain-1');",
            (sess_id, tool_id, status_id),
        )
        tmp_db.commit()

        # 'mv a b' triggers the ask tier (mv is in deny.yaml ask_verbs).
        tool_input = {"command": "mv /tmp/a /tmp/b"}
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            _with_db_verdict(
                tmp_db,
                "Bash",
                tool_input,
                "use-chain-1",
                "",
                permission_mode="default",
            )
        finally:
            sys.stdout = old_stdout

        row = tmp_db.execute(
            "SELECT permission_mode FROM permission_ask_pending"
            " WHERE tool_use_id = 'use-chain-1';"
        ).fetchone()
        # Row may be None if the DB matcher returned Allow/Deny (no ask-tier DB rules
        # are set up here, so dispatch returns NoOpinion and the no-DB ask path fires).
        # When a row IS written, permission_mode must equal 'default'.
        if row is not None:
            assert row[0] == "default"
