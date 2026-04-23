"""Command-level implementations for /permissions subcommands.

Implements the new subcommands introduced in Wave 4:
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
    from nephoscope.lib.mirror.reconcile import ReconcileReport, ReconcileError, reconcile

    from nephoscope.lib.mirror.writer import MirrorHashMismatch

    conn = _connect(db_path)
    try:
        report: ReconcileReport = reconcile(conn, Path(target_path), mode=mode)
    except ReconcileError as exc:
        print(f"reconcile error: {exc}", file=sys.stderr)
        return 1
    except MirrorHashMismatch as exc:
        print(f"reconcile error: hash mismatch — {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    first = " [first-touch: auto-adopt]" if report.first_touch else ""
    print(
        f"reconcile mode={report.mode}{first}: "
        f"applied={report.applied} "
        f"inserts={report.db_inserts} "
        f"deletes={report.db_deletes} "
        f"updates={report.db_updates}"
    )
    return 0


# ---------------------------------------------------------------------------
# mirror_status
# ---------------------------------------------------------------------------


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

    # Header
    print(f"{'scope':<20}  {'path':<50}  {'last_synced':<26}  hash_status")
    print("-" * 110)
    for row in rows:
        scope = row["scope"]
        path = row["path"] or "(not set)"
        last_synced = row["last_synced"] or "(never)"
        hash_status = row["hash_status"]
        print(f"{scope:<20}  {path:<50}  {last_synced:<26}  {hash_status}")
    return 0


def _hash_status(path_str: str | None, stored_hash: str | None) -> str:
    """Compute hash_status: stamped | null | mismatch."""
    if stored_hash is None:
        return "null"
    if path_str is None:
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
                "last_synced": gm[1] if len(gm) > 2 else gm[1],
                "hash_status": _hash_status(gm[0], gm[1]),
            }
        )
        # gm[2] is last_synced
        rows[-1]["last_synced"] = gm[2] if gm[2] is not None else None

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
        print(f"reload-hint: {p} does not exist; nothing to touch", file=sys.stderr)
        return 1
    p.touch()
    print(f"reload-hint: touched {p}")
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(
        prog="nephoscope.cli.permissions_cmd",
        description="Extended /permissions subcommands (Wave 4).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("reconcile", help="Diff DB vs JSON mirror and resolve.")
    r.add_argument("--db", default=os.environ.get("OBSERVABILITY_DB", ""), dest="db")
    r.add_argument(
        "--project",
        default=None,
        dest="target_path",
        help="Path to settings JSON to reconcile (default: global mirror).",
    )
    r.add_argument(
        "--mode",
        default="interactive",
        choices=["interactive", "plan", "auto-db-wins", "auto-json-wins", "adopt"],
    )

    ms = sub.add_parser("mirror-status", help="Print mirror table.")
    ms.add_argument("--db", default=os.environ.get("OBSERVABILITY_DB", ""), dest="db")

    md = sub.add_parser("mirror-dry-run", help="Print mirror JSON to stdout.")
    md.add_argument("--db", default=os.environ.get("OBSERVABILITY_DB", ""), dest="db")
    md.add_argument(
        "--project",
        default=None,
        dest="target_path",
        help="Project settings path (omit for global).",
    )

    rh = sub.add_parser("reload-hint", help="Touch settings.json mtime.")
    rh.add_argument("--db", default=os.environ.get("OBSERVABILITY_DB", ""), dest="db")
    rh.add_argument("--settings-path", required=True, dest="settings_path")

    args = parser.parse_args(argv)

    if args.cmd == "reconcile":
        if not args.db:
            print("reconcile: --db or OBSERVABILITY_DB required", file=sys.stderr)
            return 1
        if not args.target_path:
            # Resolve global mirror path from DB.
            conn = _connect(args.db)
            row = conn.execute(
                "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
            ).fetchone()
            conn.close()
            if not row or not row[0]:
                print("reconcile: global_mirror not configured", file=sys.stderr)
                return 1
            args.target_path = row[0]
        return reconcile_cmd(args.db, args.target_path, mode=args.mode)

    elif args.cmd == "mirror-status":
        if not args.db:
            print("mirror-status: --db or OBSERVABILITY_DB required", file=sys.stderr)
            return 1
        return mirror_status_cmd(args.db)

    elif args.cmd == "mirror-dry-run":
        if not args.db:
            print("mirror-dry-run: --db or OBSERVABILITY_DB required", file=sys.stderr)
            return 1
        return mirror_dry_run_cmd(args.db, args.target_path)

    elif args.cmd == "reload-hint":
        return reload_hint_cmd(args.settings_path)

    return 0  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
