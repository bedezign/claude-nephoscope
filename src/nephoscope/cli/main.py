"""nephoscope — top-level CLI dispatcher.

Thin argparse dispatcher that routes user-facing subcommands to the
appropriate underlying implementation modules.  No business logic lives here.

Subcommands
-----------
stats          Hit-count statistics for permission rules.
status         Structured snapshot of the permission-rule database.
reconcile      Diff DB vs JSON mirror and (optionally) apply resolution.
mirror-status  Health table and workspace coverage for tracked settings files.
mirror-dry-run Preview what would be written to a settings file.
reload-hint    Touch a settings file's timestamp so Claude Code re-reads it.
init           Bootstrap the observations database.
migrate        Apply schema updates and normalize flags in the DB.
profiles       Manage meta-profiles and verb-type profiles.

Hook entry points (nephoscope-recorder, nephoscope-permissions-hook,
nephoscope-output-scanner) are intentionally not exposed here — Claude Code
calls them via absolute path, not via this dispatcher.
"""

from __future__ import annotations

import argparse
import sys

from nephoscope.cli import init_cmd, migrate_cmd
from nephoscope.cli.permissions_cmd import (
    _cmd_stats,
    _cmd_status,
    _dispatch_reconcile,
    _require_db,
    mirror_dry_run_cmd,
    mirror_status_cmd,
    reload_hint_cmd,
)


# ---------------------------------------------------------------------------
# Wrappers for functions that take positional arguments
# ---------------------------------------------------------------------------


def _dispatch_mirror_status(args: argparse.Namespace) -> int:
    err = _require_db("mirror-status", args)
    if err is not None:
        return err
    return mirror_status_cmd(args.db)


def _dispatch_mirror_dry_run(args: argparse.Namespace) -> int:
    err = _require_db("mirror-dry-run", args)
    if err is not None:
        return err
    return mirror_dry_run_cmd(args.db, args.target_path)


def _dispatch_reload_hint(args: argparse.Namespace) -> int:
    return reload_hint_cmd(args.settings_path)


def _dispatch_init(args: argparse.Namespace) -> int:
    argv: list[str] = list(args.rest)
    if args.db_path is not None:
        argv = ["--db-path", args.db_path] + argv
    if args.no_workspace_prompts:
        argv = ["--no-workspace-prompts"] + argv
    return init_cmd.main(argv)


def _dispatch_migrate(args: argparse.Namespace) -> int:
    return migrate_cmd.main(args.rest)


def _dispatch_profiles(args: argparse.Namespace) -> int:
    from nephoscope.learners.permission import profiles as profiles_mod

    return profiles_mod.main(args.rest)


# ---------------------------------------------------------------------------
# Shared --db argument helper
# ---------------------------------------------------------------------------


def _add_db_arg(subparser: argparse.ArgumentParser) -> None:
    import os

    subparser.add_argument(
        "--db",
        default=os.environ.get("OBSERVABILITY_DB", ""),
        dest="db",
        help=(
            "Path to the nephoscope observations database file.\n"
            "Defaults to the OBSERVABILITY_DB environment variable."
        ),
    )


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nephoscope",
        description=(
            "nephoscope — Claude Code session recorder and permission manager.\n"
            "\n"
            "Use one of the subcommands below to inspect statistics, manage\n"
            "permission rules, or maintain the observations database."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.required = True

    # -- stats ---------------------------------------------------------------
    stats_p = sub.add_parser(
        "stats",
        help="Show hit-count statistics for permission rules.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    _add_db_arg(stats_p)
    stats_p.add_argument(
        "--show-unused",
        action="store_true",
        default=False,
        dest="show_unused",
        help="List all rules that have never been matched (hit_count = 0).",
    )
    stats_p.set_defaults(func=_cmd_stats)

    # -- status --------------------------------------------------------------
    status_p = sub.add_parser(
        "status",
        help="Print a structured snapshot of the permission-rule database.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    _add_db_arg(status_p)
    status_p.add_argument(
        "--json",
        action="store_true",
        default=False,
        dest="json",
        help="Emit JSON instead of the human-readable summary.",
    )
    status_p.set_defaults(func=_cmd_status)

    # -- reconcile -----------------------------------------------------------
    rec_p = sub.add_parser(
        "reconcile",
        help="Compare the rules database with settings.json and make them match.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    _add_db_arg(rec_p)
    rec_p.add_argument(
        "--project",
        default=None,
        dest="target_path",
        help="Path to a project-specific settings.json file to reconcile.",
    )
    rec_p.add_argument(
        "--mode",
        default="interactive",
        choices=["interactive", "plan", "auto-db-wins", "auto-json-wins", "adopt"],
        help="How to resolve differences.",
    )
    rec_p.add_argument(
        "--force-rehash",
        action="store_true",
        default=False,
        dest="force_rehash",
        help="Recompute the stored hash from the current settings file.",
    )
    rec_p.set_defaults(func=_dispatch_reconcile)

    # -- mirror-status -------------------------------------------------------
    ms_p = sub.add_parser(
        "mirror-status",
        help="Show which settings files are tracked and their sync status.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    _add_db_arg(ms_p)
    ms_p.set_defaults(func=_dispatch_mirror_status)

    # -- mirror-dry-run ------------------------------------------------------
    mdr_p = sub.add_parser(
        "mirror-dry-run",
        help="Preview what would be written to a settings file, without writing it.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    _add_db_arg(mdr_p)
    mdr_p.add_argument(
        "--project",
        default=None,
        dest="target_path",
        help="Path to a project-specific settings.json file to preview.",
    )
    mdr_p.set_defaults(func=_dispatch_mirror_dry_run)

    # -- reload-hint ---------------------------------------------------------
    rh_p = sub.add_parser(
        "reload-hint",
        help="Refresh a settings file's timestamp so Claude Code re-reads it.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    rh_p.add_argument(
        "--settings-path",
        required=True,
        dest="settings_path",
        help="Path of the settings.json file whose timestamp should be refreshed.",
    )
    rh_p.set_defaults(func=_dispatch_reload_hint)

    # -- init ----------------------------------------------------------------
    init_p = sub.add_parser(
        "init",
        help="Bootstrap the nephoscope observations database.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    init_p.add_argument(
        "--db-path",
        default=None,
        help="Override the resolved DB path.",
    )
    init_p.add_argument(
        "--no-workspace-prompts",
        action="store_true",
        default=False,
        help="Skip interactive prompts for trusted directories and profiles.",
    )
    # REMAINDER lets any future init flags pass through without redefinition.
    init_p.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    init_p.set_defaults(func=_dispatch_init)

    # -- migrate -------------------------------------------------------------
    mig_p = sub.add_parser(
        "migrate",
        help="Apply schema updates and normalize flags in the observations DB.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    mig_p.add_argument(
        "--db",
        default=None,
        dest="db",
        metavar="PATH",
        help="DB path (default: OBSERVABILITY_DB).",
    )
    mig_p.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    mig_p.set_defaults(func=_dispatch_migrate)

    # -- profiles ------------------------------------------------------------
    prof_p = sub.add_parser(
        "profiles",
        help="Manage meta-profiles and verb-type profiles.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    prof_p.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    prof_p.set_defaults(func=_dispatch_profiles)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
