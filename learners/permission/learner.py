"""Offline candidate generation and promotion proposal.

Run from the observability root so the ``learners.permission`` package is
importable:

    cd /home/steve/.claude/observability
    .venv/bin/python -m learners.permission.learner scan
    .venv/bin/python -m learners.permission.learner candidates
    .venv/bin/python -m learners.permission.learner active

The ``scan`` subcommand walks all new Bash rows in ``tool_calls`` past our
cursor, canonicalizes them, drops any that match the deny-list, and upserts
the rest into ``permission_candidates`` (keyed by ``command_shape_id`` after
the v5 schema refactor). ``candidates`` and ``active`` dump the contents of
the two learner tables for inspection, joining through ``command_shapes`` to
render the verb/subcommand/flags fields.

A separate ``review`` UX (Phase 4, not wired here) consumes
:func:`propose_promotions` to offer confirmations.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

# tomllib is stdlib on 3.11+; fall back to `tomli` if a caller ever runs
# this on 3.10. The shared venv pins Python to whatever uv provides, which
# is currently 3.11+, so the fallback path is belt-and-braces.
try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib  # type: ignore[import-not-found]

# Allow invocation as `python learner.py` without -m (used by a couple of
# legacy entrypoints). When imported as a package, __package__ is set and
# relative imports work; when run as a script directly, pad sys.path so
# the package is resolvable.
if __package__ in (None, ""):  # pragma: no cover - script-entry fallback
    sys.path.insert(0, "/home/steve/.claude/observability")
    from learners.permission import canonicalize as _canonicalize_mod
    from learners.permission import deny as _deny_mod

    parse_command = _canonicalize_mod.parse_command
    is_denied = _deny_mod.is_denied
    CanonicalLeaf = _canonicalize_mod.CanonicalLeaf
else:
    from .canonicalize import CanonicalLeaf, parse_command
    from .deny import is_denied


CONSUMER_NAME = "permission-learner"

_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "learner.toml"


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Thresholds:
    min_observations: int
    min_distinct_sessions: int
    promotion_window_days: int


def load_thresholds() -> Thresholds:
    """Read ``config/learner.toml`` and coerce into a Thresholds dataclass."""
    with _CONFIG_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    return Thresholds(
        min_observations=int(data.get("min_observations", 5)),
        min_distinct_sessions=int(data.get("min_distinct_sessions", 2)),
        promotion_window_days=int(data.get("promotion_window_days", 30)),
    )


# ---------------------------------------------------------------------------
# lib.db helpers — imported lazily (see _connect) and used by scan_candidates
# ---------------------------------------------------------------------------


def _lib_db():
    """Return the ``lib.db`` module, injecting the observability root in sys.path.

    Kept behind a function so tests that reload ``lib.db`` under a patched
    ``OBSERVABILITY_DB`` still see the right module — each call resolves the
    currently loaded module.
    """
    sys.path.insert(0, "/home/steve/.claude/observability")
    import lib.db as _db  # noqa: E402

    return _db


# ---------------------------------------------------------------------------
# Candidate scanning
# ---------------------------------------------------------------------------


def _now() -> str:
    """Timestamp helper matching lib.db._now() format."""
    import datetime as _dt

    return (
        _dt.datetime.now(tz=_dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _get_cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT last_processed_id FROM consumer_cursors WHERE consumer = ?;",
        (CONSUMER_NAME,),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _set_cursor(conn: sqlite3.Connection, last_id: int) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO consumer_cursors(consumer, last_processed_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(consumer) DO UPDATE SET
          last_processed_id = excluded.last_processed_id,
          updated_at = excluded.updated_at;
        """,
        (CONSUMER_NAME, last_id, now),
    )


def scan_candidates(conn: sqlite3.Connection) -> int:
    """Walk new Bash rows past the cursor and upsert canonical candidates.

    Returns the number of ``tool_calls`` rows processed this scan.

    Each row is canonicalized; every surviving (non-denied) leaf is linked to
    its ``command_shape`` (upserted if new) via the ``tool_call_shapes``
    junction, and bumps ``permission_candidates`` observation counts plus the
    ``permission_candidate_sessions`` junction. ``distinct_sessions`` is
    refreshed from the session junction at the end of the scan.

    The SELECT excludes rows whose ``permission_mode`` is ``bypassPermissions``
    or ``auto`` — those approvals weren't user-driven, so they're not positive
    evidence for "safe to auto-approve". Status is filtered via ``status_id``
    (the int-FK column introduced in v5); the legacy TEXT ``status`` column is
    dropped in v6 and must not be referenced. Tool filtering likewise goes
    through ``tool_id`` and the ``tools`` lookup — the legacy TEXT ``tool``
    column is dropped in v8. ``tc.session_id`` is an INTEGER FK into
    ``sessions(id)`` post-v8, so it's read straight through and stored on
    ``permission_candidate_sessions.session_id`` (also INTEGER in v8).
    """
    db = _lib_db()

    cursor = _get_cursor(conn)
    rows = conn.execute(
        """
        SELECT tc.id, tc.session_id, tc.command
          FROM tool_calls tc
         WHERE tc.tool_id = (SELECT id FROM tools WHERE name = 'Bash')
           AND tc.status_id = (SELECT id FROM call_statuses WHERE name = 'ok')
           AND tc.id > ?
           AND tc.command IS NOT NULL
           AND (
             tc.permission_mode_id IS NULL
             OR tc.permission_mode_id NOT IN (
               SELECT id FROM permission_modes
                WHERE name IN ('bypassPermissions', 'auto')
             )
           )
         ORDER BY tc.id ASC;
        """,
        (cursor,),
    ).fetchall()

    if not rows:
        return 0

    max_id = cursor
    # Track shape ids touched this scan so we can refresh distinct_sessions
    # once per shape at the end rather than per observation.
    touched_shape_ids: set[int] = set()
    now = _now()

    for row_id, session_id, command in rows:
        max_id = max(max_id, int(row_id))
        if not isinstance(command, str) or not command:
            continue
        try:
            leaves = parse_command(command)
        except Exception:  # noqa: BLE001 — canonicalize is defensive already.
            continue

        for leaf_index, leaf in enumerate(leaves):
            denied, _reason = is_denied(leaf)
            if denied:
                # Never promote denied shapes — also means we don't create a
                # command_shape row or junction entry for them. The hook still
                # evaluates the deny-list at runtime, so the shape registry
                # doesn't need to know about them.
                continue

            flags_json = db.minify_json(sorted(leaf.flags))
            shape_id = db.upsert_command_shape(
                conn,
                leaf.verb,
                leaf.subcommand,
                flags_json,
                now,
            )
            db.link_tool_call_shape(conn, int(row_id), shape_id, leaf_index)

            # If the user has explicitly rejected this shape, preserve the
            # tool_call_shapes history (already linked above) but skip the
            # candidate upsert — the shape is dead to the promotion pipeline.
            if _is_rejected(conn, shape_id):
                continue

            _upsert_candidate(conn, shape_id=shape_id, first_seen=now, last_seen=now)
            # Rows without a session_id (NULL FK) are dropped — legacy rows
            # predate per-session attribution and recording them would
            # reintroduce the miscount the junction exists to prevent.
            if session_id is not None:
                _upsert_candidate_session(
                    conn,
                    shape_id=shape_id,
                    session_id=int(session_id),
                    last_seen=now,
                )
            touched_shape_ids.add(shape_id)

    # Refresh the stored distinct_sessions column on every shape touched
    # this scan so the `candidates` CLI output stays accurate. The promotion
    # path reads the junction directly and does not trust this column.
    for shape_id in touched_shape_ids:
        _refresh_distinct_sessions(conn, shape_id=shape_id)

    _set_cursor(conn, max_id)
    return len(rows)


def _upsert_candidate(
    conn: sqlite3.Connection,
    *,
    shape_id: int,
    first_seen: str,
    last_seen: str,
) -> None:
    """Insert or bump a candidate row keyed by ``command_shape_id``.

    Increments ``observations`` by 1 per call. ``distinct_sessions`` is NOT
    computed here — it's refreshed from the ``permission_candidate_sessions``
    junction at the end of each scan (:func:`_refresh_distinct_sessions`) so
    the CLI dump stays accurate, and read directly from the junction by the
    promotion query so threshold decisions never trust a stale stored value.
    """
    existing = conn.execute(
        """
        SELECT observations FROM permission_candidates
         WHERE command_shape_id = ?;
        """,
        (shape_id,),
    ).fetchone()

    if existing is None:
        conn.execute(
            """
            INSERT INTO permission_candidates
              (command_shape_id, observations, distinct_sessions,
               first_seen, last_seen)
            VALUES (?, 1, 0, ?, ?);
            """,
            (shape_id, first_seen, last_seen),
        )
        return

    prev_obs = existing[0]
    conn.execute(
        """
        UPDATE permission_candidates
           SET observations = ?,
               last_seen = ?
         WHERE command_shape_id = ?;
        """,
        (int(prev_obs) + 1, last_seen, shape_id),
    )


def _upsert_candidate_session(
    conn: sqlite3.Connection,
    *,
    shape_id: int,
    session_id: int,
    last_seen: str,
) -> None:
    """Insert-or-bump a junction row for (shape, session).

    Primary key is ``(command_shape_id, session_id)`` — a conflict means the
    session already observed this shape and we just bump ``last_seen``.
    ``session_id`` is INTEGER FK into ``sessions(id)`` post-v8 (was TEXT UUID
    before v7; the v7/v8 migration flipped it to the numeric PK on sessions).
    """
    conn.execute(
        """
        INSERT INTO permission_candidate_sessions
          (command_shape_id, session_id, last_seen)
        VALUES (?, ?, ?)
        ON CONFLICT(command_shape_id, session_id)
        DO UPDATE SET last_seen = excluded.last_seen;
        """,
        (shape_id, session_id, last_seen),
    )


def _is_rejected(conn: sqlite3.Connection, shape_id: int) -> bool:
    """True if the user has previously declined to promote this shape."""
    return (
        conn.execute(
            "SELECT 1 FROM permission_rejected WHERE command_shape_id = ?;",
            (shape_id,),
        ).fetchone()
        is not None
    )


def _refresh_distinct_sessions(
    conn: sqlite3.Connection, *, shape_id: int
) -> None:
    """Recompute the stored distinct_sessions column from the junction."""
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT session_id)
          FROM permission_candidate_sessions
         WHERE command_shape_id = ?;
        """,
        (shape_id,),
    ).fetchone()
    count = int(row[0]) if row else 0
    conn.execute(
        """
        UPDATE permission_candidates
           SET distinct_sessions = ?
         WHERE command_shape_id = ?;
        """,
        (count, shape_id),
    )


# ---------------------------------------------------------------------------
# Promotion proposals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    verb: str
    subcommand: str | None
    flags: frozenset[str]
    observations: int
    distinct_sessions: int


def propose_promotions(conn: sqlite3.Connection) -> list[Candidate]:
    """Return candidates that meet thresholds and are not yet active.

    Joins ``permission_candidates`` → ``command_shapes`` so the output still
    exposes verb/subcommand/flags, and LEFT JOINs ``permission_active`` on
    ``command_shape_id`` to exclude already-promoted shapes.
    """
    thresholds = load_thresholds()
    rows = conn.execute(
        """
        SELECT cs.verb, cs.subcommand, cs.flags,
               c.observations, c.distinct_sessions
          FROM permission_candidates c
          JOIN command_shapes cs ON cs.id = c.command_shape_id
     LEFT JOIN permission_active a ON a.command_shape_id = c.command_shape_id
     LEFT JOIN permission_rejected r ON r.command_shape_id = c.command_shape_id
         WHERE a.command_shape_id IS NULL
           AND r.command_shape_id IS NULL
           AND c.observations >= ?
           AND c.distinct_sessions >= ?
         ORDER BY c.observations DESC, cs.verb, cs.subcommand;
        """,
        (thresholds.min_observations, thresholds.min_distinct_sessions),
    ).fetchall()

    out: list[Candidate] = []
    for verb, subcommand, flags_json, obs, sess in rows:
        try:
            flag_list = json.loads(flags_json)
        except (json.JSONDecodeError, TypeError):
            flag_list = []
        out.append(
            Candidate(
                verb=verb,
                subcommand=subcommand,
                flags=frozenset(flag_list),
                observations=int(obs),
                distinct_sessions=int(sess),
            )
        )
    return out


# ---------------------------------------------------------------------------
# DB connection helper
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    """Open the observations DB with migrations applied.

    Kept local so tests can substitute the lower-level ``_open``/``_migrate``
    pair and this module does not fight test fixtures over DB path env vars.
    """
    # Import lazily so test fixtures that override DB_PATH via env var still
    # work — lib.db resolves DB_PATH at import time.
    db = _lib_db()
    conn = db._open()
    db._migrate(conn)
    return conn


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _format_flags(flags: frozenset[str]) -> str:
    return " ".join(sorted(flags)) if flags else "-"


def _cmd_scan(_args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        processed = scan_candidates(conn)
        proposals = propose_promotions(conn)
    finally:
        conn.close()

    print(f"scanned {processed} tool_call rows past cursor")
    if not proposals:
        print("no promotion candidates meet thresholds yet")
        return 0
    print(f"{len(proposals)} candidate(s) eligible for promotion:")
    for c in proposals:
        sub = c.subcommand or "-"
        print(
            f"  {c.verb:<10} {sub:<15} flags=[{_format_flags(c.flags)}] "
            f"obs={c.observations} sessions={c.distinct_sessions}"
        )
    return 0


def _cmd_candidates(_args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT cs.verb, cs.subcommand, cs.flags,
                   c.observations, c.distinct_sessions,
                   c.first_seen, c.last_seen
              FROM permission_candidates c
              JOIN command_shapes cs ON cs.id = c.command_shape_id
             ORDER BY c.last_seen DESC;
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("permission_candidates is empty")
        return 0
    for verb, subcommand, flags_json, obs, sess, first_seen, last_seen in rows:
        sub = subcommand or "-"
        try:
            flags = json.loads(flags_json)
        except (json.JSONDecodeError, TypeError):
            flags = []
        print(
            f"  {verb:<10} {sub:<15} flags={flags} "
            f"obs={obs} sessions={sess} first={first_seen} last={last_seen}"
        )
    return 0


def _parse_flags_arg(raw: str | None) -> str:
    """Parse a ``--flags`` CLI argument into the stored flags-json form.

    The argument is a JSON array literal (``'["-a","-l"]'`` or ``'[]'``). We
    decode it and re-encode with :func:`lib.db.minify_json` so the byte-exact
    form matches ``command_shapes.flags`` regardless of how the user typed the
    whitespace. Missing/``None`` is treated as ``[]`` so promoting a bare verb
    still works.
    """
    db = _lib_db()
    if raw is None or raw == "":
        return db.minify_json([])
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(
            f"error: --flags must be a JSON array literal (e.g. '[\"-a\",\"-l\"]'): {exc}"
        )
    if not isinstance(parsed, list):
        raise SystemExit("error: --flags must be a JSON array literal, e.g. '[]'")
    return db.minify_json(sorted(str(x) for x in parsed))


def _resolve_shape_id(
    conn: sqlite3.Connection, verb: str, subcommand: str | None, flags_json: str
) -> int | None:
    """Look up a ``command_shape`` id matching the given tuple, or None.

    Uses the same ``IFNULL(subcommand, '')`` NULL-normalization as the
    ``command_shapes`` UNIQUE index so ``--subcommand`` omitted and explicit
    empty both match a shape stored with NULL subcommand.
    """
    row = conn.execute(
        """
        SELECT id FROM command_shapes
         WHERE verb = ?
           AND IFNULL(subcommand, '') = IFNULL(?, '')
           AND flags = ?;
        """,
        (verb, subcommand, flags_json),
    ).fetchone()
    return int(row[0]) if row is not None else None


def _format_cli_flags(flags_json: str) -> str:
    """Render a stored flags-json blob as a compact list for CLI output."""
    try:
        return str(json.loads(flags_json))
    except (json.JSONDecodeError, TypeError):
        return flags_json


def _cmd_promote(args: argparse.Namespace) -> int:
    """Promote a candidate (by shape tuple) into ``permission_active``.

    ``INSERT OR IGNORE`` so re-promoting is idempotent. Does NOT delete the
    candidate row — the ``propose_promotions`` view LEFT-JOINs through
    ``permission_active`` and will stop surfacing it once it's active.
    """
    flags_json = _parse_flags_arg(args.flags)
    conn = _connect()
    try:
        shape_id = _resolve_shape_id(conn, args.verb, args.subcommand, flags_json)
        if shape_id is None:
            print(
                f"error: no matching command_shape for verb={args.verb!r} "
                f"subcommand={args.subcommand!r} flags={_format_cli_flags(flags_json)}",
                file=sys.stderr,
            )
            return 1
        conn.execute(
            """
            INSERT OR IGNORE INTO permission_active
              (command_shape_id, promoted_at, source)
            VALUES (?, ?, 'manual');
            """,
            (shape_id, _now()),
        )
    finally:
        conn.close()

    sub = args.subcommand or "-"
    print(
        f"promoted: {args.verb} {sub} flags={_format_cli_flags(flags_json)}"
    )
    return 0


def _cmd_reject(args: argparse.Namespace) -> int:
    """Persist a shape as rejected + purge its candidate row.

    Writes to ``permission_rejected`` so future scans never re-propose this
    shape (see :func:`_is_rejected` in :func:`scan_candidates` and the LEFT
    JOIN guard in :func:`propose_promotions`). Also deletes any existing
    candidate + session-junction rows so the ``candidates`` dump doesn't
    keep showing a dead row.

    ``INSERT OR REPLACE`` on the rejected table lets a re-reject update the
    timestamp and reason without failing on the PK conflict.
    """
    flags_json = _parse_flags_arg(args.flags)
    conn = _connect()
    try:
        shape_id = _resolve_shape_id(conn, args.verb, args.subcommand, flags_json)
        if shape_id is None:
            print(
                f"error: no matching command_shape for verb={args.verb!r} "
                f"subcommand={args.subcommand!r} flags={_format_cli_flags(flags_json)}",
                file=sys.stderr,
            )
            return 1
        conn.execute(
            """
            INSERT OR REPLACE INTO permission_rejected
              (command_shape_id, rejected_at, reason)
            VALUES (?, ?, ?);
            """,
            (shape_id, _now(), args.reason),
        )
        session_cur = conn.execute(
            "DELETE FROM permission_candidate_sessions WHERE command_shape_id = ?;",
            (shape_id,),
        )
        session_rows = session_cur.rowcount or 0
        cand_cur = conn.execute(
            "DELETE FROM permission_candidates WHERE command_shape_id = ?;",
            (shape_id,),
        )
        cand_rows = cand_cur.rowcount or 0
    finally:
        conn.close()

    sub = args.subcommand or "-"
    reason_part = f" reason={args.reason!r}" if args.reason else ""
    print(
        f"rejected: {args.verb} {sub} flags={_format_cli_flags(flags_json)}"
        f"{reason_part} (purged {cand_rows} candidate, {session_rows} session row(s))"
    )
    return 0


def _cmd_unreject(args: argparse.Namespace) -> int:
    """Remove a shape's entry from ``permission_rejected``.

    Future scans will resume accumulating observation counts for this shape,
    and it becomes eligible for promotion once thresholds are met again.
    """
    flags_json = _parse_flags_arg(args.flags)
    conn = _connect()
    try:
        shape_id = _resolve_shape_id(conn, args.verb, args.subcommand, flags_json)
        if shape_id is None:
            print(
                f"error: no matching command_shape for verb={args.verb!r} "
                f"subcommand={args.subcommand!r} flags={_format_cli_flags(flags_json)}",
                file=sys.stderr,
            )
            return 1
        cur = conn.execute(
            "DELETE FROM permission_rejected WHERE command_shape_id = ?;",
            (shape_id,),
        )
        rows = cur.rowcount or 0
    finally:
        conn.close()

    sub = args.subcommand or "-"
    if rows == 0:
        print(
            f"no matching rejected entry for verb={args.verb!r} "
            f"subcommand={args.subcommand!r} flags={_format_cli_flags(flags_json)}"
        )
        return 0
    print(
        f"unrejected: {args.verb} {sub} flags={_format_cli_flags(flags_json)}"
    )
    return 0


def _cmd_rejected(_args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT cs.verb, cs.subcommand, cs.flags,
                   r.rejected_at, r.reason
              FROM permission_rejected r
              JOIN command_shapes cs ON cs.id = r.command_shape_id
             ORDER BY r.rejected_at DESC;
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("permission_rejected is empty")
        return 0
    for verb, subcommand, flags_json, rejected_at, reason in rows:
        sub = subcommand or "-"
        try:
            flags = json.loads(flags_json)
        except (json.JSONDecodeError, TypeError):
            flags = []
        reason_part = f" reason={reason!r}" if reason else ""
        print(
            f"  {verb:<10} {sub:<15} flags={flags} "
            f"rejected_at={rejected_at}{reason_part}"
        )
    return 0


def _cmd_propose(_args: argparse.Namespace) -> int:
    """Emit eligible promotions as pipe-delimited records for bash consumers.

    One line per candidate: ``verb|subcommand-or-empty|flags-json|obs|sessions``.
    The review.sh script parses this to drive interactive promote/reject
    prompts; keeping it a flat text format (not JSON) avoids a jq dependency
    in the shell layer.

    ``|`` (not ``\\t``) is the separator because bash's ``read`` with
    ``IFS=$'\\t'`` collapses consecutive tabs — whitespace IFS is treated
    specially — so an empty subcommand field would vanish and shift the
    remaining fields. ``|`` is non-whitespace, so ``IFS=|`` preserves empties.
    The pipe is safe here: flags are JSON-array literals (square brackets,
    quoted strings, commas) and the canonicalizer strips shell metacharacters
    from verbs/subcommands long before they reach this output.

    Empty second field represents NULL subcommand (matches the
    ``IFNULL(subcommand, '')`` convention used elsewhere).
    """
    conn = _connect()
    try:
        proposals = propose_promotions(conn)
    finally:
        conn.close()

    db = _lib_db()
    for c in proposals:
        sub = c.subcommand or ""
        flags_json = db.minify_json(sorted(c.flags))
        print(
            f"{c.verb}|{sub}|{flags_json}|{c.observations}|{c.distinct_sessions}"
        )
    return 0


def _cmd_active(_args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT cs.verb, cs.subcommand, cs.flags,
                   a.promoted_at, a.source
              FROM permission_active a
              JOIN command_shapes cs ON cs.id = a.command_shape_id
             ORDER BY a.promoted_at DESC;
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("permission_active is empty")
        return 0
    for verb, subcommand, flags_json, promoted_at, source in rows:
        sub = subcommand or "-"
        try:
            flags = json.loads(flags_json)
        except (json.JSONDecodeError, TypeError):
            flags = []
        print(
            f"  {verb:<10} {sub:<15} flags={flags} "
            f"source={source} promoted_at={promoted_at}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="learners.permission.learner",
        description="Permission learner — scan, list candidates, list active.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="Scan new Bash rows and propose promotions.")
    sub.add_parser("candidates", help="Dump permission_candidates table.")
    sub.add_parser("active", help="Dump permission_active table.")
    sub.add_parser(
        "propose",
        help="Emit eligible promotions as TSV lines (for review.sh).",
    )

    promote = sub.add_parser(
        "promote",
        help="Graduate a candidate shape into permission_active (manual source).",
    )
    promote.add_argument("--verb", required=True, help="Command verb, e.g. 'head'.")
    promote.add_argument(
        "--subcommand", default=None,
        help="Subcommand (omit for verbs with no subcommand; matches NULL).",
    )
    promote.add_argument(
        "--flags", default=None,
        help="JSON array literal of flags, e.g. '[\"-a\",\"-l\"]' or '[]'.",
    )

    reject = sub.add_parser(
        "reject",
        help="Persist a shape as rejected; future scans won't re-propose it.",
    )
    reject.add_argument("--verb", required=True, help="Command verb, e.g. 'head'.")
    reject.add_argument(
        "--subcommand", default=None,
        help="Subcommand (omit for verbs with no subcommand; matches NULL).",
    )
    reject.add_argument(
        "--flags", default=None,
        help="JSON array literal of flags, e.g. '[\"-a\",\"-l\"]' or '[]'.",
    )
    reject.add_argument(
        "--reason", default=None,
        help="Optional free-text reason stored with the rejection.",
    )

    unreject = sub.add_parser(
        "unreject",
        help="Remove a shape from permission_rejected; re-enables observation.",
    )
    unreject.add_argument("--verb", required=True, help="Command verb, e.g. 'head'.")
    unreject.add_argument(
        "--subcommand", default=None,
        help="Subcommand (omit for verbs with no subcommand; matches NULL).",
    )
    unreject.add_argument(
        "--flags", default=None,
        help="JSON array literal of flags, e.g. '[\"-a\",\"-l\"]' or '[]'.",
    )

    sub.add_parser("rejected", help="Dump permission_rejected table.")

    args = parser.parse_args(argv)
    if args.cmd == "scan":
        return _cmd_scan(args)
    if args.cmd == "candidates":
        return _cmd_candidates(args)
    if args.cmd == "active":
        return _cmd_active(args)
    if args.cmd == "propose":
        return _cmd_propose(args)
    if args.cmd == "promote":
        return _cmd_promote(args)
    if args.cmd == "reject":
        return _cmd_reject(args)
    if args.cmd == "unreject":
        return _cmd_unreject(args)
    if args.cmd == "rejected":
        return _cmd_rejected(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
