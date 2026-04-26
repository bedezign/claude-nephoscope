"""Flat tool-class matcher (Grep, Glob, WebSearch, …).

Flat tools have no meaningful argument structure for permission purposes and
are always serialised bare in settings.json. The matcher checks whether any
``permissions`` row exists for the verb and returns the row's decision.

Returns
-------
Verdict.Allow     — matched an approved permission row.
Verdict.Deny      — matched a rejected permission row.
Verdict.NoOpinion — no row found for this verb; fall through.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from nephoscope.learners.permission.match._types import Verdict  # type: ignore[import-untyped]


def match(
    tool_name: str,
    tool_input: dict[str, Any],
    conn: sqlite3.Connection,
    session_id: int | None,
    project_id: int | None,
    ctx: dict[str, str],
    additional_dirs: list[str] | None = None,  # noqa: ARG001 — unused; Bash-only feature
) -> Verdict:
    """Presence check: any permission row for *tool_name* → return its decision."""
    from nephoscope.lib.db import lookup_permissions  # type: ignore[import-untyped]

    rows = conn.execute(
        "SELECT id FROM rule_shapes WHERE verb = ?;",
        (tool_name,),
    ).fetchall()

    for (shape_id_raw,) in rows:
        shape_id = int(shape_id_raw)
        perms = lookup_permissions(conn, shape_id, session_id, project_id)
        if not perms:
            continue
        decision = perms[0]["decision"]
        if decision == "approved":
            return Verdict.Allow
        if decision == "rejected":
            return Verdict.Deny

    return Verdict.NoOpinion
