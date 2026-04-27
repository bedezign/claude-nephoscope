"""Interactive per-axis review CLI for permission candidates.

Replaces ``src/nephoscope/learners/permission/scripts/review.sh``.

For each eligible candidate from ``propose_promotions``:

  Axis 1 (verb)  — only when ``to_pattern_form`` finds a $VAR substitution in
                   the verb (i.e. verb is an absolute path under HOME/CWD/
                   PROJECT_ROOT). Prompts: literal / generalize / skip.

  Axis 2 (paths) — path constraint for the rule_shape.path_spec.
                   Prompts: a=any(NULL) / numbered path_spec variants / s=skip.

  Axis 3 (flags) — literal flags array vs wildcard "*".
                   Prompts: l=literal / w=wildcard / s=skip.

  Axis 4 (tier)  — permission tier.
                   Prompts: g=global / p=project / s=session / skip / q=quit.

  Post-promote   — when flags="*" and concrete sibling rules exist: offer subsume.

Promotion delegates to ``_cmd_write_permission`` via the learner module (no
external subprocess). MirrorHashMismatch is caught and surfaces the same
user-facing wording as the legacy bash script.
"""

from __future__ import annotations

import argparse
import os
import sys

from nephoscope.learners.permission.learner import (
    Candidate,
    _connect,
    _lib_db,
    _parse_flags_arg,
    _resolve_tier_ids,
    propose_promotions,
    scan_candidates,
)
from nephoscope.lib.mirror.writer import MirrorHashMismatch


# ---------------------------------------------------------------------------
# TTY I/O
# ---------------------------------------------------------------------------


def _read_line() -> str:
    """Read one line from stdin (interactive or piped — for tests)."""
    try:
        return input()
    except EOFError:
        return ""


def _prompt(text: str) -> str:
    """Print ``text`` without newline and return the user's response."""
    print(text, end="", flush=True)
    return _read_line().strip()


# ---------------------------------------------------------------------------
# Context resolution
# ---------------------------------------------------------------------------


def _resolve_context() -> tuple[str, str, str, int | None, int | None]:
    """Resolve HOME/CWD/PROJECT_ROOT and project/session ids.

    Returns (home, cwd, project_root, project_id, session_id).
    All values are best-effort; empty strings / None on failure.
    """
    from nephoscope.lib.scope import resolve_project_root

    home = os.environ.get("HOME", "")
    cwd = os.getcwd()

    project_root = ""
    try:
        result = resolve_project_root(cwd)
        project_root = result or ""
    except Exception:  # noqa: BLE001
        pass

    project_id: int | None = None
    session_id: int | None = None
    try:
        conn = _connect()
        try:
            p_row = conn.execute(
                "SELECT id FROM projects WHERE cwd = ?;", (cwd,)
            ).fetchone()
            if p_row:
                project_id = int(p_row[0])
                s_row = conn.execute(
                    "SELECT id FROM sessions WHERE project_id = ?"
                    " ORDER BY last_activity DESC LIMIT 1;",
                    (project_id,),
                ).fetchone()
                if s_row:
                    session_id = int(s_row[0])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

    return home, cwd, project_root, project_id, session_id


# ---------------------------------------------------------------------------
# Pattern variants (per-axis display data)
# ---------------------------------------------------------------------------


def _pattern_variants(
    candidate: Candidate,
    home: str,
    cwd: str,
    project_root: str,
) -> dict:
    """Compute verb pattern and path_specs for a candidate.

    Returns a dict with keys:
      verb_pattern  — str | None (non-null when verb is an absolute path under a ctx var)
      path_specs    — list[str] (from to_pattern_form)
      flags_literal — str (minified JSON or '*')
    """
    from nephoscope.learners.permission.canonicalize import (
        CanonicalLeaf,
        to_pattern_form,
    )

    flags_json = _lib_db().minify_json(sorted(candidate.flags))
    flags_list = list(candidate.flags)

    leaf = CanonicalLeaf(
        verb=candidate.verb,
        subcommand=candidate.subcommand,
        flags=frozenset(flags_list),
        redirections=(),
        raw_leaf=candidate.verb,
    )

    ctx: dict[str, str] = {}
    if home:
        ctx["home"] = home
    if cwd:
        ctx["cwd"] = cwd
    if project_root:
        ctx["project_root"] = project_root

    variants = to_pattern_form(leaf, ctx)

    verb_pattern: str | None = None
    path_specs: list[str] = []
    seen_ps: set[str] = set()

    for v in variants:
        if verb_pattern is None and v.verb != candidate.verb and v.verb.startswith("$"):
            verb_pattern = v.verb
        if v.path_spec and "$" in v.path_spec and v.path_spec not in seen_ps:
            seen_ps.add(v.path_spec)
            path_specs.append(v.path_spec)

    return {
        "verb_pattern": verb_pattern,
        "path_specs": path_specs,
        "flags_literal": flags_json,
    }


# ---------------------------------------------------------------------------
# Promote helpers
# ---------------------------------------------------------------------------


def _do_promote(
    verb: str,
    subcommand: str | None,
    flags_json: str,
    path_spec: str | None,
    tier: str,
    session_id: int | None,
    project_id: int | None,
) -> None:
    """Insert the permission and sync the mirror; raises MirrorHashMismatch on conflict."""
    from nephoscope.lib.mirror.writer import sync_affected

    flags_parsed = _parse_flags_arg(flags_json)
    conn = _connect()
    try:
        sess_id, proj_id = _resolve_tier_ids(conn, tier, session_id, project_id)
        db = _lib_db()
        now = db._now()
        shape_id = db.upsert_rule_shape(
            conn, verb, subcommand, flags_parsed, path_spec, now
        )
        perm_id = db.insert_permission(
            conn, shape_id, sess_id, proj_id, "approved", "learner", now, None
        )
        if sess_id is None:
            sync_affected(conn, perm_id)  # raises MirrorHashMismatch on conflict
    finally:
        conn.close()


def _count_concrete_siblings(
    verb: str,
    subcommand: str | None,
    tier: str,
    session_id: int | None,
    project_id: int | None,
) -> int:
    """Count concrete (non-wildcard) sibling permissions at the given tier."""
    conn = _connect()
    try:
        sess_id, proj_id = _resolve_tier_ids(conn, tier, session_id, project_id)
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
            (verb, subcommand, sess_id, proj_id),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _subsume_siblings(
    verb: str,
    subcommand: str | None,
    tier: str,
    session_id: int | None,
    project_id: int | None,
) -> int:
    """Delete concrete sibling permissions, syncing the mirror. Returns count deleted."""
    from nephoscope.lib.mirror.writer import sync_global, sync_project

    conn = _connect()
    try:
        sess_id, proj_id = _resolve_tier_ids(conn, tier, session_id, project_id)
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
            (sess_id, proj_id, verb, subcommand),
        )
        deleted = cur.rowcount or 0
        if deleted > 0 and sess_id is None:
            if proj_id is None:
                sync_global(conn)
            else:
                sync_project(conn, proj_id)
        return deleted
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Per-candidate review loop
# ---------------------------------------------------------------------------


def _build_path_opts(
    path_specs: list[str],
    project_root: str,
    cwd: str,
    home: str,
) -> list[str]:
    """Build the ordered, deduplicated path option menu for Axis 2."""
    seen_ps: set[str] = set()
    path_opts: list[str] = []

    def _add_ps(opt: str) -> None:
        if opt not in seen_ps:
            seen_ps.add(opt)
            path_opts.append(opt)

    for ps in path_specs:
        if ps:
            _add_ps(ps)
    for candidate_ps, guard in [
        ("$PROJECT_ROOT/**", project_root),
        ("$CWD/**", cwd),
        ("$HOME/**", home),
    ]:
        if guard:
            _add_ps(candidate_ps)
    return path_opts


def _prompt_axis_verb(candidate: Candidate, verb_pattern: str | None) -> str | None:
    """Prompt Axis 1 (verb). Returns chosen verb, or None to signal 'skipped'."""
    if not verb_pattern or verb_pattern == candidate.verb:
        return candidate.verb
    reply = _prompt(
        f"  Verb:  literal={candidate.verb:<40}  pattern={verb_pattern}\n"
        f"         [l=literal / g=generalize / s=skip]: "
    )
    if reply in ("s", "S"):
        return None  # skip
    if reply in ("g", "G"):
        return verb_pattern
    return candidate.verb


def _prompt_axis_paths(path_opts: list[str]) -> tuple[str | None, bool]:
    """Prompt Axis 2 (paths). Returns (chosen_path_spec, skipped)."""
    menu_parts = ["a=any"]
    for i, opt in enumerate(path_opts, start=1):
        menu_parts.append(f"{i}={opt}")
    menu_parts.append("s=skip")
    reply = _prompt(f"  Paths: [{' / '.join(menu_parts)}]: ")

    if reply in ("s", "S"):
        return None, True
    if reply in ("a", "A", ""):
        return None, False
    if reply.isdigit():
        idx = int(reply) - 1
        if 0 <= idx < len(path_opts):
            return path_opts[idx], False
    return None, False


def _prompt_axis_flags(flags_repr: str) -> tuple[str, bool]:
    """Prompt Axis 3 (flags). Returns (chosen_flags, skipped)."""
    reply = _prompt(f"  Flags: {flags_repr}  [l=literal / w=wildcard(*) / s=skip]: ")
    if reply in ("s", "S"):
        return flags_repr, True
    if reply in ("w", "W"):
        return "*", False
    return flags_repr, False


def _prompt_axis_tier(
    cwd: str,
    project_id: int | None,
    session_id: int | None,
) -> tuple[str, int | None, int | None, str]:
    """Prompt Axis 4 (tier). Returns (tier, tier_session_id, tier_project_id, outcome).

    outcome is 'ok' | 'skipped' | 'quit'.
    """
    reply = _prompt("  Tier:  [g=global / p=project / s=session / skip / q=quit]: ")

    if reply in ("q", "Q"):
        print("quitting")
        return "global", None, None, "quit"
    if reply == "skip":
        return "global", None, None, "skipped"
    if reply in ("p", "P"):
        if project_id is None:
            print(f"  (no project record for cwd={cwd} — skipping)")
            return "global", None, None, "skipped"
        return "project", None, project_id, "ok"
    if reply in ("s", "S"):
        if session_id is None:
            print(f"  (no session record for cwd={cwd} — skipping)")
            return "global", None, None, "skipped"
        return "session", session_id, None, "ok"
    return "global", None, None, "ok"


def _offer_subsume(
    candidate: Candidate,
    chosen_tier: str,
    tier_session_id: int | None,
    tier_project_id: int | None,
) -> None:
    """After promoting a wildcard-flags rule, offer to remove concrete siblings."""
    sibling_count = _count_concrete_siblings(
        candidate.verb,
        candidate.subcommand,
        chosen_tier,
        tier_session_id,
        tier_project_id,
    )
    if sibling_count == 0:
        return
    reply = _prompt(f"  Subsume {sibling_count} concrete sibling rule(s)? [Y/n]: ")
    if reply not in ("n", "N"):
        deleted = _subsume_siblings(
            candidate.verb,
            candidate.subcommand,
            chosen_tier,
            tier_session_id,
            tier_project_id,
        )
        print(f"  (subsumed {deleted} rule(s))")
    else:
        print("  (kept sibling rules)")


def _review_candidate(
    candidate: Candidate,
    home: str,
    cwd: str,
    project_root: str,
    project_id: int | None,
    session_id: int | None,
) -> str:
    """Walk the per-axis prompts for one candidate.

    Returns 'promoted' | 'skipped' | 'quit'.
    """
    sub_disp = candidate.subcommand or "-"
    flags_repr = _lib_db().minify_json(sorted(candidate.flags))
    print(
        f"\n--- {candidate.verb} {sub_disp}"
        f"  flags={flags_repr}"
        f"  (obs={candidate.observations}, sessions={candidate.distinct_sessions}) ---"
    )

    variants = _pattern_variants(candidate, home, cwd, project_root)
    verb_pattern = variants["verb_pattern"]
    path_specs: list[str] = variants["path_specs"]
    path_opts = _build_path_opts(path_specs, project_root, cwd, home)

    # Axis 1: Verb.
    chosen_verb = _prompt_axis_verb(candidate, verb_pattern)
    if chosen_verb is None:
        return "skipped"

    # Axis 2: Paths.
    chosen_path_spec, skipped = _prompt_axis_paths(path_opts)
    if skipped:
        return "skipped"

    # Axis 3: Flags.
    chosen_flags, skipped = _prompt_axis_flags(flags_repr)
    if skipped:
        return "skipped"

    # Axis 4: Tier.
    chosen_tier, tier_session_id, tier_project_id, outcome = _prompt_axis_tier(
        cwd, project_id, session_id
    )
    if outcome != "ok":
        return outcome

    # Promote.
    _do_promote(
        chosen_verb,
        candidate.subcommand,
        chosen_flags,
        chosen_path_spec,
        chosen_tier,
        tier_session_id,
        tier_project_id,
    )

    # Post-promote: offer to subsume concrete siblings when flags=*.
    if chosen_flags == "*":
        _offer_subsume(candidate, chosen_tier, tier_session_id, tier_project_id)

    return "promoted"


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nephoscope-review",
        description=(
            "Interactive review of accumulated permission candidates.\n"
            "\n"
            "For each candidate: per-axis prompts (verb / paths / flags) then\n"
            "tier (session / project / global). Promotes directly into the DB\n"
            "and syncs the JSON mirror."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.parse_args(argv)  # currently no flags; parse for --help support

    # Refresh candidates first (mirrors review.sh behaviour).
    conn = _connect()
    try:
        scan_candidates(conn)
        candidates = propose_promotions(conn)
    finally:
        conn.close()

    if not candidates:
        print("no promotion candidates meet thresholds")
        return 0

    total = len(candidates)
    print(f"reviewing {total} candidate(s) — (q)uit at any tier prompt to stop early")

    home, cwd, project_root, project_id, session_id = _resolve_context()

    promoted = 0
    skipped = 0

    for candidate in candidates:
        try:
            result = _review_candidate(
                candidate, home, cwd, project_root, project_id, session_id
            )
        except MirrorHashMismatch:
            print(
                "Settings file modified externally. Run"
                " '/nephoscope:permissions reconcile' and retry.",
                file=sys.stderr,
            )
            return 1

        if result == "promoted":
            promoted += 1
        elif result == "skipped":
            skipped += 1
        elif result == "quit":
            break

    print()
    print(f"summary: promoted {promoted}, skipped {skipped}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
