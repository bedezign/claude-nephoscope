"""Per-tool-class match dispatch.

Public API
----------
``dispatch(tool_name, tool_input, conn, session_id, project_id, cwd=None) -> Verdict``

    Routes to the appropriate per-class matcher and returns a
    :class:`~learners.permission._types.Verdict`.

``Verdict``
    Re-exported for convenience so callers only need to import from this
    package.

``HOOK_FULL_MATCH`` env var
---------------------------
Defaults to **OFF** (empty / ``"0"`` / ``"false"``).

- When **OFF**: Bash always runs full matching; all other tool classes
  short-circuit to ``Verdict.NoOpinion`` (the JSON mirror is authoritative
  and the native Claude Code gate handles them).
- When **ON** (``"1"`` / ``"true"`` / ``"on"`` / ``"yes"``): every tool
  class runs its full DB-backed matcher for debugging.

Bash is unaffected by the flag — it always runs full matching.
Orchestration tools always return ``Verdict.Allow`` when ``HOOK_FULL_MATCH``
is ON (no DB lookup needed).

Tier priority
-------------
Tier ordering (session → project → global) is enforced here in the dispatch
wrapper, not inside individual matchers.  The dispatch iterates tiers and
returns the first non-``NoOpinion`` verdict.

Tiers:
  - session:  ``session_id`` is non-NULL
  - project:  ``project_id`` is non-NULL, ``session_id`` is NULL
  - global:   both are NULL
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from learners.permission.match._types import Verdict  # type: ignore[import-untyped]

__all__ = ["dispatch", "Verdict"]


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------


def _lookup_project_info(
    conn: sqlite3.Connection, project_id: int
) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT cwd, root FROM projects WHERE id = ?;", (project_id,)
    ).fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def _build_ctx(
    conn: sqlite3.Connection,
    project_id: int | None,
    cwd: str | None = None,
) -> dict[str, str]:
    """Build path-substitution context from environment + DB."""
    ctx: dict[str, str] = {}

    home = os.path.expanduser("~")
    if home:
        ctx["home"] = home

    effective_cwd = cwd or ""
    project_root: str | None = None

    if project_id is not None:
        db_cwd, db_root = _lookup_project_info(conn, project_id)
        if not effective_cwd and db_cwd:
            effective_cwd = db_cwd
        project_root = db_root or None

    if effective_cwd:
        ctx["cwd"] = effective_cwd
    if project_root:
        ctx["project_root"] = project_root

    return ctx


def _full_match_enabled() -> bool:
    val = os.environ.get("HOOK_FULL_MATCH", "").lower()
    return val in ("1", "true", "on", "yes")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def dispatch(
    tool_name: str,
    tool_input: dict[str, Any],
    conn: sqlite3.Connection,
    session_id: int | None,
    project_id: int | None,
    cwd: str | None = None,
) -> Verdict:
    """Route *tool_name* to the appropriate matcher and return a Verdict.

    Tier priority (session → project → global) is enforced here: the
    dispatch iterates tiers and returns the first non-``NoOpinion`` result.
    """
    from lib.mirror.tool_class import classify  # type: ignore[import-untyped]

    tool_cls = classify(tool_name)
    ctx = _build_ctx(conn, project_id, cwd)

    # Bash always runs full matching regardless of HOOK_FULL_MATCH.
    if tool_cls == "bash":
        from learners.permission.match.bash import match as _match  # type: ignore[import-untyped]

        return _run_tiers(
            _match, tool_name, tool_input, conn, session_id, project_id, ctx
        )

    # For all other tool classes: short-circuit when HOOK_FULL_MATCH is OFF.
    if not _full_match_enabled():
        return Verdict.NoOpinion

    # HOOK_FULL_MATCH is ON — run full matching for remaining classes.
    if tool_cls == "orchestration":
        from learners.permission.match.orchestration import match as _match  # type: ignore[import-untyped]

        return _match(tool_name, tool_input, conn, session_id, project_id, ctx)

    if tool_cls == "file":
        from learners.permission.match.file import match as _match  # type: ignore[import-untyped]

        return _run_tiers(
            _match, tool_name, tool_input, conn, session_id, project_id, ctx
        )

    if tool_cls == "flat":
        from learners.permission.match.flat import match as _match  # type: ignore[import-untyped]

        return _run_tiers(
            _match, tool_name, tool_input, conn, session_id, project_id, ctx
        )

    if tool_cls == "mcp":
        from learners.permission.match.mcp import match as _match  # type: ignore[import-untyped]

        return _run_tiers(
            _match, tool_name, tool_input, conn, session_id, project_id, ctx
        )

    return Verdict.NoOpinion


def _run_tiers(
    matcher: Any,
    tool_name: str,
    tool_input: dict[str, Any],
    conn: sqlite3.Connection,
    session_id: int | None,
    project_id: int | None,
    ctx: dict[str, str],
) -> Verdict:
    """Iterate session → project → global tiers; return first non-NoOpinion.

    Each tier passes a scoped (session_id, project_id) pair so that
    ``lookup_permissions`` only considers rows for that tier.
    """
    tiers: list[tuple[int | None, int | None]] = [
        (session_id, project_id),  # session tier (session_id may be None)
        (None, project_id),  # project tier
        (None, None),  # global tier
    ]

    seen: set[tuple[int | None, int | None]] = set()
    for t_session, t_project in tiers:
        key = (t_session, t_project)
        if key in seen:
            continue
        seen.add(key)

        verdict = matcher(tool_name, tool_input, conn, t_session, t_project, ctx)
        if verdict != Verdict.NoOpinion:
            return verdict

    return Verdict.NoOpinion
