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
    """Open the observations DB — greenfield schema, no migration system."""
    db = _lib_db()
    return db._open()


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


_TIER_NAME = {
    "global": "everywhere",
    "project": "in this project",
    "session": "in this session",
}


def _tier_phrase(tier: str) -> str:
    """Turn a tier name into a plain-English phrase for user output."""
    return _TIER_NAME.get(tier, tier)


def _describe_rule(
    verb: str,
    subcommand: str | None,
    flags_json: str | None,
    path_spec: str | None,
) -> str:
    """Render a rule shape as a plain-English command description.

    flags_json accepts None as well as string forms because some callers
    (e.g. malformed DB rows) may pass null-equivalents; the function
    degrades gracefully to the no-options branch.
    """
    sub_part = f" {subcommand}" if subcommand else ""

    if flags_json == "*":
        flags_part = " with any options"
    elif flags_json is None:
        flags_part = " (no options)"
    else:
        try:
            flags = json.loads(flags_json)
        except json.JSONDecodeError:
            flags = []
        if flags:
            flags_part = f" with options {' '.join(str(f) for f in flags)}"
        else:
            flags_part = " (no options)"

    if path_spec is None:
        path_part = ""
    elif path_spec == "":
        path_part = " (only when no paths are given)"
    else:
        path_part = f" on paths matching {path_spec}"

    return f"{verb}{sub_part}{flags_part}{path_part}"


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
    try:
        conn = _connect()
    except Exception as exc:  # noqa: BLE001
        print(f"scan: cannot open database: {exc}", file=sys.stderr)
        return 1
    try:
        processed = scan_candidates(conn)
        proposals = propose_promotions(conn)
    finally:
        conn.close()

    if processed == 0:
        print("No new Bash commands since the last scan.")
    else:
        cmd_word = "command" if processed == 1 else "commands"
        print(f"Scanned {processed} new Bash {cmd_word} since the last run.")
    if not proposals:
        print("No recurring patterns are ready to promote yet.")
        return 0
    rule_word = "rule" if len(proposals) == 1 else "rules"
    print(f"{len(proposals)} pattern(s) ready to promote to {rule_word}:")
    for c in proposals:
        flags_json = json.dumps(sorted(c.flags))
        description = _describe_rule(c.verb, c.subcommand, flags_json, None)
        print(
            f"  {description}"
            f"  — seen {c.observations} times across"
            f" {c.distinct_sessions} session(s)"
        )
    return 0


def _cmd_candidates(_args: argparse.Namespace) -> int:
    try:
        conn = _connect()
    except Exception as exc:  # noqa: BLE001
        print(f"candidates: cannot open database: {exc}", file=sys.stderr)
        return 1
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
        print("No command patterns have been noticed yet.")
        return 0
    print(f"{len(rows)} command pattern(s) noticed so far:")
    for verb, subcommand, flags_json, obs, sess, first_seen, last_seen in rows:
        description = _describe_rule(verb, subcommand, flags_json, None)
        print(
            f"  {description}"
            f"  — seen {obs} times across {sess} session(s),"
            f" first {first_seen}, last {last_seen}"
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


def _cmd_write_permission(args: argparse.Namespace, decision: str) -> int:
    """Upsert a rule_shape and insert a permission row for the given decision."""
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
            decision,
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
                    f"The settings file at {path} was edited externally — "
                    f"run '/nephoscope:permissions reconcile' and retry.",
                    file=sys.stderr,
                )
                return 1
    finally:
        conn.close()

    description = _describe_rule(args.verb, args.subcommand, flags_json, path_spec)
    if decision == "approved":
        print(f"Approved {_tier_phrase(args.tier)}: {description}.")
    else:
        reason_part = f" (reason: {args.reason})" if args.reason else ""
        print(f"Rejected {_tier_phrase(args.tier)}: {description}{reason_part}.")
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    """Upsert a rule_shape and insert an 'approved' permissions row."""
    return _cmd_write_permission(args, "approved")


def _cmd_reject(args: argparse.Namespace) -> int:
    """Upsert a rule_shape and insert a 'rejected' permissions row."""
    return _cmd_write_permission(args, "rejected")


def _cmd_unpermit(args: argparse.Namespace) -> int:
    """Delete the permissions row matching shape + tier.

    Uses SQLite's IS operator which correctly handles NULL comparisons for
    both NULL and non-NULL values (IS NULL when arg is None, IS <val>
    otherwise), satisfying the three-tier (session/project/global) lookup.
    """
    from nephoscope.lib.mirror.writer import (
        MirrorHashMismatch,
        sync_global,
        sync_project,
    )

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
            description = _describe_rule(
                args.verb, args.subcommand, flags_json, path_spec
            )
            print(
                f"No rule found matching: {description}.",
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
                    f"The settings file at {path} was edited externally — "
                    f"run '/nephoscope:permissions reconcile' and retry.",
                    file=sys.stderr,
                )
                return 1
    finally:
        conn.close()

    description = _describe_rule(args.verb, args.subcommand, flags_json, path_spec)
    if deleted == 0:
        print(f"No matching rule found {_tier_phrase(args.tier)}: {description}.")
        return 0
    print(f"Removed {_tier_phrase(args.tier)}: {description}.")
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
    from nephoscope.lib.mirror.writer import (
        MirrorHashMismatch,
        sync_global,
        sync_project,
    )

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
        if deleted > 0 and session_id is None:
            try:
                if project_id is None:
                    sync_global(conn)
                else:
                    sync_project(conn, project_id)
            except MirrorHashMismatch as exc:
                path = str(exc).split(":")[0]
                print(
                    f"The settings file at {path} was edited externally — "
                    f"run '/nephoscope:permissions reconcile' and retry.",
                    file=sys.stderr,
                )
                return 1
    finally:
        conn.close()

    rule_word = "rule" if deleted == 1 else "rules"
    if deleted == 0:
        print("Removed 0 more-specific rules (nothing to clean up).")
    else:
        print(
            f"Removed {deleted} more-specific {rule_word} that the wildcard rule now covers."
        )
    return 0


def _cmd_permissions(_args: argparse.Namespace) -> int:
    """Dump all permission rows via the v_permissions view."""
    try:
        conn = _connect()
    except Exception as exc:  # noqa: BLE001
        print(f"permissions: cannot open database: {exc}", file=sys.stderr)
        return 1
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
        print("No permission rules have been set up yet.")
        return 0
    print(f"{len(rows)} permission rule(s):")
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
        description = _describe_rule(verb, subcommand, flags_json, path_spec)
        decision_word = "APPROVED" if decision == "approved" else "REJECTED"
        reason_part = f' — "{reason}"' if reason else ""
        print(
            f"  [{decision_word}] {_tier_phrase(tier)}: {description}"
            f"  (set {decided_at} by {source}){reason_part}"
        )
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

_tier_help = (
    "How widely the rule applies. One of:\n"
    "\n"
    "  global   applies everywhere you use Claude Code (the default)\n"
    "  project  applies only in the current project\n"
    "  session  applies only in the current Claude Code session\n"
    "\n"
    'Use "project" when the permission only makes sense in one codebase\n'
    '(for example, allowing a project-specific build command). Use "session"\n'
    "for a one-off trial that should disappear when the session ends.\n"
    "\n"
    "--tier session requires --session-id; --tier project requires --project-id."
)
_session_id_help = (
    "Internal numeric ID of the session (from the observations database).\n"
    "Required when --tier session. Usually filled in by the review tool for you."
)
_project_id_help = (
    "Internal numeric ID of the project (from the observations database).\n"
    "Required when --tier project. Usually filled in by the review tool for you."
)
_verb_help = (
    'The command name this rule is about — for example, "git", "ls", or "rm".\n'
    "\n"
    "You can also give an absolute path to a specific executable\n"
    '(for example, "/usr/local/bin/my-tool"). If the path sits under your\n'
    "home, project root, or current working directory, you may use the\n"
    "matching placeholder ($HOME, $PROJECT_ROOT, $CWD) instead, like\n"
    '"$PROJECT_ROOT/scripts/deploy.sh".'
)
_subcommand_help = (
    'The sub-action of the command — for example, with "git commit",\n'
    '"commit" is the subcommand.\n'
    "\n"
    'Leave this option out if the command takes no subcommand (like "ls").\n'
    "Not all commands have subcommands; when in doubt, omit it."
)
_flags_help = (
    "Which command-line options this rule matches, written as a list.\n"
    "\n"
    "Examples:\n"
    "  --flags '[]'                  match the command with no options\n"
    '  --flags \'["-l"]\'              match only when "-l" is used\n'
    '  --flags \'["--amend"]\'         match only when "--amend" is used\n'
    '  --flags \'["-a","-l"]\'         match only when both options are used\n'
    '  --flags "*"                     match any options (a wildcard)\n'
    "\n"
    "The list must be written in JSON form (square brackets, quoted entries).\n"
    "Leave the option out or use '[]' for the no-options case."
)
_path_spec_help = (
    "Restrict the rule to match only certain file or folder paths.\n"
    "\n"
    "Leave this option out (the default) to let the rule match any path.\n"
    'Pass "" (empty string) to match only commands that take no paths at all.\n'
    "\n"
    "To restrict to a specific area, start with one of these placeholders:\n"
    "  $PROJECT_ROOT   the root of the project you are currently working in\n"
    "  $CWD            the working directory of the current Claude Code session\n"
    "  $HOME           your home directory\n"
    "\n"
    "After the placeholder, add a path fragment:\n"
    "  /**             matches anything inside that area, at any depth\n"
    "  /subdir/**      matches anything under that specific subfolder\n"
    "  /specific/file  matches only that one path\n"
    "\n"
    "Examples:\n"
    '  --path-spec "$PROJECT_ROOT/**"     allow anywhere in the current project\n'
    '  --path-spec "$HOME/Downloads/**"   allow anywhere under your Downloads folder\n'
    '  --path-spec "$CWD/build/**"        allow only the build folder of this session\n'
    "\n"
    "Nephoscope also emits inline absolute path-specs (e.g. /opt/shared/**) for\n"
    "paths under directories added via Claude Code's --add-dir flag or the\n"
    "/permissions UI (permissions.additionalDirectories). Those specs are written\n"
    "verbatim and work the same way as placeholder-based ones."
)
_reason_help = (
    "A short free-text note saved with the rule — a reminder of why you\n"
    "made this decision. Shown later when you review the rule."
)


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
            "Learn and manage Bash permission rules for Claude Code.\n"
            "\n"
            "This tool watches which Bash commands you run in Claude Code,\n"
            "notices the ones that come up often, and helps you turn them into\n"
            "permission rules so Claude Code can run them without asking every\n"
            "time. You can also write rules by hand using 'promote' or 'reject'."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "scan",
        help="Look for new Bash commands to learn from.",
        description=(
            "Look at recent Bash commands you have run in Claude Code and\n"
            "record new patterns that might be worth turning into rules."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    sub.add_parser(
        "candidates",
        help="List recurring command patterns that have been noticed.",
        description=(
            "List the command patterns that have been noticed often enough\n"
            "to be worth reviewing. Each row shows the command, how many\n"
            "times it has been seen, and across how many sessions."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    sub.add_parser(
        "propose",
        help="Emit patterns ready for review (machine-readable; used by the review tool).",
        description=(
            "Print patterns that are ready to be turned into rules, one per\n"
            "line in a compact pipe-separated format. Intended for the\n"
            "interactive review tool, not for direct reading."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    sub.add_parser(
        "permissions",
        help="List every permission rule, approved and rejected.",
        description=(
            "Print every permission rule currently stored — both the approved\n"
            "ones (commands Claude Code may run without asking) and the\n"
            "rejected ones (commands Claude Code must never run)."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    promote = sub.add_parser(
        "promote",
        help="Approve a command pattern so Claude Code can run it without asking.",
        description=(
            "Approve a command pattern. Once approved, Claude Code may run\n"
            "commands matching this pattern without asking for permission.\n"
            "\n"
            "Examples:\n"
            "  promote --verb ls\n"
            '      allow "ls" with no options, anywhere\n'
            "\n"
            "  promote --verb git --subcommand status --flags '[]'\n"
            '      allow "git status" with no extra options\n'
            "\n"
            "  promote --verb rm --flags '*' --path-spec '$PROJECT_ROOT/**' \\\n"
            "          --tier project\n"
            '      allow "rm" with any options, but only inside this project'
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    _add_shape_args(promote)
    _add_tier_args(promote)
    promote.add_argument("--reason", default=None, help=_reason_help)
    promote.add_argument(
        "--sync",
        action="store_true",
        default=False,
        help=(
            "Also update the settings.json file after approving the rule.\n"
            "This happens automatically for project and global rules, so\n"
            "you normally do not need this option."
        ),
    )

    reject = sub.add_parser(
        "reject",
        help="Mark a command pattern as never-allowed so Claude Code will refuse it.",
        description=(
            "Mark a command pattern as rejected. Claude Code will refuse to\n"
            "run commands matching this pattern, even if you would otherwise\n"
            "approve them when asked.\n"
            "\n"
            "Example:\n"
            "  reject --verb rm --flags '[\"-rf\"]' --reason 'too dangerous'"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    _add_shape_args(reject)
    _add_tier_args(reject)
    reject.add_argument("--reason", default=None, help=_reason_help)

    unpermit = sub.add_parser(
        "unpermit",
        help="Remove a previously approved or rejected rule.",
        description=(
            "Remove an existing permission rule. Use this to undo an earlier\n"
            "approval or rejection; Claude Code will ask for permission again\n"
            "the next time it wants to run a matching command.\n"
            "\n"
            "You must give the same verb, subcommand, flags, path, and tier\n"
            "that the original rule used."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    _add_shape_args(unpermit)
    _add_tier_args(unpermit)

    pv = sub.add_parser(
        "pattern-variants",
        help=(
            "Compute placeholder-form variants of a command (machine-readable;\n"
            "used by the review tool)."
        ),
        description=(
            "For a single command pattern, print JSON describing its variants\n"
            "with $HOME, $PROJECT_ROOT, and $CWD placeholders substituted.\n"
            "Used by the interactive review tool to build its prompts."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    pv.add_argument("--verb", required=True, help=_verb_help)
    pv.add_argument("--subcommand", default=None, help=_subcommand_help)
    pv.add_argument("--flags", default=None, help=_flags_help)
    pv.add_argument(
        "--home",
        default=None,
        help=(
            "Your home directory, used to substitute $HOME in path patterns.\n"
            "Normally the review tool fills this in for you."
        ),
    )
    pv.add_argument(
        "--cwd",
        default=None,
        help=(
            "The current working directory, used to substitute $CWD in path\n"
            "patterns. Normally the review tool fills this in for you."
        ),
    )
    pv.add_argument(
        "--project-root",
        default=None,
        dest="project_root",
        help=(
            "The project root directory, used to substitute $PROJECT_ROOT in\n"
            "path patterns. Normally the review tool fills this in for you."
        ),
    )

    ci = sub.add_parser(
        "context-ids",
        help=(
            "Look up the internal project and session IDs for a directory\n"
            "(machine-readable; used by the review tool)."
        ),
        description=(
            "Print the internal numeric project ID and most-recent session ID\n"
            "for a given working directory, in a format suitable for shell\n"
            "consumption. Used by the interactive review tool."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ci.add_argument(
        "--cwd",
        default=None,
        help="The directory to look up.",
    )

    ccs = sub.add_parser(
        "count-concrete-siblings",
        help=(
            "Count how many specific-option rules exist alongside a wildcard\n"
            "rule (machine-readable; used by the review tool)."
        ),
        description=(
            "Count the number of existing rules for the same command that\n"
            "match a specific set of options, rather than any options.\n"
            "Used after approving a wildcard rule to ask whether the older,\n"
            "now-redundant specific rules should be cleaned up."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    _add_verb_sub_args(ccs)
    _add_tier_args(ccs)

    ss = sub.add_parser(
        "subsume-siblings",
        help=(
            "Remove specific-option rules that a wildcard rule now covers.\n"
            "(Run this after approving a wildcard rule to tidy up.)"
        ),
        description=(
            "Delete rules for the same command that match a specific set of\n"
            "options, leaving the wildcard rule as the sole match. Run this\n"
            "after approving a wildcard rule so the older, now-redundant\n"
            "rules do not clutter the rule list."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
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
