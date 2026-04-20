"""Runtime PreToolUse gate for Bash — **not registered in Phase 3**.

Reads a Claude Code PreToolUse payload from stdin and emits one of:

- ``{}`` — fall through to the normal prompt path (no opinion).
- ``{"hookSpecificOutput": {"hookEventName": "PreToolUse",
     "permissionDecision": "deny", "permissionDecisionReason": "..."}}``
  when the canonicalized command hits the deny-list.
- ``{"hookSpecificOutput": {"hookEventName": "PreToolUse",
     "permissionDecision": "allow", "permissionDecisionReason": "..."}}``
  when *every* canonical leaf is in ``permission_active``.

The hook is intentionally conservative: an unparseable command or a payload
missing fields defaults to ``{}`` (user prompt). The deny-list is checked
first so any learned pattern that later becomes dangerous still gets
blocked — defense in depth against promotion mistakes.

Called per Bash tool call, so the DB connection is short-lived and read-only.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Run as a standalone script; ensure the observability package is importable.
sys.path.insert(0, "/home/steve/.claude/observability")

from learners.permission.canonicalize import (  # noqa: E402
    CanonicalLeaf,
    parse_command,
)
from learners.permission.deny import is_denied  # noqa: E402


def _db_path() -> Path:
    """Resolve the observations DB path.

    Honors ``OBSERVABILITY_DB`` the same way ``lib.db.DB_PATH`` does; kept
    local so the hook doesn't pull in the heavier module on the hot path.
    """
    import os

    env = os.environ.get("OBSERVABILITY_DB")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "claude" / "observability" / "observations.db"


def _flags_key(flags: frozenset[str]) -> str:
    """Match the learner's PK representation (sorted, minified JSON array).

    Must byte-for-byte equal ``lib.db.minify_json(sorted(list(leaf.flags)))`` —
    the learner stores flags via that helper and we compare as TEXT. A default
    ``json.dumps`` (with spaces after separators) would never match. Kept local
    so this hot-path module doesn't import ``lib.db``.
    """
    return json.dumps(sorted(flags), ensure_ascii=False, separators=(",", ":"))


def _all_leaves_active(conn: sqlite3.Connection, leaves: list[CanonicalLeaf]) -> bool:
    """True iff every leaf has a row in ``permission_active``.

    Post-v5, ``permission_active`` is keyed on ``command_shape_id`` only — the
    verb/subcommand/flags live on ``command_shapes``. We JOIN through so the
    runtime gate can still look up by shape fields without needing a shape
    upsert path (which would also mutate the DB from the hot path).

    ``IFNULL(subcommand,'')`` mirrors the partial UNIQUE index on
    ``command_shapes`` and makes the NULL comparison work with a simple ``=``.
    """
    for leaf in leaves:
        sub_key = leaf.subcommand or ""
        row = conn.execute(
            """
            SELECT 1
              FROM permission_active a
              JOIN command_shapes cs ON cs.id = a.command_shape_id
             WHERE cs.verb = ?
               AND IFNULL(cs.subcommand, '') = ?
               AND cs.flags = ?;
            """,
            (leaf.verb, sub_key, _flags_key(leaf.flags)),
        ).fetchone()
        if row is None:
            return False
    return True


def _summarize(leaves: list[CanonicalLeaf]) -> str:
    pieces: list[str] = []
    for leaf in leaves:
        sub = f" {leaf.subcommand}" if leaf.subcommand else ""
        flag_part = ""
        if leaf.flags:
            flag_part = " " + " ".join(sorted(leaf.flags))
        pieces.append(f"{leaf.verb}{sub}{flag_part}".strip())
    return "; ".join(pieces)


def _now_iso() -> str:
    """Timestamp matching ``lib.db._now()`` — inlined to keep the hot path
    free of a ``lib.db`` import.
    """
    import datetime as _dt

    return (
        _dt.datetime.now(tz=_dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _mark_denied(tool_use_id: str) -> None:
    """Flip the recorder's pending row to ``denied`` + stamp ``completed_ts``.

    Runs on every deny decision. Any failure (missing DB, locked DB,
    unknown tool_use_id) is swallowed to stderr — denying the call is
    never blocked by a bookkeeping failure.
    """
    db = _db_path()
    if not db.is_file():
        return
    try:
        conn = sqlite3.connect(db, timeout=1.0)
        try:
            conn.execute(
                """
                UPDATE tool_calls
                   SET status_id = (SELECT id FROM call_statuses
                                     WHERE name = 'denied'),
                       completed_ts = ?
                 WHERE tool_use_id = ?
                   AND status_id = (SELECT id FROM call_statuses
                                     WHERE name = 'pending');
                """,
                (_now_iso(), tool_use_id),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"hook: denied-row update failed: {e}", file=sys.stderr)


def _emit(decision: str | None, reason: str | None = None) -> None:
    if decision is None:
        print("{}")
        return
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason or "",
        }
    }
    print(json.dumps(payload, ensure_ascii=False))


def _load_payload() -> dict[str, Any] | None:
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return None
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def main() -> int:
    data = _load_payload()
    if data is None:
        _emit(None)
        return 0

    tool = data.get("tool_name") or data.get("tool")
    if tool != "Bash":
        _emit(None)
        return 0

    tool_input = data.get("tool_input") or data.get("input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        _emit(None)
        return 0

    leaves = parse_command(command)
    if not leaves:
        _emit(None)
        return 0

    # Deny-first: if any leaf fires a deny rule, block the whole command.
    for leaf in leaves:
        denied, reason = is_denied(leaf)
        if denied:
            tool_use_id = data.get("tool_use_id")
            if isinstance(tool_use_id, str) and tool_use_id:
                _mark_denied(tool_use_id)
            _emit("deny", reason or "matched deny list")
            return 0

    # Active-allowlist: every leaf must be in the active table.
    db = _db_path()
    if not db.is_file():
        _emit(None)
        return 0

    conn = sqlite3.connect(db, timeout=1.0)
    try:
        if _all_leaves_active(conn, leaves):
            _emit(
                "allow",
                f"matched learned pattern(s): {_summarize(leaves)}",
            )
            return 0
    finally:
        conn.close()

    _emit(None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
