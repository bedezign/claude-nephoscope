"""Safe-shape fixture: apply to permission_active, or export from it.

The fixture at ``config/fixtures/safe_shapes.yaml`` is the one piece of
the observability DB that can't be rebuilt from observations — it captures
human trust judgments about which command shapes are safe to auto-approve.
The broader permission_active corpus (learner-promoted entries) is also
non-rebuildable once candidates have been consumed, so the fixture covers
the full table, not just manual seeds.

CLI:

    python -m learners.permission.seed            # apply fixture → DB
    python -m learners.permission.seed --apply    # same, explicit
    python -m learners.permission.seed --export   # DB → fixture (overwrites)

Apply is idempotent: INSERT OR IGNORE on permission_active, upsert on
command_shapes. Re-running adds nothing once the table matches.
Export overwrites the fixture file deterministically — entries are sorted
by (verb, subcommand, flags) so diffs are stable across runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

# Allow ``from lib.db import ...`` regardless of invocation cwd.
sys.path.insert(0, "/home/steve/.claude/observability")

from lib.db import _now, _open, minify_json, upsert_command_shape  # noqa: E402

FIXTURE_PATH = Path(__file__).parent / "config" / "fixtures" / "safe_shapes.yaml"

_HEADER = """\
# Permission-learner fixture.
#
# Captures every durable user judgment about command shapes. Two lists:
#
#   active:   shapes to auto-approve (-> permission_active)
#   rejected: shapes to never re-propose (-> permission_rejected)
#
# Managed round-trip: apply with `python -m learners.permission.seed`,
# regenerate with `python -m learners.permission.seed --export`. Export
# overwrites this file deterministically (entries sorted by verb then
# flags). Hand-edits survive only if you never run --export again.
#
# The permission hook's allow branch matches on (verb, subcommand, flags);
# CONTENT_VERBS in canonicalize.py drops the first positional from the
# shape, so `ls /foo` and `ls /bar` share the `verb: ls` shape here.
#
# Destructive flag combos (sed -i, find -delete, awk -i inplace, etc.)
# are deliberately absent from `active`. Those still go through the normal
# prompt (or can be added to `rejected` to suppress them from review).
#
# Entry schema (both lists):
#   - verb: <str>            required
#     flags: [<str>, ...]    optional; order-independent, sorted on import
#     subcommand: <str>      optional; almost never set for CONTENT_VERBS
#     source: manual|learner active only; defaults to 'manual'
#     reason: <str>          rejected only; free-text note
"""


# ---------------------------------------------------------------------------
# Apply (YAML → DB)
# ---------------------------------------------------------------------------


def _load_fixture(
    path: Path = FIXTURE_PATH,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (active_shapes, rejected_shapes) from the YAML fixture.

    Accepts the legacy single-list format (top-level ``shapes:``) and treats
    it as ``active`` with an empty rejected list.
    """
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    if "active" in data or "rejected" in data:
        active = data.get("active") or []
        rejected = data.get("rejected") or []
    else:
        # Legacy: top-level `shapes:` == active only.
        active = data.get("shapes") or []
        rejected = []
    if not isinstance(active, list):
        raise ValueError(f"{path}: 'active' must be a list")
    if not isinstance(rejected, list):
        raise ValueError(f"{path}: 'rejected' must be a list")
    return active, rejected


def _flags_key(flags: list[str] | None) -> str:
    return minify_json(sorted(flags or []))


def _validate_verb(entry: dict[str, Any]) -> str:
    verb = entry.get("verb")
    if not isinstance(verb, str) or not verb:
        raise ValueError(f"shape entry missing 'verb': {entry!r}")
    return verb


def apply_fixture(
    conn,
    active: list[dict[str, Any]],
    rejected: list[dict[str, Any]] | None = None,
) -> dict[str, tuple[int, int]]:
    """Apply ``active`` + ``rejected`` shapes. Returns counts per table.

    Return shape: ``{"active": (inserted, existed), "rejected": (inserted, existed)}``.
    Caller using the legacy two-tuple shape can still ignore the rejected key.
    """
    now = _now()

    def _resolve_scope(entry: dict) -> int:
        name = entry.get("scope") or "any"
        row = conn.execute(
            "SELECT id FROM tool_call_scopes WHERE name = ?;", (name,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown scope {name!r} in {entry!r}")
        return int(row[0])

    a_inserted = a_existed = 0
    for entry in active:
        verb = _validate_verb(entry)
        subcommand = entry.get("subcommand")
        flags_json = _flags_key(entry.get("flags"))
        source = entry.get("source") or "manual"
        if source not in ("manual", "learner"):
            raise ValueError(f"invalid source {source!r} in {entry!r}")
        shape_id = upsert_command_shape(conn, verb, subcommand, flags_json, now)
        scope_id = _resolve_scope(entry)
        cur = conn.execute(
            "INSERT OR IGNORE INTO permission_active "
            "(command_shape_id, scope_id, promoted_at, source) "
            "VALUES (?, ?, ?, ?);",
            (shape_id, scope_id, now, source),
        )
        if cur.rowcount:
            a_inserted += 1
        else:
            a_existed += 1

    r_inserted = r_existed = 0
    for entry in rejected or []:
        verb = _validate_verb(entry)
        subcommand = entry.get("subcommand")
        flags_json = _flags_key(entry.get("flags"))
        reason = entry.get("reason")
        shape_id = upsert_command_shape(conn, verb, subcommand, flags_json, now)
        scope_id = _resolve_scope(entry)
        cur = conn.execute(
            "INSERT OR IGNORE INTO permission_rejected "
            "(command_shape_id, scope_id, rejected_at, reason) "
            "VALUES (?, ?, ?, ?);",
            (shape_id, scope_id, now, reason),
        )
        if cur.rowcount:
            r_inserted += 1
        else:
            r_existed += 1

    conn.commit()
    return {
        "active": (a_inserted, a_existed),
        "rejected": (r_inserted, r_existed),
    }


# ---------------------------------------------------------------------------
# Export (DB → YAML)
# ---------------------------------------------------------------------------


def _parse_flags(flags_text: str | None) -> list[str]:
    try:
        flags = json.loads(flags_text) if flags_text else []
    except (json.JSONDecodeError, TypeError):
        flags = []
    return list(flags) if isinstance(flags, list) else []


def export_shapes(conn) -> list[dict[str, Any]]:
    """Read permission_active JOINed with command_shapes + scope name."""
    rows = conn.execute(
        """
        SELECT cs.verb, cs.subcommand, cs.flags, sc.name, pa.source
          FROM permission_active pa
          JOIN command_shapes cs     ON cs.id = pa.command_shape_id
          JOIN tool_call_scopes sc   ON sc.id = pa.scope_id
         ORDER BY cs.verb, IFNULL(cs.subcommand, ''),
                  LENGTH(cs.flags), cs.flags, sc.name;
        """
    ).fetchall()

    shapes: list[dict[str, Any]] = []
    for verb, subcommand, flags_text, scope, source in rows:
        entry: dict[str, Any] = {"verb": verb}
        if subcommand is not None:
            entry["subcommand"] = subcommand
        flags = _parse_flags(flags_text)
        if flags:
            entry["flags"] = flags
        # Only write scope when non-default — keeps legacy fixtures clean.
        if scope and scope != "any":
            entry["scope"] = scope
        # Only write source when it's non-default — keeps manual entries clean.
        if source and source != "manual":
            entry["source"] = source
        shapes.append(entry)
    return shapes


def export_rejected(conn) -> list[dict[str, Any]]:
    """Read permission_rejected JOINed with command_shapes + scope name."""
    rows = conn.execute(
        """
        SELECT cs.verb, cs.subcommand, cs.flags, sc.name, r.reason
          FROM permission_rejected r
          JOIN command_shapes cs     ON cs.id = r.command_shape_id
          JOIN tool_call_scopes sc   ON sc.id = r.scope_id
         ORDER BY cs.verb, IFNULL(cs.subcommand, ''),
                  LENGTH(cs.flags), cs.flags, sc.name;
        """
    ).fetchall()

    out: list[dict[str, Any]] = []
    for verb, subcommand, flags_text, scope, reason in rows:
        entry: dict[str, Any] = {"verb": verb}
        if subcommand is not None:
            entry["subcommand"] = subcommand
        flags = _parse_flags(flags_text)
        if flags:
            entry["flags"] = flags
        if scope and scope != "any":
            entry["scope"] = scope
        if reason:
            entry["reason"] = reason
        out.append(entry)
    return out


def write_fixture(
    active: list[dict[str, Any]],
    rejected: list[dict[str, Any]] | None = None,
    path: Path = FIXTURE_PATH,
) -> None:
    """Write active + rejected shapes to the fixture with the canonical header."""
    payload: dict[str, Any] = {"active": active}
    if rejected:
        payload["rejected"] = rejected
    body = yaml.safe_dump(
        payload,
        sort_keys=False,
        default_flow_style=None,
        width=120,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_HEADER + "\n" + body)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _do_apply() -> int:
    active, rejected = _load_fixture()
    conn = _open()
    try:
        counts = apply_fixture(conn, active, rejected)
        total_active = conn.execute(
            "SELECT COUNT(*) FROM permission_active;"
        ).fetchone()[0]
        total_rejected = conn.execute(
            "SELECT COUNT(*) FROM permission_rejected;"
        ).fetchone()[0]
    finally:
        conn.close()
    a_ins, a_exi = counts["active"]
    r_ins, r_exi = counts["rejected"]
    print(f"fixture: {len(active)} active, {len(rejected)} rejected")
    print(
        f"active   → inserted {a_ins}, already present {a_exi} (total {total_active})"
    )
    print(
        f"rejected → inserted {r_ins}, already present {r_exi} (total {total_rejected})"
    )
    return 0


def _do_export() -> int:
    conn = _open()
    try:
        active = export_shapes(conn)
        rejected = export_rejected(conn)
    finally:
        conn.close()
    write_fixture(active, rejected)
    print(f"exported {len(active)} active + {len(rejected)} rejected → {FIXTURE_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply or export the safe-shape fixture."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--apply",
        action="store_true",
        help="Apply fixture to permission_active (default).",
    )
    group.add_argument(
        "--export",
        action="store_true",
        help="Export permission_active to the fixture file (overwrites).",
    )
    args = parser.parse_args(argv)
    if args.export:
        return _do_export()
    return _do_apply()


if __name__ == "__main__":
    raise SystemExit(main())
