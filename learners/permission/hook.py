"""Runtime PreToolUse gate for Bash.

Reads a Claude Code PreToolUse payload from stdin and emits one of:

- ``{}`` — fall through to the normal prompt path (no opinion).
- ``permissionDecision=deny`` — hard-blocks via deny.py (deny.yaml rules
  or a procedural guard like ``sudo`` / guarded-path redirection).
- ``permissionDecision=ask`` — user-confirmable. On first ask of an
  ask-tier shape in a session, a row is written into
  ``permission_ask_pending``; when the recorder's Post phase sees
  ``status=ok`` for that ``tool_use_id``, it promotes the shape into
  ``permission_session_approvals`` and future matching calls in the same
  session auto-allow.
- ``permissionDecision=allow`` — every leaf is either session-approved
  (v12) or in ``permission_active``.

The hook is intentionally conservative: unparseable input / missing
payload fields default to ``{}``. Deny is checked before ask; ask is
checked against session approvals before emitting. Active-allowlist only
evaluates when no leaf asked and no leaf was denied.

Called per Bash tool call, so the DB connection is short-lived and
mostly read-only. When an ask is about to be emitted, the hook writes
pending rows to the DB — the hot path absorbs one INSERT per leaf in
that case only.
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
from learners.permission.deny import evaluate  # noqa: E402


def _db_path() -> Path:
    """Resolve the observations DB path. Honors ``OBSERVABILITY_DB``."""
    import os

    env = os.environ.get("OBSERVABILITY_DB")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "claude" / "observability" / "observations.db"


def _flags_key(flags: frozenset[str]) -> str:
    """Match the learner's stored flags format (sorted, minified JSON)."""
    return json.dumps(sorted(flags), ensure_ascii=False, separators=(",", ":"))


def _lookup_call_context(
    conn: sqlite3.Connection, tool_use_id: str
) -> tuple[int | None, int | None]:
    """Read the recorder's just-inserted row to get session + scope.

    The observability PreToolUse hook order (see settings.json) runs the
    recorder BEFORE the permission hook, so by the time we fire, the
    tool_call row exists with session_id and scope_id populated. If the
    row is missing (schema drift, recorder failure, synthetic tool_use_id
    mismatch), both return as None and we degrade to Wave 1 behavior.
    """
    row = conn.execute(
        "SELECT session_id, scope_id FROM tool_calls WHERE tool_use_id = ?;",
        (tool_use_id,),
    ).fetchone()
    if row is None:
        return None, None
    return (
        int(row[0]) if row[0] is not None else None,
        int(row[1]) if row[1] is not None else None,
    )


def _shape_id_for_leaf(conn: sqlite3.Connection, leaf: CanonicalLeaf) -> int | None:
    """Look up an existing command_shapes row for a leaf. Returns None if
    absent — the hook doesn't upsert shapes on lookup, only on ask
    registration (to keep the hot path cheap when no ask is involved).
    """
    row = conn.execute(
        """
        SELECT id FROM command_shapes
         WHERE verb = ?
           AND IFNULL(subcommand, '') = ?
           AND flags = ?;
        """,
        (leaf.verb, leaf.subcommand or "", _flags_key(leaf.flags)),
    ).fetchone()
    return int(row[0]) if row is not None else None


def _upsert_shape_inline(
    conn: sqlite3.Connection, leaf: CanonicalLeaf, now: str
) -> int:
    """Upsert command_shapes for an ask'd leaf and return its id.

    Inlined (no ``lib.db`` import) because this is the hot path and the
    helper's dependency surface is small.
    """
    flags_key = _flags_key(leaf.flags)
    row = conn.execute(
        """
        SELECT id FROM command_shapes
         WHERE verb = ?
           AND IFNULL(subcommand, '') = ?
           AND flags = ?;
        """,
        (leaf.verb, leaf.subcommand or "", flags_key),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE command_shapes SET last_seen = ? WHERE id = ?;",
            (now, int(row[0])),
        )
        return int(row[0])
    cur = conn.execute(
        """
        INSERT INTO command_shapes (verb, subcommand, flags, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?);
        """,
        (leaf.verb, leaf.subcommand, flags_key, now, now),
    )
    return int(cur.lastrowid or 0)


def _is_session_approved(
    conn: sqlite3.Connection, session_id: int, shape_id: int, scope_id: int
) -> bool:
    """Check if a (session, shape, scope) triple is pre-approved."""
    row = conn.execute(
        """
        SELECT 1 FROM permission_session_approvals
         WHERE session_id = ?
           AND command_shape_id = ?
           AND scope_id = ?;
        """,
        (session_id, shape_id, scope_id),
    ).fetchone()
    return row is not None


def _rejected_reason_for_any_leaf(
    conn: sqlite3.Connection, leaves: list[CanonicalLeaf], scope_id: int | None
) -> str | None:
    """Return a deny reason if any leaf's shape has a rejection match.

    Returns ``None`` when no leaf is rejected. Scope match: ``any`` or
    equal to the call's scope. First matching leaf's rejection wins;
    subsequent leaves aren't checked (the deny is terminal).
    """
    any_id_row = conn.execute(
        "SELECT id FROM tool_call_scopes WHERE name = 'any';"
    ).fetchone()
    any_id = int(any_id_row[0]) if any_id_row is not None else -1
    scope_ids: list[int] = [any_id]
    if scope_id is not None and scope_id != any_id:
        scope_ids.append(scope_id)
    placeholders = ",".join("?" for _ in scope_ids)
    for leaf in leaves:
        shape_id = _shape_id_for_leaf(conn, leaf)
        if shape_id is None:
            continue
        row = conn.execute(
            f"""
            SELECT reason FROM permission_rejected
             WHERE command_shape_id = ?
               AND scope_id IN ({placeholders})
             LIMIT 1;
            """,
            (shape_id, *scope_ids),
        ).fetchone()
        if row is not None:
            reason = row[0] if row[0] else ""
            desc = f"{leaf.verb}"
            if leaf.subcommand:
                desc += f" {leaf.subcommand}"
            base = f"shape '{desc}' was user-rejected"
            return f"{base}: {reason}" if reason else base
    return None


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
    """Timestamp matching ``lib.db._now()`` — inlined for hot-path leanness."""
    import datetime as _dt

    return (
        _dt.datetime.now(tz=_dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _mark_denied(tool_use_id: str) -> None:
    """Flip the recorder's pending row to ``denied`` + stamp ``completed_ts``."""
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

    tool_use_id = data.get("tool_use_id")
    tool_use_id_str = (
        tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None
    )

    db = _db_path()
    if not db.is_file():
        # No DB means no session / no approvals. Run deny-check without
        # session bookkeeping; if any leaf asks, emit ask with no pending
        # registration — the call still works, just without session memory.
        ask_reason: str | None = None
        for leaf in leaves:
            decision, reason = evaluate(leaf)
            if decision == "deny":
                if tool_use_id_str:
                    _mark_denied(tool_use_id_str)
                _emit("deny", reason or "matched deny list")
                return 0
            if decision == "ask" and ask_reason is None:
                ask_reason = reason
        if ask_reason is not None:
            _emit("ask", ask_reason)
            return 0
        _emit(None)
        return 0

    conn = sqlite3.connect(db, timeout=1.0)
    try:
        # Read the recorder's row (ran just before us) for session + scope.
        session_id_int: int | None = None
        scope_id: int | None = None
        if tool_use_id_str:
            session_id_int, scope_id = _lookup_call_context(conn, tool_use_id_str)

        # First pass: hard-deny. A single deny anywhere blocks the whole call.
        for leaf in leaves:
            decision, reason = evaluate(leaf)
            if decision == "deny":
                if tool_use_id_str:
                    _mark_denied(tool_use_id_str)
                _emit("deny", reason or "matched deny list")
                return 0

        # v13: user-rejected shapes (scope-aware) are runtime deny. Checked
        # AFTER rule-deny (rule deny has a richer reason string) but BEFORE
        # ask/active so a rejection can't be overridden by session approval
        # or allowlist.
        rejected_reason = _rejected_reason_for_any_leaf(conn, leaves, scope_id)
        if rejected_reason is not None:
            if tool_use_id_str:
                _mark_denied(tool_use_id_str)
            _emit("deny", rejected_reason)
            return 0

        # Unified clearance pass: for each leaf, check in order —
        #   (a) permission_active for (shape, scope) → cleared.
        #   (b) permission_session_approvals for (session, shape, scope)
        #       → cleared (ask-tier shortcut).
        #   (c) if still uncleared and evaluate() says ask, track ask_reason
        #       and remember the leaf for pending-registration.
        # If all leaves are cleared → emit allow. If any ask_reason surfaced
        # → emit ask and register pending rows for those leaves. Otherwise
        # fall through (no opinion).
        now = _now_iso()
        ask_reason = None
        # (leaf_index, shape_id, leaf) for each uncleared ask-tier leaf.
        pending_leaves: list[tuple[int, int, CanonicalLeaf]] = []
        all_cleared = True

        # Scope lookup — hoisted out of the leaf loop (same pattern as
        # ``_rejected_reason_for_any_leaf``).
        any_id_row = conn.execute(
            "SELECT id FROM tool_call_scopes WHERE name = 'any';"
        ).fetchone()
        any_id = int(any_id_row[0]) if any_id_row is not None else -1
        scope_ids: list[int] = [any_id]
        if scope_id is not None and scope_id != any_id:
            scope_ids.append(scope_id)
        placeholders = ",".join("?" for _ in scope_ids)

        for leaf_index, leaf in enumerate(leaves):
            existing_shape = _shape_id_for_leaf(conn, leaf)
            # Active check: fast path if shape already registered.
            if existing_shape is not None:
                row = conn.execute(
                    f"""
                    SELECT 1 FROM permission_active
                     WHERE command_shape_id = ?
                       AND scope_id IN ({placeholders});
                    """,
                    (existing_shape, *scope_ids),
                ).fetchone()
                if row is not None:
                    continue
                # Session-approved?
                if (
                    session_id_int is not None
                    and scope_id is not None
                    and _is_session_approved(
                        conn, session_id_int, existing_shape, scope_id
                    )
                ):
                    continue

            # Not cleared by active/session. Does rule say ask?
            decision, reason = evaluate(leaf)
            if decision == "ask":
                shape_id = existing_shape
                if shape_id is None:
                    shape_id = _upsert_shape_inline(conn, leaf, now)
                # Re-check session approval if we just upserted (rare).
                if (
                    session_id_int is not None
                    and scope_id is not None
                    and _is_session_approved(conn, session_id_int, shape_id, scope_id)
                ):
                    continue
                if ask_reason is None:
                    ask_reason = reason
                pending_leaves.append((leaf_index, shape_id, leaf))
                all_cleared = False
                continue

            # Not in active, not session-approved, not ask-tier → no opinion.
            all_cleared = False

        if all_cleared:
            conn.commit()
            _emit(
                "allow",
                f"matched: {_summarize(leaves)}",
            )
            return 0

        if ask_reason is not None:
            # Register pending rows for the uncleared ask-tier leaves so a
            # successful Post can promote them into session_approvals.
            if session_id_int is not None and scope_id is not None and tool_use_id_str:
                for idx, shape_id, _leaf in pending_leaves:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO permission_ask_pending
                          (tool_use_id, leaf_index, session_id,
                           command_shape_id, scope_id, asked_at)
                        VALUES (?, ?, ?, ?, ?, ?);
                        """,
                        (
                            tool_use_id_str,
                            idx,
                            session_id_int,
                            shape_id,
                            scope_id,
                            now,
                        ),
                    )
            conn.commit()
            _emit("ask", ask_reason)
            return 0
    finally:
        conn.close()

    _emit(None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
