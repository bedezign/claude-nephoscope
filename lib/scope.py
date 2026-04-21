"""Project-scope classification for observed tool calls.

Every tool call is classified as ``within_project`` / ``outside_project`` /
``mixed`` / ``no_path`` relative to its session's project root. The "root"
is resolved once per project row (stored in ``projects.root``) using three
rules in order:

1. If ``basename(cwd) == "repository"``, root is the parent dir. This
   matches the three-directory workspace convention where the git checkout
   lives at ``.../<project>/repository/`` and workspace-level state
   (``.claude/``, plans, notes) lives at ``.../<project>/``.
2. Else if ``git -C cwd rev-parse --show-toplevel`` succeeds, its output is
   the root.
3. Else the cwd itself.

A ``no_path`` classification means the tool had no path inputs to classify
— `Bash` calls with no positional paths, ``WebFetch``, MCP calls, etc.
Downstream (permission learner) treats this as "scope-agnostic".
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

# Scope enum names — must match tool_call_scopes rows seeded by v11.sql.
WITHIN_PROJECT = "within_project"
OUTSIDE_PROJECT = "outside_project"
MIXED = "mixed"
NO_PATH = "no_path"
ANY = "any"

# Path-bearing tool inputs. For each tool, the key(s) whose values are
# filesystem paths we want to classify. Bash is handled separately via
# the canonicalizer's positional_paths.
_PATH_FIELDS: dict[str, tuple[str, ...]] = {
    "Read": ("file_path",),
    "Edit": ("file_path",),
    "Write": ("file_path",),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path", "file_path"),
    "Grep": ("path",),
    "Glob": ("path",),
}


def resolve_project_root(cwd: str) -> str | None:
    """Apply the three-rule resolution and return an absolute path string.

    Returns ``None`` iff ``cwd`` itself is falsy — otherwise always returns a
    string. The resolution never fails: even if the cwd doesn't exist on
    disk, rule (3) returns it verbatim and the caller gets something usable.
    """
    if not cwd:
        return None
    try:
        path = Path(cwd)
    except (TypeError, ValueError):
        return cwd

    # Rule 1: three-dir workspace convention (cwd/repository/ → cwd/).
    if path.name == "repository":
        return str(path.parent)

    # Rule 2: git toplevel. Shell-out is bounded to 2s; any failure (not a
    # repo, git missing, permission denied) falls through to rule 3.
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        proc = None

    if proc is not None and proc.returncode == 0:
        out = proc.stdout.strip()
        if out:
            return out

    # Rule 3: fall back to cwd unchanged.
    return str(path)


def classify_paths(paths: list[str], project_root: str | None) -> str:
    """Classify one or more paths against ``project_root``.

    - No paths → ``no_path``.
    - No project_root → ``no_path`` (we can't decide; caller treats this as
      scope-agnostic).
    - All paths resolve inside root → ``within_project``.
    - All paths resolve outside → ``outside_project``.
    - Mix of both → ``mixed``.

    Paths are resolved to absolute form before the prefix check, so relative
    paths, ``./foo``, and ``../outside`` are handled correctly.
    """
    if not paths:
        return NO_PATH
    if not project_root:
        return NO_PATH

    # Normalize the root once. Add a trailing slash so "in root" checks
    # don't match sibling paths like /foo/barextra when root is /foo/bar.
    root_abs = str(Path(project_root).resolve())
    root_prefix = root_abs.rstrip("/") + "/"

    inside = 0
    outside = 0
    for p in paths:
        if not p:
            continue
        try:
            abs_p = str(Path(p).resolve())
        except (OSError, RuntimeError, ValueError):
            # Unresolvable path (e.g. broken symlink chain). Treat as outside
            # — conservative: better to re-prompt than to auto-allow.
            outside += 1
            continue
        if abs_p == root_abs or abs_p.startswith(root_prefix):
            inside += 1
        else:
            outside += 1

    if inside and outside:
        return MIXED
    if inside:
        return WITHIN_PROJECT
    if outside:
        return OUTSIDE_PROJECT
    # All paths were empty strings after filtering.
    return NO_PATH


def paths_for_tool_call(tool: str, tool_input: dict[str, Any]) -> list[str]:
    """Extract path-like values from a tool_input payload for scope checks.

    For Bash, the caller should canonicalize the command and collect
    ``positional_paths`` across leaves — that's more accurate than grepping
    the raw command string. This helper only handles the non-Bash tools
    with declarative path fields (Read/Edit/Write/Grep/Glob/NotebookEdit).
    """
    if not isinstance(tool_input, dict):
        return []
    keys = _PATH_FIELDS.get(tool)
    if not keys:
        return []
    out: list[str] = []
    for key in keys:
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            out.append(v)
    return out
