"""Command-level implementations for /nephoscope:permissions subcommands.

Subcommands:
  reconcile      Diff DB vs JSON mirror and (optionally) apply resolution.
  mirror_status  Print a table of global mirror + registered projects.
  mirror_dry_run Build mirror content from DB and write JSON to stdout.
  reload_hint    Touch settings.json mtime to prompt Claude Code re-read.

All functions are pure Python so they can be unit-tested without spawning a
subprocess.  The CLI wrappers in commands/permissions.md delegate here via
``python -m commands.permissions_cmd <subcommand> ...``.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Helper: open an isolated connection (hash-check guard off for dry-run)
# ---------------------------------------------------------------------------


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------


def reconcile_cmd(
    db_path: str | Path,
    target_path: str | Path,
    mode: str = "interactive",
) -> int:
    """Run reconcile and print a summary.

    Returns 0 on success, 1 on ReconcileError.
    """
    from nephoscope.lib.mirror.reconcile import (
        ReconcileReport,
        ReconcileError,
        reconcile,
    )

    from nephoscope.lib.mirror.writer import MirrorHashMismatch

    conn = _connect(db_path)
    try:
        report: ReconcileReport = reconcile(conn, Path(target_path), mode=mode)
    except ReconcileError as exc:
        print(f"reconcile error: {exc}", file=sys.stderr)
        return 1
    except MirrorHashMismatch as exc:
        print(
            f"reconcile error: settings file was edited externally — {exc}",
            file=sys.stderr,
        )
        return 1
    finally:
        conn.close()

    first = (
        " (first-time setup: adopted what was in the file)"
        if report.first_touch
        else ""
    )
    print(
        f"reconcile finished (mode={report.mode}){first}:"
        f" applied {report.applied} change(s),"
        f" added {report.db_inserts} rule(s),"
        f" removed {report.db_deletes} rule(s),"
        f" updated {report.db_updates} rule(s)."
    )
    return 0


# ---------------------------------------------------------------------------
# mirror_status
# ---------------------------------------------------------------------------


_HASH_STATUS_WORDS = {
    "stamped": "in sync",
    "null": "not tracked",
    "mismatch": "file changed externally",
}


def mirror_status_cmd(db_path: str | Path) -> int:
    """Print a table: global mirror + each registered project.

    Columns: scope, path, last_synced, hash_status
    hash_status values: stamped | null | mismatch
    """
    conn = _connect(db_path)
    try:
        rows = _collect_mirror_rows(conn)
    finally:
        conn.close()

    # Header — column names are stable tokens (hash_status is asserted in tests).
    print(f"{'scope':<20}  {'path':<50}  {'last_synced':<26}  hash_status")
    print("-" * 110)
    for row in rows:
        scope = row["scope"]
        path = row["path"] or "(not set)"
        last_synced = row["last_synced"] or "(never)"
        hash_status = row["hash_status"]
        status_word = _HASH_STATUS_WORDS.get(hash_status)
        suffix = f" ({status_word})" if status_word else ""
        print(f"{scope:<20}  {path:<50}  {last_synced:<26}  {hash_status}{suffix}")
    return 0


def _hash_status(path_str: str | None, stored_hash: str | None) -> str:
    """Compute hash_status: stamped | null | mismatch."""
    if stored_hash is None or path_str is None:
        return "null"
    p = Path(path_str).expanduser()
    if not p.exists():
        return "null"
    import hashlib

    on_disk = hashlib.sha256(p.read_bytes()).hexdigest()
    return "stamped" if on_disk == stored_hash else "mismatch"


def _collect_mirror_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    gm = conn.execute(
        "SELECT settings_json_path, settings_json_sha256, settings_json_last_synced"
        " FROM global_mirror WHERE id = 1;"
    ).fetchone()
    if gm:
        rows.append(
            {
                "scope": "global",
                "path": gm[0],
                "last_synced": gm[2],
                "hash_status": _hash_status(gm[0], gm[1]),
            }
        )

    projects = conn.execute(
        "SELECT id, cwd, settings_json_path, settings_json_sha256, settings_json_last_synced"
        " FROM projects ORDER BY id;"
    ).fetchall()
    for proj in projects:
        proj_id, cwd, path, sha, last_sync = proj
        scope = f"project:{proj_id}({cwd[:12] if cwd else ''})"
        rows.append(
            {
                "scope": scope,
                "path": path,
                "last_synced": last_sync,
                "hash_status": _hash_status(path, sha),
            }
        )

    return rows


# ---------------------------------------------------------------------------
# mirror_dry_run
# ---------------------------------------------------------------------------


def mirror_dry_run_cmd(
    db_path: str | Path,
    target_path: str | Path | None = None,
    *,
    project_id: int | None = None,
) -> int:
    """Build mirror content from DB and write JSON to stdout.

    If target_path is given, project_id is auto-resolved from DB.
    If project_id is given directly, it is used as-is (None = global).
    """
    from nephoscope.lib.mirror.writer import _build_content  # noqa: PLC2701

    conn = _connect(db_path)
    try:
        if target_path is not None:
            project_id = _resolve_project_id(conn, Path(target_path))
        content = _build_content(conn, project_id)
    finally:
        conn.close()

    sys.stdout.buffer.write(content)
    sys.stdout.buffer.write(b"\n")
    return 0


def _resolve_project_id(conn: sqlite3.Connection, target_path: Path) -> int | None:
    """Return project_id for target_path, or None for global scope."""
    target_abs = str(target_path.expanduser().resolve())

    row = conn.execute(
        "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
    ).fetchone()
    if row and row[0]:
        if str(Path(row[0]).expanduser().resolve()) == target_abs:
            return None

    for proj_id, proj_path in conn.execute(
        "SELECT id, settings_json_path FROM projects"
        " WHERE settings_json_path IS NOT NULL;"
    ).fetchall():
        if str(Path(proj_path).expanduser().resolve()) == target_abs:
            return int(proj_id)

    return None  # treat as global when not found


# ---------------------------------------------------------------------------
# reload_hint
# ---------------------------------------------------------------------------


def reload_hint_cmd(settings_path: str | Path) -> int:
    """Touch settings_path mtime to prompt Claude Code to re-read settings.

    Only touches the given path — never the real ~/.claude/settings.json.
    """
    p = Path(settings_path)
    if not p.exists():
        print(
            f"reload-hint: the file {p} does not exist, so there is"
            " nothing to refresh.",
            file=sys.stderr,
        )
        return 1
    p.touch()
    print(
        f"reload-hint: refreshed {p} so Claude Code will pick up the"
        " latest permission rules."
    )
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(
        prog="nephoscope.cli.permissions_cmd",
        description=(
            "Housekeeping commands for nephoscope permission rules.\n"
            "\n"
            "These tools help you keep Claude Code's settings.json file\n"
            "and the nephoscope rules database in agreement, and to inspect\n"
            "or refresh them when needed."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    _db_help = (
        "Path to the nephoscope observations database file.\n"
        "Defaults to the OBSERVABILITY_DB environment variable."
    )

    r = sub.add_parser(
        "reconcile",
        help=(
            "Compare the rules database with the settings.json file and make\n"
            "them match."
        ),
        description=(
            "Compare the rules stored in the database with what is in the\n"
            "settings.json file, and apply a resolution — either by updating\n"
            "the database from the file, or writing the database rules back\n"
            "out to the file."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    r.add_argument(
        "--db", default=os.environ.get("OBSERVABILITY_DB", ""), dest="db", help=_db_help
    )
    r.add_argument(
        "--project",
        default=None,
        dest="target_path",
        help=(
            "Path to a project-specific settings.json file to reconcile.\n"
            "Leave out to reconcile the global settings file."
        ),
    )
    r.add_argument(
        "--mode",
        default="interactive",
        choices=["interactive", "plan", "auto-db-wins", "auto-json-wins", "adopt"],
        help=(
            "How to resolve differences. One of:\n"
            "  interactive       ask about each difference (the default)\n"
            "  plan              show the differences without changing anything\n"
            "  auto-db-wins      overwrite the file with the database rules\n"
            "  auto-json-wins    update the database to match the file\n"
            "  adopt             trust the file on the first sync only"
        ),
    )

    ms = sub.add_parser(
        "mirror-status",
        help="Show which settings files are being tracked and their status.",
        description=(
            "Print a table showing the global settings file and every project\n"
            "that has its own settings file, along with when each was last\n"
            "synchronized and whether it still matches what was recorded."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ms.add_argument(
        "--db", default=os.environ.get("OBSERVABILITY_DB", ""), dest="db", help=_db_help
    )

    md = sub.add_parser(
        "mirror-dry-run",
        help="Preview what would be written to a settings file, without writing it.",
        description=(
            "Build the JSON that would be written to the given settings file\n"
            "based on the current database rules, and print it to standard\n"
            "output. No file is changed."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    md.add_argument(
        "--db", default=os.environ.get("OBSERVABILITY_DB", ""), dest="db", help=_db_help
    )
    md.add_argument(
        "--project",
        default=None,
        dest="target_path",
        help=(
            "Path to a project-specific settings.json file to preview.\n"
            "Leave out to preview the global settings file."
        ),
    )

    rh = sub.add_parser(
        "reload-hint",
        help=("Refresh a settings file's timestamp so Claude Code re-reads it."),
        description=(
            "Update the modification time of a settings.json file so that\n"
            "Claude Code notices the change and re-reads the file. Useful\n"
            "when rules were changed in the database but the settings file\n"
            "itself has not been modified."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    rh.add_argument(
        "--db", default=os.environ.get("OBSERVABILITY_DB", ""), dest="db", help=_db_help
    )
    rh.add_argument(
        "--settings-path",
        required=True,
        dest="settings_path",
        help="Path of the settings.json file whose timestamp should be refreshed.",
    )

    args = parser.parse_args(argv)

    if args.cmd == "reconcile":
        if not args.db:
            print(
                "reconcile: please give a database path with --db, or set the"
                " OBSERVABILITY_DB environment variable.",
                file=sys.stderr,
            )
            return 1
        if not args.target_path:
            # Resolve global mirror path from DB.
            conn = _connect(args.db)
            row = conn.execute(
                "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
            ).fetchone()
            conn.close()
            if not row or not row[0]:
                print(
                    "reconcile: the global settings file path has not been"
                    " set up yet in the database.",
                    file=sys.stderr,
                )
                return 1
            args.target_path = row[0]
        return reconcile_cmd(args.db, args.target_path, mode=args.mode)

    elif args.cmd == "mirror-status":
        if not args.db:
            print(
                "mirror-status: please give a database path with --db, or set"
                " the OBSERVABILITY_DB environment variable.",
                file=sys.stderr,
            )
            return 1
        return mirror_status_cmd(args.db)

    elif args.cmd == "mirror-dry-run":
        if not args.db:
            print(
                "mirror-dry-run: please give a database path with --db, or set"
                " the OBSERVABILITY_DB environment variable.",
                file=sys.stderr,
            )
            return 1
        return mirror_dry_run_cmd(args.db, args.target_path)

    elif args.cmd == "reload-hint":
        return reload_hint_cmd(args.settings_path)

    return 0  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
