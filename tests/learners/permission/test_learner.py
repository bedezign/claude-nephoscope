"""Tests for learners.permission.learner (scan + propose_promotions).

Post-v7/v8 schema: ``tool_calls`` keys into lookup tables for tool name,
subagent type and file path, and its ``session_id`` is an INTEGER FK into
``sessions(id)`` (the UUID lives on ``sessions.session_uuid``). The
``permission_candidate_sessions.session_id`` column is likewise INTEGER
post-v8. Synthetic rows are inserted via the FK columns only; the legacy
TEXT columns (``tool``, ``subagent_type``, ``file_path``, TEXT
``session_id``) are dropped in v8 and never referenced.

The insert helper below is schema-aware because the v7 → v8 migration is
split across two files owned by a second agent (recorder.py + v8.sql).
During the transition window v7 is on disk but v8 may not be yet, leaving
the legacy TEXT columns in place — we introspect ``PRAGMA table_info``
once per test and set whatever columns still exist, so the suite passes
in either schema state. Once v8 lands this helper is effectively
post-v8-only (the legacy-column branch becomes unreachable).
"""
from __future__ import annotations

from lib.db import (
    _upsert_project,
    _upsert_session,
    lookup_or_insert_tool_id,
    lookup_permission_mode_id,
    lookup_status_id,
    minify_json,
)
from learners.permission import learner


def _tool_call_columns(conn) -> set[str]:
    """Return the live column names on ``tool_calls``.

    Used to branch between the v7 (legacy TEXT columns still present) and
    v8 (FK-only) schema states. Read once per insert — the helper runs in
    tests, not the hot path.
    """
    return {row[1] for row in conn.execute("PRAGMA table_info(tool_calls);")}


def _pcs_columns(conn) -> set[str]:
    """Return the live column names on ``permission_candidate_sessions``."""
    return {
        row[1] for row in conn.execute(
            "PRAGMA table_info(permission_candidate_sessions);"
        )
    }


def _insert_tool_call(
    conn,
    *,
    command: str,
    session_id: str,
    status: str = "ok",
    tool: str = "Bash",
    permission_mode: str | None = "default",
) -> int:
    """Insert a synthetic tool_calls row for the learner to ingest.

    ``session_id`` is the caller's UUID-shaped string; the helper resolves
    it through ``_upsert_session`` to the INTEGER ``sessions.id`` that FK
    columns expect. ``tool`` is likewise resolved through
    ``lookup_or_insert_tool_id``. ``permission_mode`` may be ``None`` to
    simulate legacy rows whose payload didn't carry a mode.
    """
    now = "2026-04-20T10:00:00.000Z"
    project_id = _upsert_project(conn, cwd=f"/tmp/test-{session_id}", now=now)
    session_int_id = _upsert_session(conn, session_id, project_id, now)
    status_id = lookup_status_id(conn, status)
    permission_mode_id = (
        lookup_permission_mode_id(conn, permission_mode)
        if permission_mode is not None
        else None
    )
    tool_id = lookup_or_insert_tool_id(conn, tool)

    cols = _tool_call_columns(conn)
    # Base column list — always present post-v2.
    names = [
        "ts", "project_id", "ok", "command", "args_json",
        "tool_use_id", "completed_ts", "status_id", "permission_mode_id",
        "tool_id",
    ]
    values: list = [
        now, project_id, 1, command, "{}",
        f"use::{command}::{session_id}::{status}", now, status_id,
        permission_mode_id, tool_id,
    ]

    # Session FK column name flipped in v8: `session_id_new` (v7 staging
    # column) → `session_id` (renamed after TEXT column dropped). Both
    # cases write the INTEGER id to whichever column exists.
    if "session_id_new" in cols:
        names.append("session_id_new")
        values.append(session_int_id)
    else:
        names.append("session_id")
        values.append(session_int_id)

    # Legacy TEXT columns — still present pre-v8, must be set to satisfy
    # the v1 NOT NULL constraint on `tool`. Skipped post-v8 because the
    # columns are gone.
    if "tool" in cols:
        names.append("tool")
        values.append(tool)
    if "session_id" in cols and "session_id_new" in cols:
        # Pre-v8 only — TEXT session_id is part of the PK nowhere on
        # tool_calls but is nullable; still write the UUID for any
        # consumers that might read it during the transition.
        names.append("session_id")
        values.append(session_id)

    placeholders = ",".join(["?"] * len(names))
    sql = (
        f"INSERT INTO tool_calls ({', '.join(names)}) VALUES ({placeholders});"
    )
    cur = conn.execute(sql, values)
    return int(cur.lastrowid or 0)


def _flags_json(flags: list[str]) -> str:
    """Serialise a flag list in the same minified form the learner stores.

    Matches ``lib.db.minify_json(sorted(list(leaf.flags)))`` — compact
    separators, no whitespace — so direct string compares against the
    stored ``command_shapes.flags`` column succeed.
    """
    return minify_json(sorted(flags))


def _session_int_id(conn, session_uuid: str) -> int:
    """Resolve a UUID to its INTEGER ``sessions.id``."""
    row = conn.execute(
        "SELECT id FROM sessions WHERE session_uuid = ?;", (session_uuid,)
    ).fetchone()
    assert row is not None, f"session {session_uuid} not upserted"
    return int(row[0])


def _candidate_row(conn, verb: str, subcommand: str | None):
    """Fetch a candidate row joined with its shape fields, or None."""
    if subcommand is None:
        return conn.execute(
            """
            SELECT c.observations, c.distinct_sessions, cs.flags
              FROM permission_candidates c
              JOIN command_shapes cs ON cs.id = c.command_shape_id
             WHERE cs.verb = ? AND cs.subcommand IS NULL;
            """,
            (verb,),
        ).fetchone()
    return conn.execute(
        """
        SELECT c.observations, c.distinct_sessions, cs.flags
          FROM permission_candidates c
          JOIN command_shapes cs ON cs.id = c.command_shape_id
         WHERE cs.verb = ? AND cs.subcommand = ?;
        """,
        (verb, subcommand),
    ).fetchone()


def _count_candidates(conn, verb: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
          FROM permission_candidates c
          JOIN command_shapes cs ON cs.id = c.command_shape_id
         WHERE cs.verb = ?;
        """,
        (verb,),
    ).fetchone()
    return int(row[0])


def _pcs_session_column(conn) -> str:
    """Return the active session column on ``permission_candidate_sessions``.

    v7 has both ``session_id`` (TEXT) and ``session_id_new`` (INTEGER);
    the learner writes the INTEGER one. v8 renames it to ``session_id``.
    Tests that need to query the junction by session pick the right
    column via this introspection.
    """
    cols = _pcs_columns(conn)
    return "session_id_new" if "session_id_new" in cols else "session_id"


def test_scan_upserts_candidate_with_threshold_meeting_shape(tmp_db):
    # 5 observations of `git status` across 2 sessions.
    for i in range(3):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-A")
    for i in range(2):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-B")

    processed = learner.scan_candidates(tmp_db)
    assert processed == 5

    row = _candidate_row(tmp_db, "git", "status")
    assert row is not None
    observations, distinct_sessions, flags_json = row
    assert observations == 5
    assert distinct_sessions >= 2
    assert flags_json == _flags_json([])


def test_propose_promotions_respects_thresholds(tmp_db):
    for _ in range(3):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-A")
    for _ in range(2):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-B")
    learner.scan_candidates(tmp_db)

    promotions = learner.propose_promotions(tmp_db)
    assert any(c.verb == "git" and c.subcommand == "status" for c in promotions)


def test_propose_promotions_excludes_under_threshold(tmp_db):
    # 4 observations — below the default threshold of 5.
    for _ in range(4):
        _insert_tool_call(tmp_db, command="ls -la", session_id="sess-A")
    learner.scan_candidates(tmp_db)

    promotions = learner.propose_promotions(tmp_db)
    assert not any(c.verb == "ls" for c in promotions)


def test_propose_promotions_excludes_single_session(tmp_db):
    # 5 observations but only 1 session — distinct_sessions < 2.
    for _ in range(5):
        _insert_tool_call(tmp_db, command="echo hi", session_id="sess-only")
    learner.scan_candidates(tmp_db)

    promotions = learner.propose_promotions(tmp_db)
    # echo hi appears across 1 distinct session, so it must NOT be promoted.
    echo_candidates = [c for c in promotions if c.verb == "echo"]
    for c in echo_candidates:
        assert c.distinct_sessions >= 2, (
            "sanity: promotion requires distinct_sessions >= 2"
        )


def test_scan_skips_deny_listed_patterns(tmp_db):
    for _ in range(5):
        _insert_tool_call(tmp_db, command="rm -rf /tmp/somepath", session_id="sess-A")
    for _ in range(5):
        _insert_tool_call(tmp_db, command="rm -rf /tmp/somepath", session_id="sess-B")

    processed = learner.scan_candidates(tmp_db)
    assert processed == 10

    assert _count_candidates(tmp_db, "rm") == 0


def test_active_pattern_is_excluded_from_proposals(tmp_db):
    for _ in range(3):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-A")
    for _ in range(2):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-B")
    learner.scan_candidates(tmp_db)

    # Resolve the command_shape_id for git status (no flags) and promote it
    # manually — v5 permission_active keys on the shape FK, not verb strings.
    shape_id = tmp_db.execute(
        """
        SELECT id FROM command_shapes
         WHERE verb = ? AND IFNULL(subcommand, '') = ? AND flags = ?;
        """,
        ("git", "status", _flags_json([])),
    ).fetchone()
    assert shape_id is not None
    tmp_db.execute(
        """
        INSERT INTO permission_active(command_shape_id, promoted_at, source)
        VALUES (?, '2026-04-20T00:00:00.000Z', 'manual');
        """,
        (shape_id[0],),
    )

    promotions = learner.propose_promotions(tmp_db)
    assert not any(
        c.verb == "git" and c.subcommand == "status" for c in promotions
    )


def test_scan_advances_cursor(tmp_db):
    _insert_tool_call(tmp_db, command="git status", session_id="sess-A")
    _insert_tool_call(tmp_db, command="git status", session_id="sess-A")
    learner.scan_candidates(tmp_db)

    first_cursor = tmp_db.execute(
        "SELECT last_processed_id FROM consumer_cursors WHERE consumer = ?;",
        (learner.CONSUMER_NAME,),
    ).fetchone()
    assert first_cursor is not None
    assert first_cursor[0] >= 2

    # A second scan with no new rows should return 0.
    processed = learner.scan_candidates(tmp_db)
    assert processed == 0


def test_scan_counts_each_leaf_in_compound_command(tmp_db):
    # `a && b` should count both leaves.
    for _ in range(3):
        _insert_tool_call(tmp_db, command="ls && pwd", session_id="sess-A")
    for _ in range(2):
        _insert_tool_call(tmp_db, command="ls && pwd", session_id="sess-B")
    learner.scan_candidates(tmp_db)

    ls_row = _candidate_row(tmp_db, "ls", None)
    pwd_row = _candidate_row(tmp_db, "pwd", None)
    assert ls_row is not None and ls_row[0] == 5
    assert pwd_row is not None and pwd_row[0] == 5


# ---------------------------------------------------------------------------
# Junction-table session-attribution regression tests (schema v4 fix,
# re-expressed against the v5 FK shape).
# ---------------------------------------------------------------------------


def test_propose_excludes_threshold_observations_single_session(tmp_db):
    # 5 rows, same shape, 1 session — meets observations, fails sessions.
    for _ in range(5):
        _insert_tool_call(tmp_db, command="echo hi", session_id="sess-only")
    learner.scan_candidates(tmp_db)

    promotions = learner.propose_promotions(tmp_db)
    assert not any(c.verb == "echo" for c in promotions)


def test_propose_excludes_below_observations_threshold_two_sessions(tmp_db):
    # 3 rows across 2 sessions — meets sessions, fails observations.
    for _ in range(2):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-A")
    for _ in range(1):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-B")
    learner.scan_candidates(tmp_db)

    promotions = learner.propose_promotions(tmp_db)
    # 3 < min_observations (5), so should be excluded regardless of sessions.
    assert not any(c.verb == "git" and c.subcommand == "status" for c in promotions)


def test_propose_includes_when_both_thresholds_met(tmp_db):
    # 5 rows, 2 sessions — both thresholds met, should promote.
    for _ in range(3):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-A")
    for _ in range(2):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-B")
    learner.scan_candidates(tmp_db)

    promotions = learner.propose_promotions(tmp_db)
    match = [c for c in promotions if c.verb == "git" and c.subcommand == "status"]
    assert len(match) == 1
    assert match[0].observations == 5
    assert match[0].distinct_sessions == 2


def test_propose_excludes_high_observations_single_session(tmp_db):
    # 10 observations but all in 1 session — confirms the junction is
    # doing the work, not the (pre-v4) LIKE heuristic.
    for _ in range(10):
        _insert_tool_call(tmp_db, command="ls -la", session_id="sess-solo")
    learner.scan_candidates(tmp_db)

    promotions = learner.propose_promotions(tmp_db)
    assert not any(c.verb == "ls" for c in promotions)


def test_distinct_session_count_is_per_shape_not_per_verb(tmp_db):
    # Stage a mix of shapes across sessions and verify each shape's
    # session count is isolated to its own junction rows.
    for _ in range(3):
        _insert_tool_call(tmp_db, command="ls -la", session_id="sess-A")
    _insert_tool_call(tmp_db, command="ls -la", session_id="sess-B")
    for _ in range(2):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-C")

    learner.scan_candidates(tmp_db)

    # Junction must hold exactly the rows for each shape — verify the
    # session count isn't polluted by unrelated rows. The v5 junction keys
    # on command_shape_id, so we resolve the shape id first.
    # Canonicalize splits bundled short flags: `-la` → {'-a', '-l'}.
    ls_shape = tmp_db.execute(
        """
        SELECT id FROM command_shapes
         WHERE verb = 'ls' AND subcommand IS NULL AND flags = ?;
        """,
        (_flags_json(["-a", "-l"]),),
    ).fetchone()
    git_shape = tmp_db.execute(
        """
        SELECT id FROM command_shapes
         WHERE verb = 'git' AND subcommand = 'status' AND flags = ?;
        """,
        (_flags_json([]),),
    ).fetchone()
    assert ls_shape is not None
    assert git_shape is not None

    sess_col = _pcs_session_column(tmp_db)
    ls_sessions = tmp_db.execute(
        f"""
        SELECT COUNT(DISTINCT {sess_col})
          FROM permission_candidate_sessions
         WHERE command_shape_id = ?;
        """,
        (ls_shape[0],),
    ).fetchone()[0]
    git_sessions = tmp_db.execute(
        f"""
        SELECT COUNT(DISTINCT {sess_col})
          FROM permission_candidate_sessions
         WHERE command_shape_id = ?;
        """,
        (git_shape[0],),
    ).fetchone()[0]
    assert ls_sessions == 2, f"ls should see 2 sessions, not {ls_sessions}"
    assert git_sessions == 1, f"git status should see 1 session, not {git_sessions}"

    # And propose_promotions, which reads through the subquery, agrees:
    # ls has 4 obs (below threshold), git has 2 obs + 1 session (below both).
    promotions = learner.propose_promotions(tmp_db)
    assert not any(c.verb == "ls" for c in promotions)
    assert not any(c.verb == "git" for c in promotions)


def test_junction_upsert_bumps_last_seen_not_duplicates(tmp_db):
    # Two scans of the same rows (via cursor reset) should not duplicate
    # junction rows — PK + ON CONFLICT upsert bumps last_seen.
    for _ in range(5):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-A")
    learner.scan_candidates(tmp_db)

    tmp_db.execute(
        "UPDATE consumer_cursors SET last_processed_id = 0 WHERE consumer = ?;",
        (learner.CONSUMER_NAME,),
    )
    learner.scan_candidates(tmp_db)

    count = tmp_db.execute(
        """
        SELECT COUNT(*) FROM permission_candidate_sessions pcs
          JOIN command_shapes cs ON cs.id = pcs.command_shape_id
         WHERE cs.verb = 'git' AND cs.subcommand = 'status';
        """
    ).fetchone()[0]
    assert count == 1, f"junction should have 1 row per (shape, session), not {count}"


def test_junction_handles_null_subcommand_shape(tmp_db):
    # Verb without a subcommand (e.g. `ls -la`) stores NULL in
    # command_shapes.subcommand. Re-observations must still upsert (bump
    # last_seen) and not insert duplicates.
    for _ in range(3):
        _insert_tool_call(tmp_db, command="ls -la", session_id="sess-A")
    learner.scan_candidates(tmp_db)

    tmp_db.execute(
        "UPDATE consumer_cursors SET last_processed_id = 0 WHERE consumer = ?;",
        (learner.CONSUMER_NAME,),
    )
    learner.scan_candidates(tmp_db)

    count = tmp_db.execute(
        """
        SELECT COUNT(*) FROM permission_candidate_sessions pcs
          JOIN command_shapes cs ON cs.id = pcs.command_shape_id
         WHERE cs.verb = 'ls' AND cs.subcommand IS NULL;
        """
    ).fetchone()[0]
    assert count == 1


def test_stored_distinct_sessions_reflects_junction_after_scan(tmp_db):
    # The stored column is a convenience for the CLI dump; verify it
    # mirrors the junction count after scan_candidates completes.
    for _ in range(3):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-A")
    for _ in range(2):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-B")
    for _ in range(1):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-C")
    learner.scan_candidates(tmp_db)

    row = _candidate_row(tmp_db, "git", "status")
    assert row is not None
    stored = row[1]
    assert stored == 3, f"expected 3 distinct sessions, got {stored}"


# ---------------------------------------------------------------------------
# v5 regression: permission_mode filtering.
#
# bypassPermissions and auto approvals are not user-driven, so rows carrying
# those modes must not become positive evidence. default (and NULL) must be
# retained. Status filtering must exclude anything that's not 'ok'.
# ---------------------------------------------------------------------------


def test_bypass_permissions_rows_excluded_from_candidates(tmp_db):
    # 10 rows of `git diff` under bypassPermissions — must not appear in
    # candidates at all (not even as a zero-observation row).
    for _ in range(10):
        _insert_tool_call(
            tmp_db,
            command="git diff",
            session_id="sess-A",
            permission_mode="bypassPermissions",
        )

    processed = learner.scan_candidates(tmp_db)
    # The SELECT skips these rows entirely, so scan_candidates returns 0.
    assert processed == 0
    assert _count_candidates(tmp_db, "git") == 0


def test_auto_permission_rows_excluded_from_candidates(tmp_db):
    for _ in range(10):
        _insert_tool_call(
            tmp_db,
            command="git diff",
            session_id="sess-A",
            permission_mode="auto",
        )

    processed = learner.scan_candidates(tmp_db)
    assert processed == 0
    assert _count_candidates(tmp_db, "git") == 0


def test_default_permission_mode_is_included(tmp_db):
    # Control case: 5 observations under 'default' across 2 sessions → promoted.
    for _ in range(3):
        _insert_tool_call(
            tmp_db,
            command="git diff",
            session_id="sess-A",
            permission_mode="default",
        )
    for _ in range(2):
        _insert_tool_call(
            tmp_db,
            command="git diff",
            session_id="sess-B",
            permission_mode="default",
        )

    processed = learner.scan_candidates(tmp_db)
    assert processed == 5
    row = _candidate_row(tmp_db, "git", "diff")
    assert row is not None
    assert row[0] == 5


def test_null_permission_mode_is_included(tmp_db):
    # Legacy rows / payloads without permission_mode store NULL — graceful
    # behaviour is to include them (the learner errs toward more evidence,
    # not less, when the payload shape is unexpected).
    for _ in range(3):
        _insert_tool_call(
            tmp_db,
            command="git diff",
            session_id="sess-A",
            permission_mode=None,
        )
    for _ in range(2):
        _insert_tool_call(
            tmp_db,
            command="git diff",
            session_id="sess-B",
            permission_mode=None,
        )

    processed = learner.scan_candidates(tmp_db)
    assert processed == 5
    row = _candidate_row(tmp_db, "git", "diff")
    assert row is not None
    assert row[0] == 5


# ---------------------------------------------------------------------------
# v5 regression: status_id filtering (legacy TEXT status is gone in v6).
# ---------------------------------------------------------------------------


def test_pending_status_rows_excluded_from_candidates(tmp_db):
    for _ in range(5):
        _insert_tool_call(
            tmp_db, command="git log", session_id="sess-A", status="pending"
        )

    processed = learner.scan_candidates(tmp_db)
    assert processed == 0
    assert _count_candidates(tmp_db, "git") == 0


def test_err_status_rows_excluded_from_candidates(tmp_db):
    for _ in range(5):
        _insert_tool_call(
            tmp_db, command="git log", session_id="sess-A", status="err"
        )

    processed = learner.scan_candidates(tmp_db)
    assert processed == 0
    assert _count_candidates(tmp_db, "git") == 0


def test_denied_status_rows_excluded_from_candidates(tmp_db):
    for _ in range(5):
        _insert_tool_call(
            tmp_db, command="git log", session_id="sess-A", status="denied"
        )

    processed = learner.scan_candidates(tmp_db)
    assert processed == 0
    assert _count_candidates(tmp_db, "git") == 0


# ---------------------------------------------------------------------------
# v5 regression: tool_call_shapes junction is populated by scan.
# ---------------------------------------------------------------------------


def test_tool_call_shapes_junction_populated_on_scan(tmp_db):
    # Single-leaf command → 1 junction row per tool_calls row.
    for _ in range(3):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-A")
    learner.scan_candidates(tmp_db)

    count = tmp_db.execute(
        "SELECT COUNT(*) FROM tool_call_shapes;"
    ).fetchone()[0]
    assert count == 3


def test_multi_leaf_command_produces_multi_row_junction(tmp_db):
    # `git status; echo done` has two leaves; each row should produce 2
    # junction entries. Verify against the actual canonicalizer output so
    # this stays honest if bashlex's parsing of ``;`` ever changes.
    leaves = learner.parse_command("git status; echo done")
    expected_leaves = len(leaves)
    assert expected_leaves >= 2, (
        "canonicalize should split `;` into at least 2 leaves"
    )

    _insert_tool_call(tmp_db, command="git status; echo done", session_id="sess-A")
    learner.scan_candidates(tmp_db)

    count = tmp_db.execute(
        "SELECT COUNT(*) FROM tool_call_shapes;"
    ).fetchone()[0]
    assert count == expected_leaves


def test_command_shapes_reused_not_duplicated_on_second_observation(tmp_db):
    # Two rows, same command → 1 command_shapes row, 2 junction rows.
    _insert_tool_call(tmp_db, command="git status", session_id="sess-A")
    _insert_tool_call(tmp_db, command="git status", session_id="sess-B")
    learner.scan_candidates(tmp_db)

    shapes = tmp_db.execute(
        "SELECT COUNT(*) FROM command_shapes WHERE verb = 'git';"
    ).fetchone()[0]
    junction = tmp_db.execute(
        """
        SELECT COUNT(*) FROM tool_call_shapes tcs
          JOIN command_shapes cs ON cs.id = tcs.command_shape_id
         WHERE cs.verb = 'git';
        """
    ).fetchone()[0]
    assert shapes == 1
    assert junction == 2


# ---------------------------------------------------------------------------
# v7 regression: tool_id filter and integer session_id.
#
# Post-v7 the learner reads `tool_calls.tool_id` (FK into `tools`) instead
# of the legacy TEXT `tool` column — non-Bash rows must be skipped even
# when their tool_id resolves through a different name. Post-v8 the
# junction's session_id is an integer.
# ---------------------------------------------------------------------------


def test_scan_filters_by_tool_id_not_text(tmp_db):
    # Seed 5 Bash rows + 5 non-Bash rows of each of several tools. Only
    # the Bash rows should feed the candidates table.
    for _ in range(3):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-A")
    for _ in range(2):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-B")

    # Non-Bash rows — different tool_ids, same command text. The learner
    # MUST NOT canonicalize these.
    for tool_name in ("Read", "Edit", "Write", "Grep"):
        for _ in range(3):
            _insert_tool_call(
                tmp_db, command="git status", session_id="sess-A", tool=tool_name
            )

    processed = learner.scan_candidates(tmp_db)
    # Only the 5 Bash rows pass the WHERE filter.
    assert processed == 5

    # The `git status` shape is seeded by the Bash rows only.
    row = _candidate_row(tmp_db, "git", "status")
    assert row is not None
    assert row[0] == 5


def test_scan_skips_when_bash_tool_unknown(tmp_db):
    # If the `tools` lookup has no row named 'Bash', the subquery resolves
    # to NULL and the WHERE clause matches nothing — the learner returns
    # 0 rather than crashing.
    # Insert rows under a non-Bash tool name so the Bash row never gets
    # registered in the lookup.
    for _ in range(5):
        _insert_tool_call(
            tmp_db, command="something", session_id="sess-A", tool="NotBash"
        )

    # Sanity: 'Bash' is not in the tools table.
    bash_row = tmp_db.execute(
        "SELECT id FROM tools WHERE name = 'Bash';"
    ).fetchone()
    assert bash_row is None

    processed = learner.scan_candidates(tmp_db)
    assert processed == 0


def test_junction_session_id_is_integer_after_scan(tmp_db):
    # The recorder writes an INTEGER session_id; the learner reads it and
    # stores it on the junction as-is. Post-v8 the junction column is
    # named `session_id` (INTEGER); pre-v8 it's `session_id_new`. In
    # either case ``typeof`` must be 'integer'.
    for _ in range(3):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-A")
    learner.scan_candidates(tmp_db)

    sess_col = _pcs_session_column(tmp_db)
    type_row = tmp_db.execute(
        f"SELECT typeof({sess_col}) FROM permission_candidate_sessions LIMIT 1;"
    ).fetchone()
    assert type_row is not None
    assert type_row[0] == "integer", (
        f"junction session_id must be INTEGER post-v7, got {type_row[0]!r}"
    )

    # Sanity: the stored integer maps back to the original UUID via sessions.
    joined = tmp_db.execute(
        f"""
        SELECT s.session_uuid
          FROM permission_candidate_sessions pcs
          JOIN sessions s ON s.id = pcs.{sess_col}
         LIMIT 1;
        """
    ).fetchone()
    assert joined is not None
    assert joined[0] == "sess-A"


def test_junction_session_id_matches_upserted_id(tmp_db):
    # The INTEGER session_id stored on the junction must equal the id
    # returned by _upsert_session for the same UUID — proving the learner
    # is passing through the recorder's integer FK directly, not deriving
    # a second one.
    for _ in range(3):
        _insert_tool_call(tmp_db, command="git status", session_id="sess-A")
    learner.scan_candidates(tmp_db)

    expected_id = _session_int_id(tmp_db, "sess-A")
    sess_col = _pcs_session_column(tmp_db)
    row = tmp_db.execute(
        f"SELECT {sess_col} FROM permission_candidate_sessions LIMIT 1;"
    ).fetchone()
    assert row is not None
    assert int(row[0]) == expected_id


# ---------------------------------------------------------------------------
# Rejection persistence (v9+)
# ---------------------------------------------------------------------------


def _shape_id(conn, verb: str, subcommand: str | None = None, flags: list[str] | None = None) -> int | None:
    row = conn.execute(
        """
        SELECT id FROM command_shapes
         WHERE verb = ? AND IFNULL(subcommand, '') = IFNULL(?, '') AND flags = ?;
        """,
        (verb, subcommand, _flags_json(flags or [])),
    ).fetchone()
    return int(row[0]) if row else None


def _reject_shape(conn, verb: str, subcommand: str | None = None, flags: list[str] | None = None) -> None:
    shape_id = _shape_id(conn, verb, subcommand, flags)
    assert shape_id is not None, "must scan before rejecting"
    conn.execute(
        "INSERT OR REPLACE INTO permission_rejected "
        "(command_shape_id, rejected_at, reason) VALUES (?, ?, NULL);",
        (shape_id, "2026-04-20T10:00:00.000Z"),
    )
    conn.execute(
        "DELETE FROM permission_candidate_sessions WHERE command_shape_id=?;",
        (shape_id,),
    )
    conn.execute(
        "DELETE FROM permission_candidates WHERE command_shape_id=?;",
        (shape_id,),
    )
    conn.commit()


def test_rejected_shape_not_repopulated_on_rescan(tmp_db):
    # Observe enough times to create a candidate, reject it, then keep
    # observing the same shape. The rejected row must not reappear in
    # permission_candidates no matter how often the learner re-scans.
    for i in range(3):
        _insert_tool_call(
            tmp_db, command="ls /tmp", session_id=f"sess-{i}"
        )
    learner.scan_candidates(tmp_db)
    assert _candidate_row(tmp_db, "ls", None) is not None

    _reject_shape(tmp_db, "ls")
    assert _candidate_row(tmp_db, "ls", None) is None

    # Further observations — shape is still in tool_call_shapes history
    # but candidate must stay empty.
    for i in range(5):
        _insert_tool_call(
            tmp_db, command="ls /tmp", session_id=f"later-{i}"
        )
    learner.scan_candidates(tmp_db)
    assert _candidate_row(tmp_db, "ls", None) is None

    # The tool_call_shapes junction should still be populated — we want
    # the historical trace even for rejected shapes.
    shape_id = _shape_id(tmp_db, "ls")
    junction_count = tmp_db.execute(
        "SELECT COUNT(*) FROM tool_call_shapes WHERE command_shape_id = ?;",
        (shape_id,),
    ).fetchone()[0]
    assert junction_count == 8  # 3 pre-reject + 5 post-reject observations


def test_rejected_shape_excluded_from_propose(tmp_db):
    # Defence-in-depth: even if a candidate row somehow existed alongside a
    # rejection (e.g. race, direct DB poke), propose_promotions must still
    # exclude it.
    for i in range(6):
        _insert_tool_call(
            tmp_db, command="ls /tmp", session_id=f"sess-{i % 3}"
        )
    learner.scan_candidates(tmp_db)
    # Rejection without the candidate purge, to simulate the race.
    shape_id = _shape_id(tmp_db, "ls")
    tmp_db.execute(
        "INSERT INTO permission_rejected "
        "(command_shape_id, rejected_at, reason) VALUES (?, ?, NULL);",
        (shape_id, "2026-04-20T10:00:00.000Z"),
    )
    tmp_db.commit()

    proposals = learner.propose_promotions(tmp_db)
    assert all(p.verb != "ls" for p in proposals)


def test_unreject_reopens_candidate_flow(tmp_db):
    for i in range(3):
        _insert_tool_call(tmp_db, command="ls /tmp", session_id=f"s-{i}")
    learner.scan_candidates(tmp_db)
    _reject_shape(tmp_db, "ls")

    # Un-reject — simulate the CLI by deleting from permission_rejected.
    shape_id = _shape_id(tmp_db, "ls")
    tmp_db.execute(
        "DELETE FROM permission_rejected WHERE command_shape_id=?;", (shape_id,)
    )
    tmp_db.commit()

    # New observations should now count again.
    for i in range(4):
        _insert_tool_call(tmp_db, command="ls /tmp", session_id=f"post-{i}")
    learner.scan_candidates(tmp_db)
    row = _candidate_row(tmp_db, "ls", None)
    assert row is not None
    assert int(row[0]) == 4
