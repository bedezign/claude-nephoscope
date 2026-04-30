"""MCP tool-class matcher.

Handles fully-qualified MCP tool names (``mcp__<ns>__<tool>``) and
namespace wildcards (``mcp__<ns>__*``).

Lookup order
------------
1. Literal match: ``rule_shapes.verb == tool_name`` exactly.
2. Namespace wildcard: ``rule_shapes.verb == mcp__<ns>__*`` where ``<ns>``
   is the namespace extracted from ``tool_name``.

Returns
-------
Verdict.Allow     — first matching row is approved.
Verdict.Deny      — first matching row is rejected.
Verdict.NoOpinion — no matching rule shape; fall through.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from nephoscope.learners.permission.match._types import Verdict  # type: ignore[import-untyped]

_MCP_RE = re.compile(r"^mcp__([\w-]+)__([\w-]+|\*)$")


def _ns_wildcard(tool_name: str) -> str | None:
    """Return the ``mcp__<ns>__*`` wildcard form for *tool_name*, or None."""
    m = _MCP_RE.match(tool_name)
    if m is None:
        return None
    ns = m.group(1)
    return f"mcp__{ns}__*"


def _verdict_for_verb(
    verb: str,
    conn: sqlite3.Connection,
    session_id: int | None,
    project_id: int | None,
) -> tuple[Verdict, int | None]:
    """Look up permissions for *verb* and return ``(Verdict, permission_id)``."""
    from nephoscope.lib.db import lookup_permissions  # type: ignore[import-untyped]

    row = conn.execute(
        "SELECT id FROM rule_shapes WHERE verb = ?;",
        (verb,),
    ).fetchone()
    if row is None:
        return Verdict.NoOpinion, None

    shape_id = int(row[0])
    perms = lookup_permissions(conn, shape_id, session_id, project_id)
    if not perms:
        return Verdict.NoOpinion, None

    perm = perms[0]
    decision = perm["decision"]
    perm_id: int = perm["id"]
    if decision == "approved":
        return Verdict.Allow, perm_id
    if decision == "rejected":
        return Verdict.Deny, perm_id
    return Verdict.NoOpinion, None


def match(
    tool_name: str,
    tool_input: dict[str, Any],
    conn: sqlite3.Connection,
    session_id: int | None,
    project_id: int | None,
    ctx: dict[str, str],
    additional_dirs: list[str] | None = None,  # noqa: ARG001 — unused; Bash-only feature
) -> tuple[Verdict, int | None]:
    """Match an MCP tool invocation against literal + wildcard permission rows.

    Returns ``(Verdict, permission_id)`` where ``permission_id`` is the
    ``permissions.id`` of the matched row, or ``None`` when no match was found.
    """
    # 1. Literal match.
    verdict, perm_id = _verdict_for_verb(tool_name, conn, session_id, project_id)
    if verdict != Verdict.NoOpinion:
        return verdict, perm_id

    # 2. Namespace wildcard (mcp__<ns>__*).
    wildcard = _ns_wildcard(tool_name)
    if wildcard is not None and wildcard != tool_name:
        verdict, perm_id = _verdict_for_verb(wildcard, conn, session_id, project_id)
        if verdict != Verdict.NoOpinion:
            return verdict, perm_id

    return Verdict.NoOpinion, None
