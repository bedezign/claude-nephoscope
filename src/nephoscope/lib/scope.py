"""Project-scope utilities for observed tool calls.

Three main exports:

1. Scope: lightweight dataclass naming the table and row id for cache-or-column reads.

2. resolve_project_root(cwd): Apply a three-rule resolution to find the
   project root from the session's working directory.
   - Rule 1: If cwd basename is "repository", return parent (three-dir workspace).
   - Rule 2: If in a git repo, return git toplevel.
   - Rule 3: Fall back to cwd.

3. paths_for_tool_call(tool, tool_input): Extract path-like values from a
   tool_input payload for use in permission checks and logging.

4. get_additional_dirs(conn, scope): Return additionalDirectories for the given
   scope. Two semantics depending on scope.table:

   - ``'global_mirror'`` / ``'projects'``: mtime-gated cache of
     ``permissions.additionalDirectories`` from the scope's settings.json
     file. Fast path returns the cached array; slow path re-parses the file
     and restamps the cache.

   - ``'sessions'``: plain SELECT on ``sessions.extra_dirs``. The column
     stores ``--add-dir`` flags captured at SessionStart from the parent
     process's argv (``/proc/{ppid}/cmdline``). No mtime, no settings file,
     no slow path — just decode the stored JSON list.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_VALID_TABLES: frozenset[str] = frozenset({"global_mirror", "projects", "sessions"})


@dataclass(frozen=True)
class Scope:
    """Names the row source for ``get_additional_dirs``.

    Three valid tables, two read shapes:

    - ``'global_mirror'`` (singleton, id=1) and ``'projects'`` (project rows
      identified by their integer pk) share the mtime-cached
      ``additionalDirectories`` columns.
    - ``'sessions'`` (per-session rows) holds captured ``--add-dir`` flags
      in ``extra_dirs``; the reader does a plain SELECT (no mtime gating).
    """

    table: str
    id: int

    def __post_init__(self) -> None:
        if self.table not in _VALID_TABLES:
            raise ValueError(
                f"Scope.table must be one of {sorted(_VALID_TABLES)},"
                f" got {self.table!r}"
            )


def get_additional_dirs(conn: sqlite3.Connection, scope: Scope) -> list[str]:
    """Return additionalDirectories for the given scope.

    Two semantics depending on ``scope.table``:

    - ``'global_mirror'`` / ``'projects'``: mtime-gated cache of
      ``permissions.additionalDirectories`` from the scope's settings.json.
      Fast path: stored mtime matches the file → return cached array.
      Slow path: mtime differs (or no cache yet) → re-read, re-parse, restamp.
      Malformed / missing / non-UTF-8 settings file: return ``[]`` and leave
      any previously-good cache unchanged.

    - ``'sessions'``: plain SELECT on ``sessions.extra_dirs``. No mtime, no
      settings file. Malformed JSON in the column returns ``[]`` without
      crashing.
    """
    if scope.table == "sessions":
        row = conn.execute(
            "SELECT extra_dirs FROM sessions WHERE id = ?;",
            (scope.id,),
        ).fetchone()
        if row is None:
            return []
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError):
            return []
        return value if isinstance(value, list) else []

    row = conn.execute(
        f"SELECT settings_json_path, settings_json_mtime, additional_dirs"
        f" FROM {scope.table} WHERE id = ?;",
        (scope.id,),
    ).fetchone()
    if row is None or not row[0]:
        return []
    path_str, cached_mtime, cached_json = row
    p = Path(path_str).expanduser()
    try:
        on_disk_mtime = p.stat().st_mtime
    except OSError:
        return []
    if cached_mtime is not None and cached_mtime == on_disk_mtime and cached_json:
        return json.loads(cached_json)
    # Slow path — re-parse the file.
    try:
        data = json.loads(p.read_bytes())
    except (OSError, ValueError, TypeError):
        return []
    dirs = (data.get("permissions") or {}).get("additionalDirectories") or []
    dirs = [str(d) for d in dirs]
    conn.execute(
        f"UPDATE {scope.table} SET settings_json_mtime = ?, additional_dirs = ?"
        f" WHERE id = ?;",
        (on_disk_mtime, json.dumps(dirs), scope.id),
    )
    return dirs


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
