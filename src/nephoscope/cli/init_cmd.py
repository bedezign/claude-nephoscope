"""Explicit DB bootstrap CLI.

Usage:
    nephoscope-init [--db-path PATH] [--no-workspace-prompts]

Materialises the observations SQLite file at the resolved path and applies
``lib/schema.sql``. Useful for pre-seeding a non-default
``$OBSERVABILITY_DB`` before first session, or for verifying install during
debugging. Safe to re-run — existing sessions, tool_calls, and permissions
are never deleted. The schema loader is a no-op on an existing database;
fixture seeding only adds new permission shapes without removing old ones.

Resolution order:
    ``--db-path`` arg > ``$OBSERVABILITY_DB`` > config ``database`` key
    > ``${CLAUDE_PLUGIN_DATA}/observations.db``.

After DB bootstrap, ``nephoscope-init`` seeds the ``global_mirror`` singleton
row (id=1) pointing at ``~/.claude/settings.json`` if it does not yet exist.
This singleton is required before any trusted-directory rule can be written to
the global mirror.

An optional interactive phase then prompts for workspace_roots (project
directories to pre-approve for file access). This phase writes a Full Access
``$TRUSTED_DIR/**`` Allow rule to the DB *and* syncs it to
``~/.claude/settings.json``. It requires an interactive terminal — it is
silently skipped when stdin is not a TTY (e.g. when run from inside Claude
Code or piped). Use ``--no-workspace-prompts`` to suppress it explicitly in
scripts or CI.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from nephoscope.config import _coerce_trusted_dirs, _config_path
from nephoscope.lib.db import _open
from nephoscope.lib.paths import canonicalize, observations_db_path

_FIXTURES_DIR = (
    Path(__file__).resolve().parent.parent
    / "learners"
    / "permission"
    / "config"
    / "fixtures"
)

# Fixtures loaded automatically on first install, in application order.
# Each path is relative to _FIXTURES_DIR.
_AUTO_LOAD_FIXTURES: list[str] = [
    "safe_shapes.yaml",
    "credential_leaks.yaml",
    "secret_manager_standalones.yaml",
]

# Meta-profiles loaded automatically on first install, in application order.
# Each path is relative to _FIXTURES_DIR/meta-profiles/.
# These are bundled security baselines — not opt-in, always applied on init.
_AUTO_LOAD_META_PROFILES: list[str] = [
    "credential-file-tools.yaml",
]

# Verb-category profiles applied automatically on first install.
# Each path is relative to _FIXTURES_DIR/profiles/.
# Other profiles (python, javascript, task-runners, secrets-management) are opt-in.
_AUTO_LOAD_VERB_PROFILES: list[str] = [
    "core.yaml",
    "shell-scripting.yaml",
]


def _ensure_database_in_config(db_path: Path) -> None:  # pyright: ignore[reportUnusedFunction]
    """Write the ``database`` key to the config file if not already present.

    Reads the existing TOML dict, skips silently if ``database`` is already
    set, otherwise writes the key and clears the ``get_config`` cache so the
    next call sees the freshly-written value.

    Called from the SessionStart hook with the resolved ``$CLAUDE_PLUGIN_DATA``
    path — the only site guaranteed to have ``CLAUDE_PLUGIN_DATA`` in env.
    """
    from nephoscope.config import get_config

    config_path = _config_path()
    existing = _load_config_dict(config_path)
    if existing.get("database"):
        return
    existing["database"] = str(db_path)
    _write_config_file(config_path, existing)
    get_config.cache_clear()


def _resolve_target(cli_path: str | None) -> Path:
    """Return the DB path honouring the CLI override first, then env/defaults."""
    if cli_path:
        return Path(cli_path)
    try:
        return observations_db_path()
    except RuntimeError:
        print(
            "nephoscope: no database path configured — set OBSERVABILITY_DB,"
            " add 'database = ...' to config, or pass --db-path.",
            file=sys.stderr,
        )
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Workspace-roots configuration helpers
# ---------------------------------------------------------------------------


def _load_config_dict(path: Path) -> dict[str, object]:
    """Load existing TOML as a plain dict; return empty dict if absent."""
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)  # type: ignore[return-value]


_TOML_CTRL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def _contains_control_chars(s: str) -> bool:
    """Return True when *s* contains characters forbidden in TOML basic strings.

    TOML basic strings (``"..."``) cannot contain raw control characters in the
    range 0x00-0x08, 0x0A-0x1F, or 0x7F.  0x09 (tab) is allowed by the spec.
    """
    return bool(_TOML_CTRL_RE.search(s))


def _escape_toml_string(s: str) -> str:
    """Escape a string for use inside a TOML basic string (double-quoted).

    TOML basic strings treat ``\\`` as an escape prefix and ``"`` as a
    terminator, so both must be escaped before embedding in ``"..."``.

    Raises ``ValueError`` when *s* contains raw control characters (0x00-0x08,
    0x0A-0x1F, 0x7F) that TOML basic strings cannot represent without escaping.
    Directory paths containing these characters are almost certainly a mistake.
    """
    if _contains_control_chars(s):
        raise ValueError(
            f"path contains a control character and cannot be stored in TOML: {s!r}"
        )
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _write_config_file(path: Path, data: dict[str, Any]) -> None:
    """Write *data* to *path* as minimal TOML using an atomic temp+rename.

    Supports bool, list[str], and str values — sufficient for NephoscopeConfig.
    Parent directory is created if needed.

    The write is atomic on POSIX: content is flushed and fsync'd to a sibling
    ``.tmp`` file, then ``os.rename`` replaces the target in a single syscall.
    The ``.tmp`` file is removed on failure to avoid stale fragments.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, list):
            quoted = ", ".join(f'"{_escape_toml_string(v)}"' for v in value)
            lines.append(f"{key} = [{quoted}]")
        elif isinstance(value, str):
            lines.append(f'{key} = "{_escape_toml_string(value)}"')
        else:
            raise TypeError(
                f"_write_config_file: unsupported value type {type(value)!r} for key {key!r}"
            )
    content = ("\n".join(lines) + "\n").encode("utf-8")
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as fh:
            tmp_path = Path(fh.name)
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(tmp_path, path)
    except BaseException:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _prompt_for_paths() -> list[str]:
    """Interactively collect directory paths from the user.

    Returns a list of resolved absolute paths for entries that exist on disk.
    Returns an empty list if the user presses Enter immediately.
    """
    print(
        "Configure trusted directories.\n"
        "Each directory is a top-level project directory. nephoscope will pre-approve\n"
        "Read, Edit, and Write access to every file beneath it (path/**).\n"
        "Enter one path per line — blank line to finish, or just Enter to skip."
    )
    roots: list[str] = []
    while True:
        try:
            raw = input("Enter path (or blank to finish): ").strip()
        except EOFError:
            break
        if not raw:
            break
        resolved = os.path.realpath(os.path.expanduser(raw))
        if _contains_control_chars(resolved):
            print(
                f"  warning: path contains a control character, skipping: {resolved!r}",
                file=sys.stderr,
            )
            continue
        if not os.path.isdir(resolved):
            print(f"  warning: not a directory, skipping: {resolved}", file=sys.stderr)
            continue
        roots.append(resolved)
    return roots


def _prompt_for_profiles() -> list[Path]:
    """Return selected optional profile paths from an interactive numbered menu.

    Prints a numbered list of available profiles and prompts the user for
    space-separated numbers. Returns an empty list when no profiles are
    available or the user skips (blank input / EOF). Emits a warning to stderr
    for each token that is not a valid number or is out of range.
    """
    from nephoscope.learners.permission.profiles import list_profiles

    profiles = list_profiles()
    if not profiles:
        return []
    print("\nOptional permission profiles — select which to pre-approve:")
    for i, entry in enumerate(profiles, start=1):
        print(f"  {i}) {entry.id:<20} {entry.description}")
    print()
    try:
        raw = input("Enter numbers separated by spaces (or Enter to skip): ").strip()
    except EOFError:
        return []
    if not raw:
        return []
    if raw.lower() == "all":
        return [p.path for p in profiles]
    selected: list[Path] = []
    seen: set[Path] = set()
    for token in raw.split():
        if not token.isdecimal():
            print(
                f"warning: '{token}' is not a number — enter numbers from 1 to {len(profiles)}",
                file=sys.stderr,
            )
            continue
        idx = int(token) - 1
        if not (0 <= idx < len(profiles)):
            print(
                f"warning: {token} is out of range — enter numbers from 1 to {len(profiles)}",
                file=sys.stderr,
            )
            continue
        if profiles[idx].path not in seen:
            seen.add(profiles[idx].path)
            selected.append(profiles[idx].path)
    return selected


def _seed_optional_profiles(
    conn: sqlite3.Connection,
    paths: list[Path],
) -> None:
    """Apply each selected optional profile to the DB."""
    from nephoscope.learners.permission.profiles import apply_profile

    for path in paths:
        try:
            apply_profile(conn, path)
        except Exception as exc:  # noqa: BLE001 — optional profile load must not abort init
            print(f"  warning: could not load {path.name}: {exc}", file=sys.stderr)


def _seed_global_mirror_singleton(conn: sqlite3.Connection) -> None:
    """Ensure the global_mirror singleton row (id=1) exists.

    Uses INSERT OR IGNORE so a second call is always a no-op.
    The ``settings_json_path`` is set to ``~/.claude/settings.json``
    canonicalized — the standard location Claude Code reads and writes.
    This row is required by ``sync_affected`` → ``sync_global`` →
    ``_read_global_meta``; without it, seeding a Full Access rule during
    ``_append_trusted_dirs`` raises RuntimeError.
    """
    settings_path = canonicalize(Path.home() / ".claude" / "settings.json")
    conn.execute(
        "INSERT OR IGNORE INTO global_mirror"
        " (id, settings_json_path, settings_json_sha256, settings_json_last_synced)"
        " VALUES (1, ?, NULL, NULL);",
        (settings_path,),
    )


def _seed_full_access_rules(conn: sqlite3.Connection) -> None:
    """Ensure a global Allow rule for $TRUSTED_DIR/** exists for each file-tool verb.

    Seeds one ``rule_shapes`` row per verb in ``FILE_VERBS`` (Read, Write, Edit,
    MultiEdit, NotebookEdit) with ``path_spec='$TRUSTED_DIR/**'`` and a matching
    global-tier ``approved`` permission.  Each verb is independent — the
    idempotency check is per-verb so repeated calls remain safe.
    """
    from nephoscope.lib.db import _now, insert_permission, upsert_rule_shape
    from nephoscope.lib.mirror.tool_class import FILE_VERBS
    from nephoscope.lib.mirror.writer import sync_affected

    ts = _now()
    for verb in FILE_VERBS:
        shape_id = upsert_rule_shape(
            conn,
            verb=verb,
            subcommand=None,
            flags_json="[]",
            path_spec="$TRUSTED_DIR/**",
            ts=ts,
            context="any",
        )
        existing = conn.execute(
            "SELECT id FROM permissions"
            " WHERE rule_shape_id = ? AND session_id IS NULL AND project_id IS NULL;",
            (shape_id,),
        ).fetchone()
        if existing is None:
            perm_id = insert_permission(
                conn,
                shape_id,
                session_id=None,
                project_id=None,
                decision="approved",
                source="seed",
                ts=ts,
            )
            sync_affected(conn, perm_id)


def _append_trusted_dirs(new_roots: list[str]) -> None:
    """Merge *new_roots* into the config file's trusted_dirs and flush the cache."""
    from nephoscope.config import get_config

    config_path = _config_path()
    existing = _load_config_dict(config_path)
    existing["trusted_dirs"] = (
        _coerce_trusted_dirs(existing.get("trusted_dirs")) + new_roots
    )
    _write_config_file(config_path, existing)
    get_config.cache_clear()

    conn = _open()
    try:
        try:
            _seed_full_access_rules(conn)
        except Exception as exc:  # noqa: BLE001 — seed failure must not abort config write.
            print(
                f"nephoscope-init: warning — trusted-dir rule seed failed: {exc}",
                file=sys.stderr,
            )
    finally:
        conn.close()


def _configure_workspace_roots(args: argparse.Namespace) -> None:
    """Optionally prompt the user to configure trusted_dirs in the config file.

    Decision flow (in priority order):
    1. ``--no-workspace-prompts`` flag → return immediately, no write.
    2. trusted_dirs already non-empty in config → return, skip.
    3. auto_register_project_paths=True → silently add CWD, write, return.
    4. stdin is not a TTY → return, skip (non-interactive context).
    5. Interactive prompt → collect paths, write if any collected.
    """
    if args.no_workspace_prompts:
        return

    from nephoscope.config import get_config

    if get_config().trusted_dirs:
        return

    if get_config().auto_register_project_paths:
        _append_trusted_dirs([os.path.realpath(os.getcwd())])
        return

    if not sys.stdin.isatty():
        return

    collected = _prompt_for_paths()
    if collected:
        _append_trusted_dirs(collected)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _seed_auto_fixtures(conn: sqlite3.Connection) -> bool:
    """Seed fixtures, meta-profiles, and verb-type profiles into the DB.

    Optional fixtures (``_AUTO_LOAD_FIXTURES``) warn and continue on error.
    Bundled security baselines (``_AUTO_LOAD_META_PROFILES``) print a fatal
    message and return False so that ``main()`` can exit non-zero.
    Verb-type profiles (``_AUTO_LOAD_VERB_PROFILES``) warn and continue.

    Returns True when all mandatory baselines loaded successfully.
    """
    from nephoscope.learners.permission.profiles import apply_profile
    from nephoscope.learners.permission.seed import apply_fixtures, apply_verb_types

    for fixture_name in _AUTO_LOAD_FIXTURES:
        fixture_path = _FIXTURES_DIR / fixture_name
        try:
            apply_fixtures(conn, fixture_path)
        except Exception as exc:  # noqa: BLE001
            print(
                f"nephoscope-init: warning — fixture load failed ({fixture_name}): {exc}",
                file=sys.stderr,
            )

    for meta_name in _AUTO_LOAD_META_PROFILES:
        meta_path = _FIXTURES_DIR / "meta-profiles" / meta_name
        try:
            apply_profile(conn, meta_path)
        except Exception as exc:
            print(
                f"nephoscope-init: fatal — bundled security baseline failed ({meta_name}): {exc}",
                file=sys.stderr,
            )
            return False

    for profile_name in _AUTO_LOAD_VERB_PROFILES:
        profile_path = _FIXTURES_DIR / "profiles" / profile_name
        try:
            apply_verb_types(conn, profile_path)
        except Exception as exc:  # noqa: BLE001
            print(
                f"nephoscope-init: warning — verb types load failed ({profile_name}): {exc}",
                file=sys.stderr,
            )

    return True


def _prompt_and_seed_profiles() -> None:
    selected = _prompt_for_profiles()
    if selected:
        profile_conn = _open()
        try:
            _seed_optional_profiles(profile_conn, selected)
            profile_conn.commit()
        finally:
            profile_conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap the nephoscope observations database.",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Override the resolved DB path (bypasses OBSERVABILITY_DB and plugin data dir).",
    )
    parser.add_argument(
        "--no-workspace-prompts",
        action="store_true",
        default=False,
        help="Skip interactive prompts (trusted directories and optional permission profiles).",
    )
    args = parser.parse_args(argv)

    target = _resolve_target(args.db_path)

    # If the user passed --db-path, pin OBSERVABILITY_DB so the lib.db
    # helpers (which re-resolve on every call) see the same target.
    if args.db_path:
        os.environ["OBSERVABILITY_DB"] = str(target)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"nephoscope-init: cannot create {target.parent}: {exc}", file=sys.stderr)
        return 1

    already_existed = target.exists()

    try:
        conn = _open()
    except Exception as exc:  # noqa: BLE001 — surface init failures verbatim.
        print(
            f"nephoscope-init: failed to initialise DB at {target}: {exc}",
            file=sys.stderr,
        )
        return 1

    _seed_global_mirror_singleton(conn)

    if not already_existed:
        if not _seed_auto_fixtures(conn):
            conn.close()
            return 1
        conn.commit()

    conn.close()

    state = "already initialised" if already_existed else "initialised"
    print(f"nephoscope-init: {state} at {target}")

    _configure_workspace_roots(args)

    # Optional profile prompt — fresh DB only, TTY only, no-workspace-prompts suppresses.
    if not already_existed and not args.no_workspace_prompts and sys.stdin.isatty():
        _prompt_and_seed_profiles()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
