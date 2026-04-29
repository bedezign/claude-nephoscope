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
import sys
from pathlib import Path
from typing import Any

from nephoscope.config import get_config  # type: ignore[import-untyped]
from nephoscope.learners.permission.match._types import Verdict  # type: ignore[import-untyped]

__all__ = ["dispatch", "Verdict"]

# File-class tools promoted to full DB matching when non_bash_tool_matching=true.
# Intentionally narrower than FILE_VERBS — MultiEdit and NotebookEdit are
# excluded because their argument shape is not yet handled in production.
_FILE_CLASS_TOOLS: frozenset[str] = frozenset({"Write", "Edit", "Read"})


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

    home = str(Path.home())
    if home:
        ctx["home"] = home
        ctx["claude_dir"] = str(Path.home() / ".claude")

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


def _get_additional_dirs(
    conn: sqlite3.Connection,
    project_id: int | None,
    session_id: int | None = None,
) -> list[str]:
    """Return merged global + project + session additionalDirectories.

    Three sources, in priority order for dedup (first wins, order preserved):

    1. ``global_mirror`` — mtime-cached read from the global settings.json.
    2. ``projects`` — mtime-cached read from the project's settings.local.json.
    3. ``sessions`` — plain SELECT on ``sessions.extra_dirs``, populated from
       ``--add-dir`` flags captured at SessionStart.

    Each lookup is wrapped in a broad except so a failure in any one source
    degrades to an empty list there rather than aborting the whole merge —
    the matcher must keep returning a useful answer even when one cache is
    poisoned or one row is missing.
    """
    from nephoscope.lib.scope import Scope, get_additional_dirs  # type: ignore[import-untyped]

    def _safe_get(scope: Scope) -> list[str]:  # type: ignore[misc]
        try:
            return get_additional_dirs(conn, scope)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"nephoscope: _safe_get({scope}): {e}\n")
            return []

    global_dirs = _safe_get(Scope("global_mirror", 1))
    project_dirs = (
        _safe_get(Scope("projects", project_id)) if project_id is not None else []
    )
    session_dirs = (
        _safe_get(Scope("sessions", session_id)) if session_id is not None else []
    )

    # Deduplicate while preserving order (global first, project, session last).
    return list(dict.fromkeys(global_dirs + project_dirs + session_dirs))


def _full_match_enabled() -> bool:
    val = os.environ.get("HOOK_FULL_MATCH", "").lower()
    return val in ("1", "true", "on", "yes")


def _file_tool_matching_enabled() -> bool:
    return get_config().non_bash_tool_matching


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
    from nephoscope.lib.mirror.tool_class import classify  # type: ignore[import-untyped]

    tool_cls = classify(tool_name)
    ctx = _build_ctx(conn, project_id, cwd)
    trusted_dirs = get_config().trusted_dirs or None

    # Bash always runs full matching regardless of HOOK_FULL_MATCH.
    if tool_cls == "bash":
        from nephoscope.learners.permission.match.bash import match as _match  # type: ignore[import-untyped]

        additional_dirs = _get_additional_dirs(conn, project_id, session_id)
        return _run_tiers(
            _match,
            tool_name,
            tool_input,
            conn,
            session_id,
            project_id,
            ctx,
            additional_dirs=additional_dirs,
            trusted_dirs=trusted_dirs,
        )

    # For all other tool classes: short-circuit when HOOK_FULL_MATCH is OFF,
    # unless the tool is in _FILE_CLASS_TOOLS and non_bash_tool_matching is on.
    file_promoted = tool_name in _FILE_CLASS_TOOLS and _file_tool_matching_enabled()
    if not _full_match_enabled() and not file_promoted:
        return Verdict.NoOpinion

    # HOOK_FULL_MATCH is ON — run full matching for remaining classes.
    if tool_cls == "orchestration":
        from nephoscope.learners.permission.match.orchestration import match as _match  # type: ignore[import-untyped]

        return _match(tool_name, tool_input, conn, session_id, project_id, ctx)

    if tool_cls == "file":
        from nephoscope.learners.permission.match.file import match as _match  # type: ignore[import-untyped]

        # additional_dirs is Bash-only by design (Bash-session extra dirs are
        # not meaningful for file-path matching).  trusted_dirs is passed
        # through for $TRUSTED_DIR path-spec resolution in match/file.py.
        return _run_tiers(
            _match,
            tool_name,
            tool_input,
            conn,
            session_id,
            project_id,
            ctx,
            trusted_dirs=trusted_dirs,
        )

    if tool_cls == "flat":
        from nephoscope.learners.permission.match.flat import match as _match  # type: ignore[import-untyped]

        return _run_tiers(
            _match, tool_name, tool_input, conn, session_id, project_id, ctx
        )

    if tool_cls == "mcp":
        from nephoscope.learners.permission.match.mcp import match as _match  # type: ignore[import-untyped]

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
    additional_dirs: list[str] | None = None,
    trusted_dirs: list[str] | None = None,
) -> Verdict:
    """Iterate session → project → global tiers; return first non-NoOpinion.

    Each tier passes a scoped (session_id, project_id) pair so that
    ``lookup_permissions`` only considers rows for that tier.

    ``additional_dirs`` and ``trusted_dirs`` are forwarded as keyword
    arguments to the matcher only when they are non-None.  Only the Bash
    matcher and ``file.py`` accept them; matchers that do not declare these
    params (``flat.py``, ``mcp.py``) are safe because the kwargs are
    omitted when the values are None.
    """
    tiers: list[tuple[int | None, int | None]] = [
        (session_id, project_id),
        (None, project_id),
        (None, None),
    ]

    extra_kwargs: dict[str, Any] = {}
    if additional_dirs is not None:
        extra_kwargs["additional_dirs"] = additional_dirs
    if trusted_dirs is not None:
        extra_kwargs["trusted_dirs"] = trusted_dirs

    seen: set[tuple[int | None, int | None]] = set()
    for t_session, t_project in tiers:
        key = (t_session, t_project)
        if key in seen:
            continue
        seen.add(key)

        verdict = matcher(
            tool_name,
            tool_input,
            conn,
            t_session,
            t_project,
            ctx,
            **extra_kwargs,
        )
        if verdict != Verdict.NoOpinion:
            return verdict

    return Verdict.NoOpinion
