"""Runtime PreToolUse gate — dispatch model.

Reads a Claude Code PreToolUse payload from stdin and emits one of:

- ``{}``                       — fall through (no opinion / ``NoOpinion``).
- ``permissionDecision=deny``  — hard block.
- ``permissionDecision=ask``   — user-confirmable; registers a pending row.
- ``permissionDecision=allow`` — every leaf is approved.

Priority order
--------------
1. **Procedural deny** — deny.py / deny.yaml ``deny`` tier fires immediately
   (Bash only; before any DB access).
2. **Dispatch** — ``match.dispatch`` routes to the per-tool-class matcher.
   Tier priority (session → project → global) is enforced inside dispatch.
3. **Ask-tier bookkeeping** — when dispatch returns ``Verdict.Ask`` for a
   Bash call, register a ``permission_ask_pending`` row.
4. **Procedural ask** — for Bash calls with no DB opinion, deny.py ``ask``
   tier fires (no-DB fast path).

Verdict → response mapping
--------------------------
``Verdict.Allow``     → ``{permissionDecision: "allow"}``
``Verdict.Deny``      → ``{permissionDecision: "deny"}``
``Verdict.Ask``       → ``{permissionDecision: "ask"}``
``Verdict.NoOpinion`` → ``{}`` (fall through to Claude Code's native gate)
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from nephoscope.learners.permission.canonicalize import (  # noqa: E402
    CanonicalLeaf,
    parse_command,
)
from nephoscope.learners.permission.deny import evaluate  # noqa: E402
from nephoscope.learners.permission.match import Verdict, dispatch  # noqa: E402
from nephoscope.lib.paths import is_disabled, observations_db_path  # noqa: E402


# ---------------------------------------------------------------------------
# DB / timestamp helpers
# ---------------------------------------------------------------------------


def _db_path() -> Path:
    """Resolve the observations DB path from env + plugin-data defaults."""
    return observations_db_path()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _now_iso() -> str:
    import datetime as _dt

    return (
        _dt.datetime.now(tz=_dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# DB lookups
# ---------------------------------------------------------------------------


def _lookup_call_context(
    conn: sqlite3.Connection, tool_use_id: str
) -> tuple[int | None, int | None]:
    row = conn.execute(
        "SELECT session_id, project_id FROM tool_calls WHERE tool_use_id = ?;",
        (tool_use_id,),
    ).fetchone()
    if row is None:
        return None, None
    return (
        int(row[0]) if row[0] is not None else None,
        int(row[1]) if row[1] is not None else None,
    )


# ---------------------------------------------------------------------------
# Output / side-effect helpers
# ---------------------------------------------------------------------------


_MARK_DENIED_SQL = """
    UPDATE tool_calls
       SET status_id = (SELECT id FROM call_statuses WHERE name = 'denied'),
           completed_ts = ?
     WHERE tool_use_id = ?
       AND status_id = (SELECT id FROM call_statuses WHERE name = 'pending');
"""


def _mark_denied(
    tool_use_id: str,
    conn: sqlite3.Connection | None = None,
) -> None:
    if conn is not None:
        try:
            conn.execute(_MARK_DENIED_SQL, (_now_iso(), tool_use_id))
        except sqlite3.Error:
            pass
        return
    db = _db_path()
    if not db.is_file():
        return
    try:
        c = _connect(db)
        try:
            c.execute(_MARK_DENIED_SQL, (_now_iso(), tool_use_id))
        finally:
            c.close()
    except sqlite3.Error:
        pass


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


def _summarize(leaves: list[CanonicalLeaf]) -> str:
    pieces: list[str] = []
    for leaf in leaves:
        sub = f" {leaf.subcommand}" if leaf.subcommand else ""
        flag_part = ""
        if leaf.flags:
            flag_part = " " + " ".join(sorted(leaf.flags))
        pieces.append(f"{leaf.verb}{sub}{flag_part}".strip())
    return "; ".join(pieces)


def _register_ask_pending(
    conn: sqlite3.Connection,
    tool_use_id_str: str,
    session_id: int,
    leaves: list[CanonicalLeaf],
) -> None:
    """Insert a permission_ask_pending row for the first ask-tier leaf."""
    first_ask_leaf = _first_ask_leaf(leaves)
    if first_ask_leaf is None:
        return
    conn.execute(
        "INSERT OR IGNORE INTO permission_ask_pending"
        " (tool_use_id, session_id, verb, subcommand, flags, asked_at)"
        " VALUES (?, ?, ?, ?, ?, ?);",
        (
            tool_use_id_str,
            session_id,
            first_ask_leaf.verb,
            first_ask_leaf.subcommand,
            json.dumps(sorted(first_ask_leaf.flags), separators=(",", ":")),
            _now_iso(),
        ),
    )


def _ask_reason_from_leaves(leaves: list[CanonicalLeaf]) -> str | None:
    """Return the first ask-tier reason from deny.py for a pre-parsed leaf list."""
    for leaf in leaves:
        outcome, reason = evaluate(leaf)
        if outcome == "ask":
            return reason
    return None


def _emit_allow(tool_input: dict[str, Any]) -> None:
    """Emit allow with an optional matched-shape reason for Bash commands."""
    reason = ""
    if isinstance(tool_input, dict):
        cmd = tool_input.get("command", "")
        if cmd:
            leaves = parse_command(cmd)
            reason = f"matched: {_summarize(leaves)}" if leaves else ""
    _emit("allow", reason)


def _emit_deny(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_use_id_str: str | None,
    conn: sqlite3.Connection | None,
) -> None:
    """Mark the call as denied (if trackable) and emit a deny response."""
    if tool_use_id_str:
        _mark_denied(tool_use_id_str, conn=conn)
    if tool_name == "Bash":
        cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        leaves = parse_command(cmd) if cmd else []
        verb = leaves[0].verb if leaves else tool_name
        reason = f"shape '{verb}' was user-rejected"
    else:
        reason = f"{tool_name} denied"
    _emit("deny", reason)


def _emit_ask_bash(
    tool_input: dict[str, Any],
    tool_use_id_str: str | None,
    conn: sqlite3.Connection | None,
    session_id: int | None,
) -> None:
    """Register ask-pending bookkeeping and emit an ask response for Bash."""
    cmd = tool_input.get("command", "")
    leaves = parse_command(cmd) if cmd else []
    if (
        leaves
        and session_id is not None
        and tool_use_id_str is not None
        and conn is not None
    ):
        _register_ask_pending(conn, tool_use_id_str, session_id, leaves)
    _emit("ask", _ask_reason_from_leaves(leaves))


def _emit_verdict(
    verdict: Verdict,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_use_id_str: str | None,
    conn: sqlite3.Connection | None,
    session_id: int | None,
) -> None:
    """Translate a Verdict to hook output, with side-effects for Deny/Ask."""
    if verdict == Verdict.Allow:
        _emit_allow(tool_input)
    elif verdict == Verdict.Deny:
        _emit_deny(tool_name, tool_input, tool_use_id_str, conn)
    elif verdict == Verdict.Ask:
        if tool_name == "Bash" and isinstance(tool_input, dict):
            _emit_ask_bash(tool_input, tool_use_id_str, conn, session_id)
        else:
            _emit("ask")
    else:  # NoOpinion
        _emit(None)


def _first_ask_leaf(leaves: list[CanonicalLeaf]) -> CanonicalLeaf | None:
    """Return the first leaf that triggers the ask tier in deny.py."""
    for leaf in leaves:
        outcome, _ = evaluate(leaf)
        if outcome == "ask":
            return leaf
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _procedural_deny_bash(
    tool_input: dict[str, Any], tool_use_id_str: str | None
) -> bool:
    """Check deny-tier rules for Bash before DB access. Returns True if denied (and emitted)."""
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        _emit(None)
        return True

    leaves = parse_command(command)
    if not leaves:
        _emit(None)
        return True

    for leaf in leaves:
        outcome, reason = evaluate(leaf)
        if outcome == "deny":
            if tool_use_id_str:
                _mark_denied(tool_use_id_str)
            _emit("deny", reason or "matched deny list")
            return True
    return False


def _no_db_bash_ask(tool_input: dict[str, Any]) -> bool:
    """Emit ask if any leaf triggers the ask tier when there is no DB. Returns True if emitted."""
    command = tool_input.get("command", "")
    if not command:
        return False
    for leaf in parse_command(command):
        outcome, reason = evaluate(leaf)
        if outcome == "ask":
            _emit("ask", reason)
            return True
    return False


def _with_db_verdict(
    conn: sqlite3.Connection,
    tool: str,
    tool_input: dict[str, Any],
    tool_use_id_str: str | None,
    payload_cwd: str,
) -> None:
    """Dispatch to the per-tool-class matcher and emit the verdict."""
    session_id_int: int | None = None
    project_id_int: int | None = None
    if tool_use_id_str:
        session_id_int, project_id_int = _lookup_call_context(conn, tool_use_id_str)

    verdict = dispatch(
        tool,
        tool_input,
        conn,
        session_id_int,
        project_id_int,
        cwd=payload_cwd or None,
    )
    _emit_verdict(verdict, tool, tool_input, tool_use_id_str, conn, session_id_int)


def _parse_tool_fields(
    data: dict[str, Any],
) -> tuple[str | None, dict[str, Any], str | None, str]:
    """Extract (tool, tool_input, tool_use_id_str, payload_cwd) from a payload dict.

    Returns tool=None when the payload has no valid tool name.
    """
    tool = data.get("tool_name") or data.get("tool")
    if not isinstance(tool, str) or not tool:
        return None, {}, None, ""

    tool_input = data.get("tool_input") or data.get("input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    tool_use_id = data.get("tool_use_id")
    tool_use_id_str = (
        tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None
    )

    payload_cwd: str = data.get("cwd") or ""
    return tool, tool_input, tool_use_id_str, payload_cwd


def main() -> int:  # NOSONAR S3516 - hook entry points must always exit 0 (domain rule)
    # Opt-out marker short-circuits the entire gate; fall-through keeps
    # Claude Code's native prompt behaviour intact while the plugin is
    # muted.
    if is_disabled():
        _emit(None)
        return 0

    data = _load_payload()
    if data is None:
        _emit(None)
        return 0

    tool, tool_input, tool_use_id_str, payload_cwd = _parse_tool_fields(data)
    if tool is None:
        _emit(None)
        return 0

    # Step 1: procedural deny — Bash only; fires before any DB access.
    if tool == "Bash" and _procedural_deny_bash(tool_input, tool_use_id_str):
        return 0

    # No-DB fast path (Bash only): ask tier from deny.py.
    db = _db_path()
    if not db.is_file():
        if tool == "Bash" and _no_db_bash_ask(tool_input):
            return 0
        _emit(None)
        return 0

    # With DB: dispatch to per-tool-class matcher.
    conn = _connect(db)
    try:
        _with_db_verdict(conn, tool, tool_input, tool_use_id_str, payload_cwd)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
