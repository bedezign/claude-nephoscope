"""Meta-profile loader for nephoscope permission profiles.

A meta-profile is a YAML file that groups ``permissions`` and ``verb_types``
entries under a single ``_meta`` header (id + description). Profiles provide
a convenient way to bulk-load related permission rules — e.g. all common
read-only git commands — without applying every bundled fixture file.

Two directories are searched: a bundled directory shipped with nephoscope and
a user directory under the Claude Code plugin-data path. On id collision,
bundled profiles win.

Both ``_bundled_dir()`` and ``_user_dir()`` are zero-arg functions evaluated lazily
on each call so ``monkeypatch.setenv`` overrides work in tests.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from nephoscope.learners.permission.seed import (
    _apply_permission_list,
    _apply_verb_type_list,
    _sync_global_mirror,
)
from nephoscope.lib.db import _now
from nephoscope.lib.paths import _plugin_data_dir

# ---------------------------------------------------------------------------
# Directory helpers (both lazy — evaluated on each call for testability)
# ---------------------------------------------------------------------------


def _bundled_dir() -> Path:
    """Resolve the bundled profiles directory.

    Evaluated lazily on each call so ``monkeypatch.setattr`` works in tests.
    """
    return Path(__file__).parent / "config" / "fixtures" / "meta-profiles"


def _user_dir() -> Path:
    """Resolve the user profiles directory.

    Order: ``${CLAUDE_PLUGIN_DATA}/profiles`` > ``~/.claude/plugins/data/nephoscope-bedezign-nephoscope/profiles``.
    Evaluated lazily on each call so ``monkeypatch.setenv("CLAUDE_PLUGIN_DATA", ...)`` works in tests.
    """
    plugin_data = _plugin_data_dir()
    if plugin_data is not None:
        return plugin_data / "profiles"
    return (
        Path.home()
        / ".claude"
        / "plugins"
        / "data"
        / "nephoscope-bedezign-nephoscope"
        / "profiles"
    )


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileEntry:
    """A discovered meta-profile entry."""

    id: str
    description: str
    path: Path
    source: str
    order: int = 999


# ---------------------------------------------------------------------------
# TTY I/O
# ---------------------------------------------------------------------------


def _read_line() -> str:
    """Read one line from stdin (interactive or piped — for tests)."""
    try:
        return input()
    except EOFError:
        return ""


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def _load_dir_entries(directory: Path, source: str) -> list[ProfileEntry]:
    """Load ProfileEntry objects from all valid YAML files in a directory.

    Silently skips files that cannot be parsed or lack a valid ``_meta.id``.
    """
    entries: list[ProfileEntry] = []
    for yaml_path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(
                f"warning: skipping malformed profile {yaml_path}: {exc}",
                file=sys.stderr,
            )
            continue
        if not isinstance(data, dict):
            continue
        meta = data.get("_meta")
        if not meta or not meta.get("id"):
            continue
        _raw_order = meta.get("order", 999)
        order = int(_raw_order) if isinstance(_raw_order, int) else 999
        entries.append(
            ProfileEntry(
                id=meta["id"],
                description=meta.get("description", ""),
                path=yaml_path,
                source=source,
                order=order,
            )
        )
    return entries


def list_profiles(
    bundled_dir: Path | None = None,
    user_dir: Path | None = None,
) -> list[ProfileEntry]:
    """Discover meta-profile YAML files from bundled and user directories.

    Args:
        bundled_dir: override the bundled profiles directory (default: ``_bundled_dir()``).
        user_dir: override the user profiles directory (default: ``_user_dir()``).

    Returns:
        List of ``ProfileEntry`` objects sorted by ``(order, id)``, with bundled
        entries before user entries when both have the same order. On id collision,
        bundled wins and the user-side entry is dropped entirely. Profiles without
        an ``order`` field in ``_meta`` default to order 999.
    """
    if bundled_dir is None:
        bundled_dir = _bundled_dir()
    if user_dir is None:
        user_dir = _user_dir()

    bundled_entries = _load_dir_entries(bundled_dir, "bundled")
    user_entries = _load_dir_entries(user_dir, "user")

    # Bundled wins on id collision — filter out user entries with colliding ids.
    bundled_ids = {e.id for e in bundled_entries}
    user_entries = [e for e in user_entries if e.id not in bundled_ids]

    combined = bundled_entries + user_entries
    return sorted(combined, key=lambda e: (e.order, e.id))


def apply_profile(conn: sqlite3.Connection, path: Path) -> tuple[int, int]:
    """Apply a meta-profile YAML file to the DB.

    Args:
        conn: SQLite connection.
        path: path to the meta-profile YAML file.

    Returns:
        ``(permissions_count, verb_types_count)`` — the number of entries
        processed from each section (not the number of rows actually inserted).

    Raises:
        ValueError: if the YAML is not a mapping, missing ``_meta``, missing
            ``_meta.id``, or any entry fails validation.
    """
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))

    if not isinstance(data, dict):
        raise ValueError(f"profile must be a YAML mapping, got {type(data).__name__}")

    if "_meta" not in data:
        raise ValueError("profile missing _meta block")

    meta = data["_meta"]
    if not isinstance(meta, dict):
        raise ValueError(f"profile _meta must be a mapping, got {type(meta).__name__}")
    if not meta.get("id"):
        raise ValueError("profile _meta.id is required — id must be a non-empty string")

    permissions: list[Any] = data.get("permissions") or []
    verb_types: list[Any] = data.get("verb_types") or []

    perms_count = _apply_permission_list(conn, permissions, _now())
    verb_types_count = _apply_verb_type_list(conn, verb_types)

    if permissions:
        _sync_global_mirror(conn)

    return perms_count, verb_types_count


def load_profile_by_id(
    profile_id: str,
    conn: sqlite3.Connection,
    bundled_dir: Path | None = None,
    user_dir: Path | None = None,
) -> tuple[int, int]:
    """Resolve a profile by id and apply it to the DB.

    Args:
        profile_id: the ``_meta.id`` value to look up.
        conn: SQLite connection.
        bundled_dir: override the bundled profiles directory.
        user_dir: override the user profiles directory.

    Returns:
        ``(permissions_count, verb_types_count)`` from ``apply_profile``.

    Raises:
        ValueError: if no profile with the given id is found.
    """
    entries = list_profiles(bundled_dir=bundled_dir, user_dir=user_dir)
    match = next((e for e in entries if e.id == profile_id), None)
    if match is None:
        raise ValueError(f"profile '{profile_id}' not found")
    return apply_profile(conn, match.path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``nephoscope-profiles`` console script."""
    parser = argparse.ArgumentParser(
        prog="nephoscope-profiles",
        description="Manage bundled and user meta-profiles for nephoscope.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "list",
        help="List available profiles.",
        description="List all available bundled and user profiles.",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    load_p = sub.add_parser(
        "load",
        help="Load a profile by id into the DB.",
        description="Load a profile by its id, with an interactive confirmation prompt.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    load_p.add_argument(
        "ids",
        nargs="+",
        help="Profile id(s) to load — space or comma separated.",
    )

    args = parser.parse_args(argv)

    if args.cmd == "list":
        _cmd_list()
        return 0
    if args.cmd == "load":
        return _cmd_load(args.ids)
    return 1  # pragma: no cover


def _parse_ids(raw: list[str]) -> list[str]:
    """Normalise space- and comma-separated id tokens into a flat deduplicated list."""
    seen: set[str] = set()
    out: list[str] = []
    for token in raw:
        for part in token.split(","):
            part = part.strip()
            if part and part not in seen:
                seen.add(part)
                out.append(part)
    return out


def _cmd_list() -> None:
    """Print a formatted table of available profiles."""
    entries = list_profiles()
    if not entries:
        print("No profiles found.")
        return

    id_w = max(len(e.id) for e in entries)
    src_w = max(len(e.source) for e in entries)
    id_w = max(id_w, len("id"))
    src_w = max(src_w, len("source"))

    header = f"  {'id':<{id_w}}  {'source':<{src_w}}  description"
    print(header)
    print("  " + "-" * (id_w + src_w + len("  description") + 4))
    for e in entries:
        print(f"  {e.id:<{id_w}}  {e.source:<{src_w}}  {e.description}")


def _cmd_load(raw_ids: list[str]) -> int:
    """Interactively load one or more profiles by id."""
    ids = _parse_ids(raw_ids)
    try:
        _user_dir().mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f'  Error: cannot create user profiles directory: {exc}', file=sys.stderr)
        return 1
    all_entries = {e.id: e for e in list_profiles()}

    # Resolve all ids upfront — fail fast if any unknown.
    resolved: list[tuple[ProfileEntry, int, int]] = []
    for pid in ids:
        entry = all_entries.get(pid)
        if entry is None:
            print(f"  Profile not found: {pid!r}", file=sys.stderr)
            return 1
        try:
            data = yaml.safe_load(entry.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"  Error reading {pid!r}: {exc}", file=sys.stderr)
            return 1
        perm_count = len(data.get("permissions") or [])
        vt_count = len(data.get("verb_types") or [])
        resolved.append((entry, perm_count, vt_count))

    # Show combined summary.
    for entry, perms, verbs in resolved:
        print(f"  {entry.id}: {perms} permissions, {verbs} verb_types")

    label = "this profile" if len(resolved) == 1 else f"these {len(resolved)} profiles"
    print(f"  Load {label}? [Y/n]: ", end="", flush=True)
    answer = _read_line().strip()

    if answer.strip() not in ("", "y", "Y", "yes", "YES"):
        print("  Aborted.")
        return 0

    from nephoscope.learners.permission.learner import connect  # noqa: PLC0415

    conn = connect()
    try:
        total_perms = total_verbs = 0
        for entry, _, _ in resolved:
            p, v = load_profile_by_id(entry.id, conn)
            total_perms += p
            total_verbs += v
    finally:
        conn.close()

    print(f"  loaded: {total_perms} permissions, {total_verbs} verb_types")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
