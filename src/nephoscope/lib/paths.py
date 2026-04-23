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
