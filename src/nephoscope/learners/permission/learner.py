"""Permission learner.

Run from the observability root so the ``learners.permission`` package is
importable:

    cd ~/.claude/observability
    .venv/bin/python -m learners.permission.learner scan
    .venv/bin/python -m learners.permission.learner candidates
    .venv/bin/python -m learners.permission.learner permissions

The ``scan`` subcommand walks all new Bash rows in ``tool_calls`` past the
consumer cursor, canonicalizes them, and upserts results directly into
``permission_candidates`` (verb/subcommand/flags inline — no ``command_shapes``
join).  The deny filter is NOT applied at scan time; it runs at propose time
so every observed command accumulates evidence regardless of deny-list status.

``propose`` emits eligible candidates as pipe-delimited lines for review.sh.
``promote`` / ``reject`` upsert a ``rule_shape`` and INSERT a ``permissions``
row.  ``unpermit`` deletes a matching permissions row.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

# tomllib is stdlib on 3.11+; fall back to tomli on 3.10.
try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found]

from nephoscope.learners.permission.canonicalize import CanonicalLeaf, parse_command
from nephoscope.learners.permission.deny import evaluate

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
    """Read config/learner.toml and coerce into a Thresholds dataclass."""
    with _CONFIG_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    return Thresholds(
        min_observations=int(data.get("min_observations", 5)),
        min_distinct_sessions=int(data.get("min_distinct_sessions", 2)),
        promotion_window_days=int(data.get("promotion_window_days", 30)),
    )


# ---------------------------------------------------------------------------
# lib.db access
# ---------------------------------------------------------------------------


def _lib_db():
    """Return the lib.db module.

    Kept behind a function so tests can reload lib.db under a patched
    OBSERVABILITY_DB and still see the right module — each call resolves
    the currently loaded module.
    """
    import nephoscope.lib.db as _db

    return _db


def _now() -> str:
    """UTC timestamp — thin wrapper over lib.db._now()."""
    return _lib_db()._now()


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Candidate scanning
# ---------------------------------------------------------------------------


def scan_candidates(conn: sqlite3.Connection) -> int:
    """Walk new Bash rows past the cursor; upsert permission_candidates inline.

    Deny filter is NOT applied here — every observed command accumulates
    evidence. The deny check happens at propose time only.

    Rows without a session_id are skipped — without a session we cannot
    track distinct_sessions, which is the primary de-noise signal.

    Returns the number of tool_calls rows processed this scan.
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
    now = _now()

    for row_id, session_id, command in rows:
        max_id = max(max_id, int(row_id))
        if not isinstance(command, str) or not command:
            continue
        if session_id is None:
            # Rows without session attribution cannot contribute to
            # distinct_sessions tracking — skip the upsert entirely.
            continue
        try:
            leaves = parse_command(command)
        except Exception:  # noqa: BLE001 — canonicalize is defensive
            continue

        for leaf in leaves:
            flags_json = db.minify_json(sorted(leaf.flags))
            db.upsert_candidate(
                conn,
                leaf.verb,
                leaf.subcommand,
                flags_json,
                int(session_id),
                now,
            )

    _set_cursor(conn, max_id)
    return len(rows)


# ---------------------------------------------------------------------------
# Promotion proposals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    id: int
    verb: str
    subcommand: str | None
    flags: frozenset[str]
    observations: int
    distinct_sessions: int


def _candidate_leaf(
    verb: str, subcommand: str | None, flags_json: str
) -> CanonicalLeaf:
    """Reconstruct a minimal CanonicalLeaf from stored candidate fields.

    Used to run the deny-filter at propose time without re-parsing the original
    command — the canonical shape (verb, subcommand, flags) is sufficient.
    """
    try:
        flags_list = json.loads(flags_json)
    except (json.JSONDecodeError, TypeError):
        flags_list = []
    return CanonicalLeaf(
        verb=verb,
        subcommand=subcommand,
        flags=frozenset(flags_list),
        redirections=(),
        raw_leaf=verb,
    )


def propose_promotions(conn: sqlite3.Connection) -> list[Candidate]:
    """Return candidates meeting thresholds, not yet globally permitted, and not denied.

    Exclusion criteria (applied in order):
    1. Below min_observations or min_distinct_sessions thresholds.
    2. Already has a global-tier permission (approved or rejected) for a
       rule_shape matching (verb, subcommand, flags) — any path_spec.
    3. Deny filter applies (deny or ask tier) — these commands are never
       auto-promoted.
    """
    thresholds = load_thresholds()
    rows = conn.execute(
        """
        SELECT c.id, c.verb, c.subcommand, c.flags,
               c.observations, c.distinct_sessions
          FROM permission_candidates c
         WHERE c.observations >= ?
           AND c.distinct_sessions >= ?
           AND NOT EXISTS (
             SELECT 1
               FROM rule_shapes rs
               JOIN permissions p ON p.rule_shape_id = rs.id
              WHERE rs.verb = c.verb
                AND IFNULL(rs.subcommand, '') = IFNULL(c.subcommand, '')
                AND rs.flags = c.flags
                AND p.session_id IS NULL
                AND p.project_id IS NULL
           )
         ORDER BY c.observations DESC, c.verb, c.subcommand;
        """,
        (thresholds.min_observations, thresholds.min_distinct_sessions),
    ).fetchall()

    out: list[Candidate] = []
    for cand_id, verb, subcommand, flags_json, obs, sess in rows:
        # Apply deny filter at propose time.
        leaf = _candidate_leaf(verb, subcommand, flags_json)
        decision, _reason = evaluate(leaf)
        if decision is not None:
            # deny or ask tier — skip from promotion pipeline.
            continue
        try:
            flag_list = json.loads(flags_json)
        except (json.JSONDecodeError, TypeError):
            flag_list = []
        out.append(
            Candidate(
                id=int(cand_id),
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
    """Open the observations DB (no migration — Phase 8 greenfield schema)."""
    db = _lib_db()
    return db._open()


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _format_flags(flags: frozenset[str]) -> str:
    return " ".join(sorted(flags)) if flags else "-"


def _parse_flags_arg(raw: str | None) -> str:
    """Parse a --flags CLI argument into the stored flags-json form.

    The argument is a JSON array literal (e.g. '["-a","-l"]' or '[]'),
    or the wildcard sentinel ``"*"``.
    Missing/None is treated as [] (bare verb with no flags).
    """
    db = _lib_db()
    if raw is None or raw == "":
        return db.minify_json([])
    if raw == "*":
        return "*"
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(
            f'error: --flags must be a JSON array literal (e.g. \'["-a","-l"]\') '
            f'or the wildcard "*": {exc}'
        )
    if not isinstance(parsed, list):
        raise SystemExit("error: --flags must be a JSON array literal, e.g. '[]'")
    return db.minify_json(sorted(str(x) for x in parsed))


def _format_cli_flags(flags_json: str) -> str:
    """Render a stored flags-json blob as a compact list for CLI output."""
    try:
        return str(json.loads(flags_json))
    except (json.JSONDecodeError, TypeError):
        return flags_json


def _resolve_tier_ids(
    conn: sqlite3.Connection,  # noqa: ARG001 — reserved for future validation
    tier: str,
    session_id_arg: int | None,
    project_id_arg: int | None,
) -> tuple[int | None, int | None]:
    """Resolve --tier + optional --session-id / --project-id to (session_id, project_id).

    Returns (session_id, project_id) for the permissions row. Exactly one of
    the two will be set (or both None for global tier), satisfying the schema
    CHECK constraint.
    """
    if tier == "global":
        return (None, None)
    if tier == "session":
        if session_id_arg is None:
            raise SystemExit("error: --tier session requires --session-id")
        return (session_id_arg, None)
    if tier == "project":
        if project_id_arg is None:
            raise SystemExit("error: --tier project requires --project-id")
        return (None, project_id_arg)
    raise SystemExit(
        f"error: unknown tier {tier!r}; expected session, project, or global"
    )


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------


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
            SELECT verb, subcommand, flags, observations, distinct_sessions,
                   first_seen, last_seen
              FROM v_candidates
             ORDER BY last_seen DESC;
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


def _cmd_propose(_args: argparse.Namespace) -> int:
    """Emit eligible promotions as pipe-delimited records for review.sh.

    One line per candidate: ``verb|subcommand-or-empty|flags-json|obs|sessions``.
    ``|`` separator avoids bash IFS issues with empty subcommand fields.
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
        print(f"{c.verb}|{sub}|{flags_json}|{c.observations}|{c.distinct_sessions}")
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    """Upsert a rule_shape and insert an 'approved' permissions row."""
    from nephoscope.lib.mirror.writer import MirrorHashMismatch, sync_affected

    flags_json = _parse_flags_arg(args.flags)
    path_spec: str | None = args.path_spec
    conn = _connect()
    try:
        session_id, project_id = _resolve_tier_ids(
            conn, args.tier, args.session_id, args.project_id
        )
        db = _lib_db()
        now = _now()
        shape_id = db.upsert_rule_shape(
            conn, args.verb, args.subcommand, flags_json, path_spec, now
        )
        perm_id = db.insert_permission(
            conn,
            shape_id,
            session_id,
            project_id,
            "approved",
            "learner",
            now,
            args.reason,
        )
        # Mirror sync: session-tier rules have no JSON analogue; skip them.
        if session_id is None:
            try:
                sync_affected(conn, perm_id)
            except MirrorHashMismatch as exc:
                path = str(exc).split(":")[0]
                print(
                    f"settings file at {path} was edited externally — "
                    f"run '/permissions reconcile' and retry",
                    file=sys.stderr,
                )
                return 1
    finally:
        conn.close()

    sub = args.subcommand or "-"
    ps = f" path_spec={path_spec!r}" if path_spec is not None else ""
    print(
        f"promoted: {args.verb} {sub} flags={_format_cli_flags(flags_json)}"
        f" tier={args.tier}{ps}"
    )
    return 0


def _cmd_reject(args: argparse.Namespace) -> int:
    """Upsert a rule_shape and insert a 'rejected' permissions row."""
    from nephoscope.lib.mirror.writer import MirrorHashMismatch, sync_affected

    flags_json = _parse_flags_arg(args.flags)
    path_spec: str | None = args.path_spec
    conn = _connect()
    try:
        session_id, project_id = _resolve_tier_ids(
            conn, args.tier, args.session_id, args.project_id
        )
        db = _lib_db()
        now = _now()
        shape_id = db.upsert_rule_shape(
            conn, args.verb, args.subcommand, flags_json, path_spec, now
        )
        perm_id = db.insert_permission(
            conn,
            shape_id,
            session_id,
            project_id,
            "rejected",
            "learner",
            now,
            args.reason,
        )
        # Mirror sync: session-tier rules have no JSON analogue; skip them.
        if session_id is None:
            try:
                sync_affected(conn, perm_id)
            except MirrorHashMismatch as exc:
                path = str(exc).split(":")[0]
                print(
                    f"settings file at {path} was edited externally — "
                    f"run '/permissions reconcile' and retry",
                    file=sys.stderr,
                )
                return 1
    finally:
        conn.close()

    sub = args.subcommand or "-"
    ps = f" path_spec={path_spec!r}" if path_spec is not None else ""
    reason_part = f" reason={args.reason!r}" if args.reason else ""
    print(
        f"rejected: {args.verb} {sub} flags={_format_cli_flags(flags_json)}"
        f" tier={args.tier}{ps}{reason_part}"
    )
    return 0


def _cmd_unpermit(args: argparse.Namespace) -> int:
    """Delete the permissions row matching shape + tier.

    Uses SQLite's IS operator which correctly handles NULL comparisons for
    both NULL and non-NULL values (IS NULL when arg is None, IS <val>
    otherwise), satisfying the three-tier (session/project/global) lookup.
    """
    from nephoscope.lib.mirror.writer import MirrorHashMismatch, sync_global, sync_project

    flags_json = _parse_flags_arg(args.flags)
    path_spec: str | None = args.path_spec
    conn = _connect()
    try:
        session_id, project_id = _resolve_tier_ids(
            conn, args.tier, args.session_id, args.project_id
        )
        row = conn.execute(
            """
            SELECT id FROM rule_shapes
             WHERE verb = ?
               AND IFNULL(subcommand, '') = IFNULL(?, '')
               AND flags = ?
               AND IFNULL(path_spec, '') = IFNULL(?, '');
            """,
            (args.verb, args.subcommand, flags_json, path_spec),
        ).fetchone()
        if row is None:
            print(
                f"no matching rule_shape for verb={args.verb!r} "
                f"subcommand={args.subcommand!r} "
                f"flags={_format_cli_flags(flags_json)} "
                f"path_spec={path_spec!r}",
                file=sys.stderr,
            )
            return 1
        shape_id = int(row[0])
        cur = conn.execute(
            """
            DELETE FROM permissions
             WHERE rule_shape_id = ?
               AND session_id IS ?
               AND project_id IS ?;
            """,
            (shape_id, session_id, project_id),
        )
        deleted = cur.rowcount or 0

        # Mirror sync after deletion: session-tier has no JSON analogue.
        if deleted > 0 and session_id is None:
            try:
                if project_id is None:
                    sync_global(conn)
                else:
                    sync_project(conn, project_id)
            except MirrorHashMismatch as exc:
                path = str(exc).split(":")[0]
                print(
                    f"settings file at {path} was edited externally — "
                    f"run '/permissions reconcile' and retry",
                    file=sys.stderr,
                )
                return 1
    finally:
        conn.close()

    sub = args.subcommand or "-"
    if deleted == 0:
        print(
            f"no matching permission row for verb={args.verb!r} "
            f"subcommand={args.subcommand!r} tier={args.tier}"
        )
        return 0
    print(
        f"unpermitted: {args.verb} {sub} flags={_format_cli_flags(flags_json)}"
        f" tier={args.tier} ({deleted} row(s) deleted)"
    )
    return 0


def _cmd_pattern_variants(args: argparse.Namespace) -> int:
    """Compute pattern variants for a single candidate and print them as JSON.

    Used by review.sh to decide which per-axis prompts to display.

    Outputs one JSON object::

        {
            "verb_pattern": "<$VAR/...>" | null,
            "path_specs":   ["$VAR/**", ...],
            "flags_literal": "<minified-json-or-*>"
        }

    ``verb_pattern`` is non-null only when the verb is an absolute path that
    falls under a recognised context variable.  ``path_specs`` is populated
    from positional-path variants (empty for most DB candidates which have no
    stored positional_paths).
    """
    from .canonicalize import CanonicalLeaf, to_pattern_form  # noqa: PLC0415

    flags_json = _parse_flags_arg(args.flags)
    if flags_json == "*":
        flags_list: list[str] = []
    else:
        try:
            flags_list = json.loads(flags_json)
        except (json.JSONDecodeError, TypeError):
            flags_list = []

    leaf = CanonicalLeaf(
        verb=args.verb,
        subcommand=args.subcommand,
        flags=frozenset(flags_list),
        redirections=(),
        raw_leaf=args.verb,
    )

    ctx: dict[str, str] = {}
    if args.home:
        ctx["home"] = args.home
    if args.cwd:
        ctx["cwd"] = args.cwd
    if args.project_root:
        ctx["project_root"] = args.project_root

    variants = to_pattern_form(leaf, ctx)

    verb_pattern: str | None = None
    path_specs: list[str] = []
    seen_ps: set[str] = set()

    for v in variants:
        if verb_pattern is None and v.verb != args.verb and v.verb.startswith("$"):
            verb_pattern = v.verb
        if v.path_spec and "$" in v.path_spec and v.path_spec not in seen_ps:
            seen_ps.add(v.path_spec)
            path_specs.append(v.path_spec)

    print(
        json.dumps(
            {
                "verb_pattern": verb_pattern,
                "path_specs": path_specs,
                "flags_literal": flags_json,
            }
        )
    )
    return 0


def _cmd_context_ids(args: argparse.Namespace) -> int:
    """Resolve project_id and latest session_id for a given cwd.

    Prints two shell-assignment-style lines suitable for ``eval``::

        project_id=<int>
        session_id=<int>

    Prints empty-valued lines when the cwd is not found in the DB.
    """
    cwd = args.cwd or ""
    conn = _connect()
    try:
        p_row = conn.execute(
            "SELECT id FROM projects WHERE cwd = ?;", (cwd,)
        ).fetchone()
        project_id: int | str = int(p_row[0]) if p_row else ""

        s_row: tuple | None = None
        if p_row:
            s_row = conn.execute(
                "SELECT id FROM sessions WHERE project_id = ?"
                " ORDER BY last_activity DESC LIMIT 1;",
                (project_id,),
            ).fetchone()
        session_id: int | str = int(s_row[0]) if s_row else ""
    finally:
        conn.close()

    print(f"project_id={project_id}")
    print(f"session_id={session_id}")
    return 0


def _cmd_count_concrete_siblings(args: argparse.Namespace) -> int:
    """Print the count of concrete (non-wildcard) sibling permissions at a tier.

    "Siblings" are permissions whose rule_shape has the same verb+subcommand
    but a literal (non-``"*"``) flags value, at the given tier.  Used by
    review.sh to decide whether to offer a subsume prompt after promoting a
    flags=``"*"`` rule.
    """
    conn = _connect()
    try:
        session_id, project_id = _resolve_tier_ids(
            conn, args.tier, args.session_id, args.project_id
        )
        row = conn.execute(
            """
            SELECT COUNT(*)
              FROM permissions p
              JOIN rule_shapes rs ON rs.id = p.rule_shape_id
             WHERE rs.verb = ?
               AND IFNULL(rs.subcommand, '') = IFNULL(?, '')
               AND rs.flags != '*'
               AND p.session_id IS ?
               AND p.project_id IS ?;
            """,
            (args.verb, args.subcommand, session_id, project_id),
        ).fetchone()
        count = int(row[0]) if row else 0
    finally:
        conn.close()

    print(count)
    return 0


def _cmd_subsume_siblings(args: argparse.Namespace) -> int:
    """Delete concrete sibling permissions for verb+sub at a tier.

    Called after promoting a flags=``"*"`` rule (decision 8-15).  Removes all
    permissions whose rule_shape matches the same verb+subcommand but carries a
    literal (non-``"*"``) flags value at the same tier, so the wildcard rule
    is the sole match point.

    Prints a one-line summary: ``"subsumed N concrete sibling rule(s)"``.
    """
    conn = _connect()
    try:
        session_id, project_id = _resolve_tier_ids(
            conn, args.tier, args.session_id, args.project_id
        )
        cur = conn.execute(
            """
            DELETE FROM permissions
             WHERE session_id IS ?
               AND project_id IS ?
               AND rule_shape_id IN (
                 SELECT id FROM rule_shapes
                  WHERE verb = ?
                    AND IFNULL(subcommand, '') = IFNULL(?, '')
                    AND flags != '*'
               );
            """,
            (session_id, project_id, args.verb, args.subcommand),
        )
        deleted = cur.rowcount or 0
    finally:
        conn.close()

    print(f"subsumed {deleted} concrete sibling rule(s)")
    return 0


def _cmd_permissions(_args: argparse.Namespace) -> int:
    """Dump all permission rows via the v_permissions view."""
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT verb, subcommand, flags, path_spec,
                   decision, source, tier, decided_at, reason
              FROM v_permissions
             ORDER BY decided_at DESC;
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("permissions table is empty")
        return 0
    for (
        verb,
        subcommand,
        flags_json,
        path_spec,
        decision,
        source,
        tier,
        decided_at,
        reason,
    ) in rows:
        sub = subcommand or "-"
        try:
            flags = json.loads(flags_json)
        except (json.JSONDecodeError, TypeError):
            flags = []
        ps = f" path_spec={path_spec!r}" if path_spec is not None else ""
        reason_part = f" reason={reason!r}" if reason else ""
        print(
            f"  {decision:<8} {tier:<8} {verb:<10} {sub:<15} "
            f"flags={flags}{ps} source={source} at={decided_at}{reason_part}"
        )
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

_tier_help = "Tier: session, project, or global (default: global)."
_session_id_help = "sessions.id integer (required when --tier session)."
_project_id_help = "projects.id integer (required when --tier project)."
_verb_help = "Command verb, e.g. 'git'."
_subcommand_help = "Subcommand (omit for no-subcommand verbs; matches NULL)."
_flags_help = (
    "JSON array literal of flags, e.g. '[\"--amend\"]' or '[]'; "
    'or the wildcard sentinel "*".'
)
_path_spec_help = (
    'path_spec stored on the rule_shape: NULL=any, ""=no-paths, '
    '"$VAR/**"=glob. Omit for NULL (any).'
)
_reason_help = "Optional free-text reason stored with the permission."


def _add_shape_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--verb", required=True, help=_verb_help)
    p.add_argument("--subcommand", default=None, help=_subcommand_help)
    p.add_argument("--flags", default=None, help=_flags_help)
    p.add_argument("--path-spec", default=None, dest="path_spec", help=_path_spec_help)


def _add_verb_sub_args(p: argparse.ArgumentParser) -> None:
    """Add --verb and --subcommand only (no --flags / --path-spec)."""
    p.add_argument("--verb", required=True, help=_verb_help)
    p.add_argument("--subcommand", default=None, help=_subcommand_help)


def _add_tier_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--tier",
        default="global",
        choices=["session", "project", "global"],
        help=_tier_help,
    )
    p.add_argument(
        "--session-id", default=None, type=int, dest="session_id", help=_session_id_help
    )
    p.add_argument(
        "--project-id", default=None, type=int, dest="project_id", help=_project_id_help
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nephoscope.learners.permission.learner",
        description=(
            "Permission learner — scan candidates, propose promotions, "
            "promote/reject/unpermit rules."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "scan", help="Scan new Bash rows; upsert into permission_candidates."
    )
    sub.add_parser("candidates", help="Dump v_candidates.")
    sub.add_parser(
        "propose",
        help="Emit eligible promotions as pipe-delimited lines (for review.sh).",
    )
    sub.add_parser("permissions", help="Dump v_permissions (all permission rows).")

    promote = sub.add_parser("promote", help="Promote a shape to approved.")
    _add_shape_args(promote)
    _add_tier_args(promote)
    promote.add_argument("--reason", default=None, help=_reason_help)
    promote.add_argument(
        "--sync",
        action="store_true",
        default=False,
        help="Explicitly request mirror sync after promote (default: on for non-session tier).",
    )

    reject = sub.add_parser("reject", help="Reject a shape.")
    _add_shape_args(reject)
    _add_tier_args(reject)
    reject.add_argument("--reason", default=None, help=_reason_help)

    unpermit = sub.add_parser("unpermit", help="Delete a permission row.")
    _add_shape_args(unpermit)
    _add_tier_args(unpermit)

    pv = sub.add_parser(
        "pattern-variants",
        help="Compute pattern variants for a candidate (JSON output, for review.sh).",
    )
    pv.add_argument("--verb", required=True, help=_verb_help)
    pv.add_argument("--subcommand", default=None, help=_subcommand_help)
    pv.add_argument("--flags", default=None, help=_flags_help)
    pv.add_argument("--home", default=None, help="$HOME path for pattern substitution.")
    pv.add_argument("--cwd", default=None, help="Current working directory.")
    pv.add_argument(
        "--project-root",
        default=None,
        dest="project_root",
        help="Project root path.",
    )

    ci = sub.add_parser(
        "context-ids",
        help="Resolve project_id and session_id for a cwd (shell-assignment output).",
    )
    ci.add_argument("--cwd", default=None, help="Directory to look up.")

    ccs = sub.add_parser(
        "count-concrete-siblings",
        help="Count concrete (non-wildcard) sibling permissions for verb+sub at tier.",
    )
    _add_verb_sub_args(ccs)
    _add_tier_args(ccs)

    ss = sub.add_parser(
        "subsume-siblings",
        help="Delete concrete sibling permissions for verb+sub at tier (after flags=* promote).",
    )
    _add_verb_sub_args(ss)
    _add_tier_args(ss)

    args = parser.parse_args(argv)

    _cmd_map = {
        "scan": _cmd_scan,
        "candidates": _cmd_candidates,
        "propose": _cmd_propose,
        "permissions": _cmd_permissions,
        "promote": _cmd_promote,
        "reject": _cmd_reject,
        "unpermit": _cmd_unpermit,
        "pattern-variants": _cmd_pattern_variants,
        "context-ids": _cmd_context_ids,
        "count-concrete-siblings": _cmd_count_concrete_siblings,
        "subsume-siblings": _cmd_subsume_siblings,
    }
    handler = _cmd_map.get(args.cmd)
    if handler is None:  # pragma: no cover
        return 1
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
