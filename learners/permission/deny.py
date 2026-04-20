"""Deny-list evaluation for canonicalized leaves.

Two-layer model:

1. **Declarative** — ``config/deny.yaml`` lists denied verbs, verb+subcommand
   pairs, and verb+flag-pattern pairs. Easy to extend without code changes.
2. **Procedural** — ``is_denied`` also applies rules that are awkward to
   express in YAML: the universal ``sudo`` ban, destructive redirections
   into guarded system paths, and truncate-writes (``>``) that would
   overwrite an existing regular file.

Appending (``>>``) is not denied on its own — it's typically log growth,
not data destruction — unless the target sits under a guarded path.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from .canonicalize import CanonicalLeaf


_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "deny.yaml"

# Redirections into any of these prefixes are always denied, regardless
# of whether the target currently exists. These are the paths whose
# integrity we never want a learned pattern to be able to corrupt.
_GUARDED_WRITE_PREFIXES: tuple[str, ...] = (
    "/home/steve/.claude/",
    "/home/steve/.cache/claude/",
    "/etc/",
    "/var/",
    "/usr/",
    "/boot/",
    "/sys/",
    "/proc/",
)

_cached_config: dict[str, Any] | None = None


def _load_config() -> dict[str, Any]:
    """Lazy-load and cache the YAML config."""
    global _cached_config
    if _cached_config is not None:
        return _cached_config
    if not _CONFIG_PATH.is_file():
        _cached_config = {}
        return _cached_config
    with _CONFIG_PATH.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    if not isinstance(loaded, dict):
        loaded = {}
    _cached_config = loaded
    return _cached_config


def _reset_cache() -> None:
    """Test hook — force a re-read of the config on the next call."""
    global _cached_config
    _cached_config = None


def is_denied(leaf: CanonicalLeaf) -> tuple[bool, str | None]:
    """Return ``(True, reason)`` if any deny rule fires, else ``(False, None)``.

    Rules are checked in order from cheapest to most expensive (filesystem
    stat last) so common allow-paths stay fast.
    """
    config = _load_config()

    # 1. Procedural: sudo is never allowed.
    if leaf.verb == "sudo":
        return True, "verb 'sudo' is never auto-allowed"

    # 2. Declarative: denied verbs.
    denied_verbs = config.get("denied_verbs") or []
    if isinstance(denied_verbs, list) and leaf.verb in denied_verbs:
        return True, f"verb '{leaf.verb}' is in deny list"

    # 3. Declarative: denied verb+subcommand pairs.
    denied_subcommands = config.get("denied_subcommands") or {}
    if isinstance(denied_subcommands, dict):
        subs = denied_subcommands.get(leaf.verb) or []
        if (
            isinstance(subs, list)
            and leaf.subcommand is not None
            and leaf.subcommand in subs
        ):
            return True, f"subcommand '{leaf.verb} {leaf.subcommand}' is in deny list"

    # 4. Declarative: denied flag patterns for this verb.
    denied_flag_patterns = config.get("denied_flag_patterns") or {}
    if isinstance(denied_flag_patterns, dict):
        patterns = denied_flag_patterns.get(leaf.verb) or []
        if isinstance(patterns, list):
            for pattern in patterns:
                if pattern in leaf.flags:
                    return True, (
                        f"flag '{pattern}' on '{leaf.verb}' is in deny list"
                    )

    # 5. Procedural: redirection guards.
    for redir in leaf.redirections:
        reason = _redirect_reason(redir.op, redir.target)
        if reason is not None:
            return True, reason

    return False, None


def _redirect_reason(op: str, target: str) -> str | None:
    """Classify a redirection as denied or acceptable.

    - Any write (``>`` or ``>>``) into a guarded prefix is denied.
    - A truncate-write (``>``) over an existing regular file is denied.
    - Append (``>>``) outside guarded paths is acceptable.
    - Reads (``<``) and fd dups are acceptable.
    """
    if op in (">", ">>"):
        for prefix in _GUARDED_WRITE_PREFIXES:
            if target.startswith(prefix):
                return f"redirection '{op} {target}' targets guarded path"
    if op == ">":
        try:
            # Resolve symlinks so the stat reflects the ultimate target.
            # os.path.isfile handles broken symlinks gracefully (returns False).
            if os.path.isfile(target):
                return (
                    f"redirection '> {target}' would overwrite existing file"
                )
        except OSError:
            # Unreadable parent dirs etc. — treat as "cannot confirm safe".
            return f"redirection '> {target}' could not be stat'd"
    return None
