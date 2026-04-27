"""Explicit DB bootstrap CLI.

Usage:
    nephoscope-init [--db-path PATH]

Materialises the observations SQLite file at the resolved path and applies
``lib/schema.sql``. Useful for pre-seeding a non-default
``$OBSERVABILITY_DB`` before first session, or for verifying install during
debugging. Idempotent — re-running against an existing DB is a no-op (the
schema loader only fires on an empty database).

Resolution order:
    ``--db-path`` arg > ``$OBSERVABILITY_DB`` > ``${CLAUDE_PLUGIN_DATA}/observations.db``
    > ``~/.cache/nephoscope/observations.db``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from nephoscope.lib.db import _open
from nephoscope.lib.paths import observations_db_path

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
    "credential_leaks.yaml",
    "secret_manager_standalones.yaml",
]


def _resolve_target(cli_path: str | None) -> Path:
    """Return the DB path honouring the CLI override first, then env/defaults."""
    if cli_path:
        return Path(cli_path)
    return observations_db_path()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap the nephoscope observations database.",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Override the resolved DB path (bypasses OBSERVABILITY_DB and plugin data dir).",
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

    if not already_existed:
        from nephoscope.learners.permission.seed import apply_fixtures

        for fixture_name in _AUTO_LOAD_FIXTURES:
            fixture_path = _FIXTURES_DIR / fixture_name
            try:
                apply_fixtures(conn, fixture_path)
            except Exception as exc:  # noqa: BLE001 — fixture load failure must not abort init.
                print(
                    f"nephoscope-init: warning — fixture load failed ({fixture_name}): {exc}",
                    file=sys.stderr,
                )

    conn.close()

    state = "already initialised" if already_existed else "initialised"
    print(f"nephoscope-init: {state} at {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
