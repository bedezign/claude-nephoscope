"""Typed lazy-loading config for nephoscope.

Config source: ``NEPHOSCOPE_CONFIG`` env var, or ``~/.config/nephoscope/config.toml``.

Absent config file returns defaults silently.  Malformed TOML propagates
``tomllib.TOMLDecodeError`` to the caller.

``get_config`` is wrapped in ``lru_cache`` so the file is read at most once per
process.  Tests must call ``get_config.cache_clear()`` between runs to prevent
cross-test pollution — and must change ``NEPHOSCOPE_CONFIG`` *before* calling
``get_config`` in that test, because the cache stores the result, not the path.
"""

from __future__ import annotations

import functools
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class NephoscopeConfig:
    trusted_dirs: list[str] = field(default_factory=list)
    auto_register_project_paths: bool = False
    non_bash_tool_matching: bool = True


def _coerce_trusted_dirs(value: object) -> list[str]:
    """Validate and coerce a raw ``trusted_dirs`` config value.

    - Missing / None → empty list.
    - A list of strings → returned as-is.
    - Anything else → raises ``TypeError`` with a descriptive message.

    A bare string would silently decompose to a list of single characters
    via ``list()``, which is never the intended behaviour.
    """
    if value is None:
        return []
    if isinstance(value, list):
        bad = [i for i, v in enumerate(value) if not isinstance(v, str)]
        if bad:
            raise TypeError(
                f"config: trusted_dirs items at positions {bad} are not strings"
            )
        return value  # type: ignore[return-value]
    raise TypeError(
        f"config: trusted_dirs must be a list of strings, got {type(value).__name__}"
    )


def _config_path() -> Path:
    env = os.environ.get("NEPHOSCOPE_CONFIG")
    if env:
        return Path(env)
    return Path.home() / ".config" / "nephoscope" / "config.toml"


@functools.lru_cache(maxsize=1)
def get_config() -> NephoscopeConfig:
    """Return the active NephoscopeConfig, loading from disk on first call.

    Reads ``_config_path()`` at call time (not at import).  The result is
    cached — callers that change ``NEPHOSCOPE_CONFIG`` between calls must invoke
    ``get_config.cache_clear()`` first or the old config is returned.
    """
    path = _config_path()
    if not path.exists():
        return NephoscopeConfig()
    with path.open("rb") as f:
        data = tomllib.load(f)
    return NephoscopeConfig(
        trusted_dirs=_coerce_trusted_dirs(data.get("trusted_dirs")),
        auto_register_project_paths=bool(
            data.get("auto_register_project_paths", False)
        ),
        non_bash_tool_matching=bool(data.get("non_bash_tool_matching", True)),
    )
