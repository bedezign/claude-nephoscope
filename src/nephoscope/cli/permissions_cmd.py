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

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from nephoscope.lib.mirror.permissions_hash import settings_permissions_hash

_SETTINGS_JSON_PATH_QUERY = "SELECT settings_json_path FROM global_mirror WHERE id = 1;"

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


def reset_mirror_hash(conn: sqlite3.Connection) -> None:
    """Recompute and store the current on-disk hash in global_mirror.

    Reads settings_json_path from global_mirror, hashes the permissions slice
    of that file, and writes the result back to settings_json_sha256.  This
    clears a stale stored hash so the next reconcile passes the hash-check gate
    inside the atomic writer.

    No-op when settings_json_path is NULL or the file does not exist.
    """
    row = conn.execute(_SETTINGS_JSON_PATH_QUERY).fetchone()
    if row is None or row[0] is None:
        return
    path = Path(row[0]).expanduser()
    if not path.exists():
        return
    new_hash = settings_permissions_hash(path.read_bytes())
    conn.execute(
        "UPDATE global_mirror SET settings_json_sha256 = ? WHERE id = 1;",
        (new_hash,),
    )


def reconcile_cmd(
    db_path: str | Path,
    target_path: str | Path,
    mode: str = "interactive",
    *,
    force_rehash: bool = False,
) -> int:
    """Run reconcile and print a summary.

    When *force_rehash* is True, recompute global_mirror.settings_json_sha256
    from the on-disk file before reconciling.  Use this to recover from a
    MirrorHashMismatch caused by direct edits to settings.json outside of
    nephoscope (e.g. via a text editor).

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
        if force_rehash:
            reset_mirror_hash(conn)
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

    After the table, prints a Workspace Coverage section when workspace_roots
    are configured in nephoscope.toml.
    """
    conn = _connect(db_path)
    try:
        rows = _collect_mirror_rows(conn)
        gm_row = conn.execute(_SETTINGS_JSON_PATH_QUERY).fetchone()
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

    global_settings_path = Path(gm_row[0]) if gm_row and gm_row[0] else Path("")
    _print_workspace_coverage(global_settings_path)
    return 0


def _hash_status(path_str: str | None, stored_hash: str | None) -> str:
    """Compute hash_status: stamped | null | mismatch.

    Returns "mismatch" when the file is unreadable, empty, or contains
    malformed JSON — same UX as a real hash mismatch.
    """
    if stored_hash is None or path_str is None:
        return "null"
    p = Path(path_str).expanduser()
    if not p.exists():
        return "null"
    try:
        on_disk = settings_permissions_hash(p.read_bytes())
    except (ValueError, TypeError):
        return "mismatch"
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

    row = conn.execute(_SETTINGS_JSON_PATH_QUERY).fetchone()
    if row and row[0] and str(Path(row[0]).expanduser().resolve()) == target_abs:
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
# workspace_coverage
# ---------------------------------------------------------------------------


def _print_workspace_coverage(settings_path: Path) -> None:
    """Print a Workspace Coverage section showing which workspace roots are covered.

    Covered means the global settings.json has a Write(<root>/**) entry in
    _nephoscopeAllowedTools — the marker written by the mirror writer when
    workspace_roots are configured.

    Skipped entirely when workspace_roots is empty.
    """
    from nephoscope.config import get_config

    # uses cached config; reflects state at process start
    cfg = get_config()
    if not cfg.trusted_dirs:
        return

    covered_entries: list[str] = []
    if settings_path.exists():
        try:
            data: dict = json.loads(settings_path.read_text())
            covered_entries = data.get("_nephoscopeAllowedTools", [])
        except json.JSONDecodeError:
            covered_entries = []

    print("\nWorkspace coverage:")
    any_uncovered = False
    for root in cfg.trusted_dirs:
        resolved = os.path.realpath(os.path.expanduser(root))
        marker = f"Write({resolved}/**)"
        if marker in covered_entries:
            symbol = "✓"
        else:
            symbol = "✗"
            any_uncovered = True
        print(f"  {resolved}  {symbol}")
    if any_uncovered:
        print(
            "  Run 'nephoscope-permissions reconcile' to generate allowedTools entries."
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _add_db_arg(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--db",
        default=os.environ.get("OBSERVABILITY_DB", ""),
        dest="db",
        help=(
            "Path to the nephoscope observations database file.\n"
            "Defaults to the OBSERVABILITY_DB environment variable."
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
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

    r = sub.add_parser(
        "reconcile",
        help="Compare the rules database with the settings.json file and make them match.",
        description=(
            "Compare the rules stored in the database with what is in the\n"
            "settings.json file, and apply a resolution — either by updating\n"
            "the database from the file, or writing the database rules back\n"
            "out to the file."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    _add_db_arg(r)
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
    r.add_argument(
        "--force-rehash",
        action="store_true",
        default=False,
        dest="force_rehash",
        help=(
            "Recompute the stored hash from the current settings file before\n"
            "reconciling. Use this to recover from a hash mismatch caused by\n"
            "editing settings.json directly outside of nephoscope."
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
    _add_db_arg(ms)

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
    _add_db_arg(md)
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
        help="Refresh a settings file's timestamp so Claude Code re-reads it.",
        description=(
            "Update the modification time of a settings.json file so that\n"
            "Claude Code notices the change and re-reads the file. Useful\n"
            "when rules were changed in the database but the settings file\n"
            "itself has not been modified."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    _add_db_arg(rh)
    rh.add_argument(
        "--settings-path",
        required=True,
        dest="settings_path",
        help="Path of the settings.json file whose timestamp should be refreshed.",
    )

    return parser


def _require_db(subcommand: str, args: argparse.Namespace) -> int | None:
    """Return 1 with an error message when --db is not set; None when set."""
    if not args.db:
        print(
            f"{subcommand}: please give a database path with --db, or set"
            " the OBSERVABILITY_DB environment variable.",
            file=sys.stderr,
        )
        return 1
    return None


def _dispatch_reconcile(args: argparse.Namespace) -> int:
    err = _require_db("reconcile", args)
    if err is not None:
        return err
    if not args.target_path:
        conn = _connect(args.db)
        row = conn.execute(_SETTINGS_JSON_PATH_QUERY).fetchone()
        conn.close()
        if not row or not row[0]:
            print(
                "reconcile: the global settings file path has not been"
                " set up yet in the database.",
                file=sys.stderr,
            )
            return 1
        args.target_path = row[0]
    return reconcile_cmd(
        args.db,
        args.target_path,
        mode=args.mode,
        force_rehash=args.force_rehash,
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    match args.cmd:
        case "reconcile":
            return _dispatch_reconcile(args)
        case "mirror-status":
            err = _require_db("mirror-status", args)
            if err is not None:
                return err
            return mirror_status_cmd(args.db)
        case "mirror-dry-run":
            err = _require_db("mirror-dry-run", args)
            if err is not None:
                return err
            return mirror_dry_run_cmd(args.db, args.target_path)
        case "reload-hint":
            return reload_hint_cmd(args.settings_path)
        case _:
            return 0  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
