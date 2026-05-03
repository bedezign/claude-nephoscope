"""Per-axis review CLI for permission candidates.

Two driving modes:

  * **Interactive walker** (no subcommand) — prompts the human reviewer per axis
    (verb / paths / flags / tier) for every eligible candidate. Replaces
    ``src/nephoscope/learners/permission/scripts/review.sh``.

  * **Non-interactive subcommands** (``list`` / ``show`` / ``commit``) — emit
    the same axis choices as JSON (or human-readable text with ``--text``) so
    a caller without a TTY can drive the workflow one candidate at a time.

Axes (shared between both modes):

  Axis 1 (verb)  — only when ``to_pattern_form`` finds a $VAR substitution in
                   the verb (i.e. verb is an absolute path under HOME/CWD/
                   PROJECT_ROOT).
  Axis 2 (paths) — path constraint for the rule_shape.path_spec.
  Axis 3 (flags) — literal flags array vs wildcard "*".
  Axis 4 (tier)  — permission tier (global / project / session).
  Post-promote   — when flags="*" and concrete sibling rules exist: the
                   interactive walker offers subsume; ``commit`` reports the
                   count so the caller can decide whether to follow up.

Promotion delegates to the learner module (no external subprocess).
MirrorHashMismatch is caught and surfaces the same wording as the legacy bash
script.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from nephoscope.learners.permission.learner import (
    Candidate,
    connect,
    lib_db,
    parse_flags_arg,
    resolve_tier_ids,
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
        conn = connect()
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

    flags_json = lib_db().minify_json(sorted(candidate.flags))
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

    flags_parsed = parse_flags_arg(flags_json)
    conn = connect()
    try:
        sess_id, proj_id = resolve_tier_ids(tier, session_id, project_id)
        db = lib_db()
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
    conn = connect()
    try:
        sess_id, proj_id = resolve_tier_ids(tier, session_id, project_id)
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

    conn = connect()
    try:
        sess_id, proj_id = resolve_tier_ids(tier, session_id, project_id)
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
    *,
    suggested: str | None = None,
) -> list[str]:
    """Build the ordered, deduplicated path option menu for Axis 2.

    When ``suggested`` is set (a cross-project generalization from
    ``generalize_path_spec``), it is inserted at position 0 so the reviewer
    sees it first.
    """
    seen_ps: set[str] = set()
    path_opts: list[str] = []

    def _add_ps(opt: str) -> None:
        if opt not in seen_ps:
            seen_ps.add(opt)
            path_opts.append(opt)

    if suggested:
        _add_ps(suggested)
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
    if not verb_pattern:
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
    flags_repr = lib_db().minify_json(sorted(candidate.flags))
    print(
        f"\n--- {candidate.verb} {sub_disp}"
        f"  flags={flags_repr}"
        f"  (obs={candidate.observations}, sessions={candidate.distinct_sessions}) ---"
    )

    variants = _pattern_variants(candidate, home, cwd, project_root)
    verb_pattern = variants["verb_pattern"]
    path_specs: list[str] = variants["path_specs"]
    path_opts = _build_path_opts(
        path_specs, project_root, cwd, home, suggested=candidate.suggested_path_spec
    )

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
# Non-interactive helpers (JSON-friendly views shared by list / show / commit)
# ---------------------------------------------------------------------------


_HASH_MISMATCH_MSG = (
    "Settings file modified externally. Run"
    " '/nephoscope:permissions reconcile' and retry."
)


def _summary_dict(c: Candidate) -> dict:
    """Compact list-view summary for one candidate."""
    return {
        "id": c.id,
        "verb": c.verb,
        "subcommand": c.subcommand,
        "flags": sorted(c.flags),
        "observations": c.observations,
        "distinct_sessions": c.distinct_sessions,
    }


def _tier_status(value: int | None, label: str, cwd: str) -> str:
    return "ok" if value is not None else f"no {label} record for cwd={cwd}"


def _show_dict(
    c: Candidate,
    home: str,
    cwd: str,
    project_root: str,
    project_id: int | None,
    session_id: int | None,
) -> dict:
    """Full per-axis choice set + recommendation for one candidate."""
    variants = _pattern_variants(c, home, cwd, project_root)
    path_opts = _build_path_opts(
        variants["path_specs"],
        project_root,
        cwd,
        home,
        suggested=c.suggested_path_spec,
    )
    return {
        **_summary_dict(c),
        "context": {
            "home": home or None,
            "cwd": cwd or None,
            "project_root": project_root or None,
        },
        "axes": {
            "verb": {
                "literal": c.verb,
                "generalize": variants["verb_pattern"],
            },
            "paths": {
                "any_label": "any (no path constraint)",
                "options": [
                    {"index": i + 1, "spec": opt} for i, opt in enumerate(path_opts)
                ],
                "suggested": c.suggested_path_spec,
            },
            "flags": {
                "literal": variants["flags_literal"],
                "wildcard": "*",
            },
            "tier": {
                "global": "ok",
                "project": _tier_status(project_id, "project", cwd),
                "session": _tier_status(session_id, "session", cwd),
            },
        },
    }


def _print_show_text(detail: dict) -> None:
    """Human-readable rendering for `show --text`."""
    print(f"candidate #{detail['id']}: {detail['verb']}")
    if detail["subcommand"]:
        print(f"  subcommand: {detail['subcommand']}")
    print(f"  observations: {detail['observations']}")
    print(f"  distinct_sessions: {detail['distinct_sessions']}")
    print(f"  flags: {detail['flags']}")
    axes = detail["axes"]
    print("  verb axis:")
    print(f"    literal:    {axes['verb']['literal']}")
    print(f"    generalize: {axes['verb']['generalize'] or '(none)'}")
    print("  paths axis:")
    print(f"    any:  {axes['paths']['any_label']}")
    for opt in axes["paths"]["options"]:
        print(f"    {opt['index']}: {opt['spec']}")
    if axes["paths"]["suggested"]:
        print(f"    suggested: {axes['paths']['suggested']}")
    print("  flags axis:")
    print(f"    literal:  {axes['flags']['literal']}")
    print(f"    wildcard: {axes['flags']['wildcard']}")
    print("  tier axis:")
    for tier in ("global", "project", "session"):
        print(f"    {tier}: {axes['tier'][tier]}")


def _load_candidates() -> list[Candidate]:
    """Refresh + propose; close the connection. Mirrors the interactive flow."""
    conn = connect()
    try:
        scan_candidates(conn)
        return propose_promotions(conn)
    finally:
        conn.close()


def _find_candidate(candidate_id: int) -> Candidate | None:
    return next((c for c in _load_candidates() if c.id == candidate_id), None)


# ---------------------------------------------------------------------------
# Non-interactive subcommand handlers
# ---------------------------------------------------------------------------


def _cmd_list(args: argparse.Namespace) -> None:
    candidates = _load_candidates()
    rows = [_summary_dict(c) for c in candidates]
    if args.text:
        if not rows:
            print("no promotion candidates meet thresholds")
            return
        for r in rows:
            sub = r["subcommand"] or "-"
            flags = json.dumps(r["flags"], separators=(",", ":"))
            print(
                f"#{r['id']:>4}  {r['verb']:<20}  {sub:<10}  flags={flags:<24}"
                f"  obs={r['observations']:<5} sessions={r['distinct_sessions']}"
            )
        return
    print(json.dumps(rows, indent=2))


def _cmd_show(args: argparse.Namespace) -> int:
    candidate = _find_candidate(args.candidate_id)
    if candidate is None:
        print(
            f"no eligible candidate with id={args.candidate_id}",
            file=sys.stderr,
        )
        return 1
    home, cwd, project_root, project_id, session_id = _resolve_context()
    detail = _show_dict(candidate, home, cwd, project_root, project_id, session_id)
    if args.text:
        _print_show_text(detail)
    else:
        print(json.dumps(detail, indent=2))
    return 0


def _resolve_paths_arg(raw: str, path_opts: list[str]) -> tuple[str | None, str | None]:
    """Map ``--paths`` value to a (chosen_spec, error) pair.

    Returns:
        (None, None)       — raw=="any"; no path constraint.
        (spec, None)       — raw matched a path option (by index or literal).
        (None, error_msg)  — raw was invalid; ``error_msg`` describes why.
    """
    if raw == "any":
        return None, None
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(path_opts):
            return path_opts[idx], None
        return None, (
            f"--paths {raw}: index out of range (1..{len(path_opts)})"
            if path_opts
            else f"--paths {raw}: no path options for this candidate"
        )
    if raw in path_opts:
        return raw, None
    return None, (
        f"--paths {raw}: not in this candidate's options"
        f" ({', '.join(path_opts) or 'no options available'})"
    )


def _cmd_commit(args: argparse.Namespace) -> int:
    candidate = _find_candidate(args.candidate_id)
    if candidate is None:
        print(
            f"no eligible candidate with id={args.candidate_id}",
            file=sys.stderr,
        )
        return 1

    home, cwd, project_root, project_id, session_id = _resolve_context()
    variants = _pattern_variants(candidate, home, cwd, project_root)

    if args.verb == "generalize":
        verb_pattern = variants["verb_pattern"]
        if not verb_pattern:
            print(
                "--verb generalize: no $VAR pattern available for this candidate",
                file=sys.stderr,
            )
            return 1
        chosen_verb = verb_pattern
    else:
        chosen_verb = candidate.verb

    chosen_flags = "*" if args.flags == "wildcard" else variants["flags_literal"]

    path_opts = _build_path_opts(
        variants["path_specs"],
        project_root,
        cwd,
        home,
        suggested=candidate.suggested_path_spec,
    )
    chosen_path, err = _resolve_paths_arg(args.paths, path_opts)
    if err is not None:
        print(err, file=sys.stderr)
        return 1

    tier_session: int | None = None
    tier_project: int | None = None
    if args.tier == "project":
        if project_id is None:
            print(
                f"--tier project: no project record for cwd={cwd}",
                file=sys.stderr,
            )
            return 1
        tier_project = project_id
    elif args.tier == "session":
        if session_id is None:
            print(
                f"--tier session: no session record for cwd={cwd}",
                file=sys.stderr,
            )
            return 1
        tier_session = session_id

    try:
        _do_promote(
            chosen_verb,
            candidate.subcommand,
            chosen_flags,
            chosen_path,
            args.tier,
            tier_session,
            tier_project,
        )
    except MirrorHashMismatch:
        print(_HASH_MISMATCH_MSG, file=sys.stderr)
        return 1

    siblings = 0
    if chosen_flags == "*":
        siblings = _count_concrete_siblings(
            candidate.verb,
            candidate.subcommand,
            args.tier,
            tier_session,
            tier_project,
        )

    print(
        json.dumps(
            {
                "result": "promoted",
                "candidate_id": candidate.id,
                "verb": chosen_verb,
                "subcommand": candidate.subcommand,
                "flags": chosen_flags,
                "path_spec": chosen_path,
                "tier": args.tier,
                "subsumable_concrete_siblings": siblings,
            },
            indent=2,
        )
    )
    return 0


# ---------------------------------------------------------------------------
# Interactive walker (default mode — no subcommand)
# ---------------------------------------------------------------------------


def _cmd_interactive() -> int:
    candidates = _load_candidates()

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
            print(_HASH_MISMATCH_MSG, file=sys.stderr)
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


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nephoscope-review",
        description=(
            "Review accumulated permission candidates.\n"
            "\n"
            "Without a subcommand, walks each candidate interactively (verb /\n"
            "paths / flags / tier prompts).\n"
            "\n"
            "With a subcommand, exposes the same axis choices as JSON so a\n"
            "non-TTY caller can drive list → show → commit one candidate at\n"
            "a time."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd")

    p_list = sub.add_parser(
        "list",
        help="emit eligible candidates (id + summary) as JSON or text",
    )
    p_list.add_argument("--text", action="store_true", help="human-readable format")

    p_show = sub.add_parser(
        "show",
        help="emit the four-axis choice set for one candidate",
    )
    p_show.add_argument("candidate_id", type=int)
    p_show.add_argument("--text", action="store_true", help="human-readable format")

    p_commit = sub.add_parser(
        "commit",
        help="promote one candidate with explicit per-axis choices",
    )
    p_commit.add_argument("candidate_id", type=int)
    p_commit.add_argument(
        "--verb",
        choices=["literal", "generalize"],
        default="literal",
        help="use the candidate's verb (literal) or its $VAR pattern (generalize)",
    )
    p_commit.add_argument(
        "--paths",
        default="any",
        help="'any', a 1-based index from `show`, or one of its option spec strings",
    )
    p_commit.add_argument(
        "--flags",
        choices=["literal", "wildcard"],
        default="literal",
        help="store the literal flags (literal) or replace with '*' (wildcard)",
    )
    p_commit.add_argument(
        "--tier",
        choices=["global", "project", "session"],
        required=True,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "list":
        _cmd_list(args)
        return 0
    if args.cmd == "show":
        return _cmd_show(args)
    if args.cmd == "commit":
        return _cmd_commit(args)
    return _cmd_interactive()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
