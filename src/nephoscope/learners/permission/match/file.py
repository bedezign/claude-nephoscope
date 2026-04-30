"""File tool-class matcher (Read, Edit, Write, MultiEdit, NotebookEdit).

Looks up ``rule_shapes`` rows whose ``verb`` equals the tool name (e.g.
``"Read"``) or a verb group containing it (e.g. ``"Reading"``, ``"Full
Access"``) and whose ``path_spec`` glob matches the resolved file path
from ``tool_input``.

Token resolution
----------------
``$HOME``, ``$CWD``, ``$PROJECT_ROOT``, and ``$TRUSTED_DIR`` in stored
``path_spec`` values are substituted with the actual paths from ``ctx`` /
``trusted_dirs`` before matching.  ``$TRUSTED_DIR`` is multi-valued: a
path_spec containing it is expanded once per configured trusted directory;
the file_path matches if it satisfies any expansion.  When ``trusted_dirs``
is empty or None, ``$TRUSTED_DIR``-shaped path_specs match nothing.

Trusted-dir scoping
-------------------
Rules with ``$TRUSTED_DIR`` path-specs are resolved against the configured
``trusted_dirs`` list at match time — the same model as ``$HOME`` / ``$CWD``
/ ``$PROJECT_ROOT``, but multi-valued.  Every match goes through the full DB
tier walk.  Explicit Deny rules for sub-paths inside a trusted dir DO fire:
a ``$TRUSTED_DIR/.env`` deny rule overrides a ``$TRUSTED_DIR/**`` allow rule.

Returns
-------
Verdict.Deny      — most specific matched rule_shape(s) include a rejected decision,
                    or tied rules include a conflicting rejected decision (deny-on-tie).
Verdict.Allow     — all most-specific matched rule_shapes have approved decisions.
Verdict.NoOpinion — no DB row matched; fall through.
"""

from __future__ import annotations

import fnmatch
import sqlite3
import sys
from pathlib import PurePosixPath
from typing import Any

from nephoscope.learners.permission.match._types import Verdict  # type: ignore[import-untyped]

# Map token names (as stored in path_spec) to ctx dict keys.
# Single-valued tokens only — $TRUSTED_DIR is multi-valued and handled separately.
_TOKEN_MAP: dict[str, str] = {
    "$HOME": "home",
    "$CWD": "cwd",
    "$PROJECT_ROOT": "project_root",
    "$CLAUDE_DIR": "claude_dir",
}

_TRUSTED_DIR_TOKEN = "$TRUSTED_DIR"


def _wildcard_count(path_spec: str | None) -> int:
    """Return the number of path components containing '*' in *path_spec*.

    None (any-path) is the least specific, represented as sys.maxsize.
    Literal paths with no wildcards return 0 (most specific).
    """
    if path_spec is None:
        return sys.maxsize
    return sum(1 for part in path_spec.split("/") if "*" in part)


def _resolve_path_spec(path_spec: str, ctx: dict[str, str]) -> str:
    """Substitute single-valued ``$VAR`` tokens in *path_spec* with values from *ctx*."""
    result = path_spec
    # Substitute longest tokens first (PROJECT_ROOT before HOME if both present).
    for token, key in sorted(_TOKEN_MAP.items(), key=lambda kv: -len(kv[0])):
        if token in result and key in ctx:
            result = result.replace(token, ctx[key])
    return result


def _glob_match(pattern: str, path: str) -> bool:
    """Return True if *path* matches *pattern* (supports ``**``).

    Claude Code stores absolute path patterns with a leading ``//`` double-slash
    (e.g. ``//usr/local/lib/**``).  POSIX leaves ``//`` prefix
    implementation-defined; ``PurePosixPath`` treats it as root ``"//"``
    which makes ``.match()`` return ``False`` against a normal single-slash
    path.  Normalise to a single leading slash before matching.
    """
    if not pattern or not path:
        return False
    # Normalise double-leading-slash that Claude Code emits for absolute patterns.
    if pattern.startswith("//"):
        pattern = pattern[1:]
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
    path_spec: str | None,
    file_path: str,
    ctx: dict[str, str],
    trusted_dirs: list[str] | None = None,
) -> bool:
    """Return True when *path_spec* matches *file_path*.

    Single-valued tokens (``$HOME``, ``$CWD``, ``$PROJECT_ROOT``) are
    substituted from *ctx*.  The multi-valued ``$TRUSTED_DIR`` token is
    expanded once per entry in *trusted_dirs*; the spec matches if any
    expansion matches.  When *trusted_dirs* is empty/None, a path_spec
    containing ``$TRUSTED_DIR`` never matches.
    """
    if path_spec is None:
        return True  # NULL → any path
    if path_spec == "":
        return not file_path  # empty string → no-path constraint
    if not file_path:
        return False

    if _TRUSTED_DIR_TOKEN in path_spec:
        if not trusted_dirs:
            return False
        for trusted_dir in trusted_dirs:
            expanded = path_spec.replace(_TRUSTED_DIR_TOKEN, trusted_dir)
            # Resolve remaining single-valued tokens in the expanded spec.
            resolved = _resolve_path_spec(expanded, ctx)
            if _glob_match(resolved, file_path):
                return True
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
) -> tuple[Verdict, int | None]:
    """Match a file-tool invocation against path-glob permission rows.

    Applies specificity-first conflict resolution: among all matching rules,
    only those with the lowest wildcard count (most specific path_spec) vote.
    If any of the most-specific rules has a ``rejected`` decision, returns
    ``(Verdict.Deny, perm_id)`` (also used as deny-on-tie when counts are equal).
    If all most-specific rules have ``approved`` decisions, returns
    ``(Verdict.Allow, perm_id)``.  Otherwise returns ``(Verdict.NoOpinion, None)``.

    ``perm_id`` is the ``permissions.id`` of the decisive row.
    """
    from nephoscope.lib.db import lookup_permissions  # type: ignore[import-untyped]
    from nephoscope.lib.mirror.tool_class import VERB_GROUPS

    file_path: str = ""
    if isinstance(tool_input, dict):
        file_path = tool_input.get("file_path") or tool_input.get("path") or ""
        if not isinstance(file_path, str):
            file_path = ""

    # Candidate verbs: the literal tool name plus any group names that contain it.
    candidate_verbs = [tool_name] + [
        group_name
        for group_name, members in VERB_GROUPS.items()
        if tool_name in members
    ]
    placeholders = ",".join("?" * len(candidate_verbs))
    rows = conn.execute(
        f"SELECT id, path_spec FROM rule_shapes WHERE verb IN ({placeholders});",
        candidate_verbs,
    ).fetchall()

    matched: list[tuple[str | None, str, int]] = []  # (path_spec, decision, perm_id)
    for shape_id_raw, path_spec in rows:
        if not _path_spec_matches(path_spec, file_path, ctx, trusted_dirs):
            continue
        perms = lookup_permissions(conn, int(shape_id_raw), session_id, project_id)
        if not perms:
            continue
        matched.append((path_spec, perms[0]["decision"], perms[0]["id"]))

    if not matched:
        return Verdict.NoOpinion, None

    with_wc = [(_wildcard_count(ps), d, pid) for ps, d, pid in matched]
    min_wc = min(w for w, _, _ in with_wc)
    specific = [(d, pid) for w, d, pid in with_wc if w == min_wc]

    if any(d == "rejected" for d, _ in specific):
        # Return the first deny perm_id
        deny_pid = next((pid for d, pid in specific if d == "rejected"), None)
        return Verdict.Deny, deny_pid
    if all(d == "approved" for d, _ in specific):
        # Return the first approve perm_id
        allow_pid = next((pid for d, pid in specific if d == "approved"), None)
        return Verdict.Allow, allow_pid
    return Verdict.NoOpinion, None
