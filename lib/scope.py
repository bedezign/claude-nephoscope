"""Project-scope utilities for observed tool calls.

Two main functions:

1. resolve_project_root(cwd): Apply a three-rule resolution to find the
   project root from the session's working directory.
   - Rule 1: If cwd basename is "repository", return parent (three-dir workspace).
   - Rule 2: If in a git repo, return git toplevel.
   - Rule 3: Fall back to cwd.

2. paths_for_tool_call(tool, tool_input): Extract path-like values from a
   tool_input payload for use in permission checks and logging.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


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
