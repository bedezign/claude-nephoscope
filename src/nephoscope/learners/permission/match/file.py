"""File tool-class matcher (Read, Edit, Write, MultiEdit, NotebookEdit).

Looks up ``rule_shapes`` rows whose ``verb`` equals the tool name (e.g.
``"Read"``) and whose ``path_spec`` glob matches the resolved file path
from ``tool_input``.

Token resolution
----------------
``$HOME``, ``$CWD``, and ``$PROJECT_ROOT`` in stored ``path_spec`` values
are substituted with the actual paths from ``ctx`` before matching.

Returns
-------
Verdict.Allow     — matched an approved permission.
Verdict.Deny      — matched a rejected permission.
Verdict.NoOpinion — no DB row matched; fall through.
"""

from __future__ import annotations

import fnmatch
import os
import sqlite3
import sys
from pathlib import PurePosixPath
from typing import Any

from nephoscope.learners.permission.match._types import Verdict  # type: ignore[import-untyped]


def _in_workspace_root(target_path: str, roots: list[str]) -> bool:
    """Return True when *target_path* falls under any path in *roots*.

    Both target and each root are resolved through ``os.path.realpath`` +
    ``os.path.expanduser`` before comparison so that ``~``-prefixed roots,
    symlinks, and ``..`` traversals are handled correctly.

    An empty *target_path* is never considered to be inside any root — it
    would resolve to the process cwd, which could accidentally match a root
    and produce a phantom Allow.

    Note: dynamic imports via ``importlib`` or ``__import__`` bypass this
    check; the guard is intentionally static/realpath-based.
    """
    if not target_path:
        return False
    resolved_target = os.path.realpath(os.path.expanduser(target_path))
    for root in roots:
        resolved_root = os.path.realpath(os.path.expanduser(root))
        if resolved_target == resolved_root:
            return True
        if resolved_target.startswith(resolved_root + os.sep):
            return True
    return False


# Map token names (as stored in path_spec) to ctx dict keys.
_TOKEN_MAP: dict[str, str] = {
    "$HOME": "home",
    "$CWD": "cwd",
    "$PROJECT_ROOT": "project_root",
}


def _resolve_path_spec(path_spec: str, ctx: dict[str, str]) -> str:
    """Substitute ``$VAR`` tokens in *path_spec* with values from *ctx*."""
    result = path_spec
    # Substitute longest tokens first (PROJECT_ROOT before HOME if both present).
    for token, key in sorted(_TOKEN_MAP.items(), key=lambda kv: -len(kv[0])):
        if token in result and key in ctx:
            result = result.replace(token, ctx[key])
    return result


def _glob_match(pattern: str, path: str) -> bool:
    """Return True if *path* matches *pattern* (supports ``**``)."""
    if not pattern or not path:
        return False
    try:
        # PurePosixPath.match supports ** in Python 3.12+.
        if PurePosixPath(path).match(pattern):
            return True
    except Exception as e:
        sys.stderr.write(
            f"nephoscope: _glob_match failed for pattern={pattern!r}: {e}\n"
        )
    # Fallback: collapse /**  to /* for fnmatch (covers one path component).
    fallback = pattern.replace("/**", "/*").replace("**/", "*/").replace("**", "*")
    return fnmatch.fnmatch(path, fallback)


def _path_spec_matches(
    path_spec: str | None, file_path: str, ctx: dict[str, str]
) -> bool:
    """Return True when path_spec allows the given file_path."""
    if path_spec is None:
        return True  # NULL → any path
    if path_spec == "":
        return not file_path  # empty string → no-path constraint
    # Glob pattern (possibly with $VAR tokens).
    if not file_path:
        return False
    resolved = _resolve_path_spec(path_spec, ctx)
    return _glob_match(resolved, file_path)


def match(
    tool_name: str,
    tool_input: dict[str, Any],
    conn: sqlite3.Connection,
    session_id: int | None,
    project_id: int | None,
    ctx: dict[str, str],
    trusted_dirs: list[str] | None = None,
) -> Verdict:
    """Match a file-tool invocation against path-glob permission rows.

    Workspace fast-path
    -------------------
    When *trusted_dirs* is non-empty and the resolved target path falls under
    any entry in the list, ``Verdict.Allow`` is returned immediately — **before**
    the DB tier walk.  This is intentional: a "trusted directory" asserts
    unconditional approval for the entire subtree.  Explicit Deny rules for
    sub-paths inside a trusted dir are NOT consulted.  If you need deny rules
    to take effect for a path, do not include its ancestor in ``trusted_dirs``.
    """
    from nephoscope.lib.db import lookup_permissions  # type: ignore[import-untyped]

    file_path: str = ""
    if isinstance(tool_input, dict):
        file_path = tool_input.get("file_path") or tool_input.get("path") or ""
        if not isinstance(file_path, str):
            file_path = ""

    # Workspace fast-path: unconditional Allow for any path under a trusted dir.
    if trusted_dirs and _in_workspace_root(file_path, trusted_dirs):
        return Verdict.Allow

    rows = conn.execute(
        "SELECT id, path_spec FROM rule_shapes WHERE verb = ?;",
        (tool_name,),
    ).fetchall()

    for shape_id_raw, path_spec in rows:
        if not _path_spec_matches(path_spec, file_path, ctx):
            continue

        perms = lookup_permissions(conn, int(shape_id_raw), session_id, project_id)
        if not perms:
            continue
        decision = perms[0]["decision"]
        if decision == "approved":
            return Verdict.Allow
        if decision == "rejected":
            return Verdict.Deny

    return Verdict.NoOpinion
