"""Shared path resolution for runtime paths.

All lookups are lazy (evaluated on call, not at import) so tests using
``monkeypatch.setenv`` work without patching module globals.

Resolution order for each resource:

- Observations DB:     ``OBSERVABILITY_DB`` → ``${CLAUDE_PLUGIN_DATA}/observations.db``
                       → ``~/.cache/nephoscope/observations.db``
- Disable marker:      ``NEPHOSCOPE_DISABLE_MARKER`` → ``${CLAUDE_PLUGIN_DATA}/disabled``
                       → ``~/.config/nephoscope/disabled``
- Instincts directory: ``NEPHOSCOPE_INSTINCT_DIR`` → ``${CLAUDE_PLUGIN_DATA}/instincts``
                       → ``~/.claude/instincts``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _plugin_data_dir() -> Path | None:
    """Return ``${CLAUDE_PLUGIN_DATA}`` as a Path, or None if unset/empty."""
    val = os.environ.get("CLAUDE_PLUGIN_DATA")
    return Path(val) if val else None


def observations_db_path() -> Path:
    """Resolve the observations DB path.

    Order: ``OBSERVABILITY_DB`` env > ``${CLAUDE_PLUGIN_DATA}/observations.db``
    > ``~/.cache/nephoscope/observations.db``.
    """
    env = os.environ.get("OBSERVABILITY_DB")
    if env:
        return Path(env)
    plugin_data = _plugin_data_dir()
    if plugin_data is not None:
        return plugin_data / "observations.db"
    return Path.home() / ".cache" / "nephoscope" / "observations.db"


def disable_marker_path() -> Path:
    """Resolve the opt-out marker path.

    Order: ``NEPHOSCOPE_DISABLE_MARKER`` env > ``${CLAUDE_PLUGIN_DATA}/disabled``
    > ``~/.config/nephoscope/disabled``.
    """
    env = os.environ.get("NEPHOSCOPE_DISABLE_MARKER")
    if env:
        return Path(env)
    plugin_data = _plugin_data_dir()
    if plugin_data is not None:
        return plugin_data / "disabled"
    return Path.home() / ".config" / "nephoscope" / "disabled"


def instinct_dir() -> Path:
    """Resolve the instinct write directory.

    Order: ``NEPHOSCOPE_INSTINCT_DIR`` env > ``${CLAUDE_PLUGIN_DATA}/instincts``
    > ``~/.claude/instincts``.
    """
    env = os.environ.get("NEPHOSCOPE_INSTINCT_DIR")
    if env:
        return Path(env)
    plugin_data = _plugin_data_dir()
    if plugin_data is not None:
        return plugin_data / "instincts"
    return Path.home() / ".claude" / "instincts"


def is_disabled() -> bool:
    """Return True when the opt-out marker exists."""
    try:
        return disable_marker_path().is_file()
    except OSError:
        return False


def _default_cmdline_path() -> Path:
    """Return the path to the parent process's cmdline file.

    Default resolves on each call so tests can monkeypatch this function to
    point at a fake /proc layout instead of touching os.getppid.
    """
    return Path(f"/proc/{os.getppid()}/cmdline")


def _consume_variadic_values(parts: list[str], start: int) -> tuple[list[str], int]:
    """Consume positional values after a bare ``--add-dir`` flag.

    Reads from ``parts[start]`` forward, stopping at the next ``-`` flag,
    ``--``, or end of list.  Returns ``(collected_values, next_index)``.
    """
    values: list[str] = []
    i = start
    while i < len(parts):
        value = parts[i]
        if value == "--" or value.startswith("-"):
            break
        if value:
            values.append(value)
        i += 1
    return values, i


def _collect_add_dir_values(parts: list[str]) -> list[str]:
    """Walk a decoded argv list and return all ``--add-dir`` values.

    Handles both ``--add-dir <val> [val ...]`` (variadic) and
    ``--add-dir=<val>`` (inline) forms.  Stops scanning at ``--``.
    """
    out: list[str] = []
    i = 0
    while i < len(parts):
        token = parts[i]
        if token == "--":
            break
        if token == "--add-dir":
            values, i = _consume_variadic_values(parts, i + 1)
            out.extend(values)
            continue
        if token.startswith("--add-dir="):
            value = token[len("--add-dir=") :]
            if value:
                out.append(value)
        i += 1
    return out


def extract_add_dir_args(cmdline_path: Path | str | None = None) -> list[str]:
    """Parse the parent process's argv for ``--add-dir`` values.

    Reads ``/proc/<ppid>/cmdline`` (NUL-separated argv) and returns the list
    of values passed via ``--add-dir <value> [value ...]`` (variadic, mirrors
    Claude Code's own arg parsing) and ``--add-dir=<value>``.

    The variadic shape means a sequence like ``--add-dir /a /b -c`` captures
    both ``/a`` and ``/b`` and stops at the next ``-`` flag. ``--`` terminates
    flag parsing entirely.

    ``cmdline_path`` is overridable for tests; the default resolves the path
    lazily from ``os.getppid()`` (via ``_default_cmdline_path``) so each call
    sees the current parent.

    Returns canonicalized paths via ``canonicalize()`` so storage matches the
    rest of the DB's path columns (tilde expansion, symlink resolution).

    Returns ``[]`` on any failure: file missing, non-Linux, parse error,
    permission denied, malformed UTF-8. Captures must never crash session
    start; failure here just means the recorder records an empty extras list.
    """
    if cmdline_path is None:
        cmdline_path = _default_cmdline_path()
    try:
        raw = Path(cmdline_path).read_bytes()
    except OSError as exc:
        print(
            f"[nephoscope] extract_add_dir_args: {type(exc).__name__}: {exc.strerror}",
            file=sys.stderr,
        )
        return []
    if not raw:
        return []
    # /proc/<pid>/cmdline is NUL-separated; trailing NUL leaves an empty tail.
    parts = [p.decode("utf-8", errors="replace") for p in raw.split(b"\x00") if p]
    raw_values = _collect_add_dir_values(parts)
    return [c for c in (canonicalize(p) for p in raw_values) if c]


def canonicalize(p: str | Path | None) -> str:
    """Return a stored-path form: expanduser + resolve, str-ified.

    Use at every DB path-write site so the following columns hold one
    canonical string per logical file regardless of which tilde/symlink
    form the caller passed in:

    - ``projects.cwd``
    - ``projects.root``
    - ``projects.settings_json_path`` (future INSERT sites)
    - ``global_mirror.settings_json_path`` (future INSERT sites)
    - ``file_paths.path``
    - ``sessions.transcript_path``

    Empty / ``None`` inputs round-trip to the empty string — some callers
    pass an unset cwd and we don't want to synthesize a garbage path.

    Uses ``resolve(strict=False)`` so non-existent paths don't raise —
    ``file_paths`` routinely holds paths that existed only briefly. If
    ``resolve`` itself raises ``OSError`` (permission denied while
    walking a symlink, for example), falls back to the expanduser-only
    form rather than propagating — canonicalize sits on hot DB-write
    paths and a single unreadable dir must not crash the recorder. The
    fallback is still deterministic and still collapses tilde variants,
    it just doesn't chase symlinks through the inaccessible segment.
    """
    if not p:
        return ""
    expanded = Path(p).expanduser()
    try:
        return str(expanded.resolve(strict=False))
    except OSError:
        return str(expanded)
