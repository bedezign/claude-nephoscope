"""Permission evaluation for canonicalized leaves.

Three-valued outcome: ``"deny"`` (hard block), ``"ask"`` (user-confirmable),
or ``None`` (no opinion — fall through to the learner's allowlist check).

Two configuration layers:

1. **Declarative** — ``config/deny.yaml`` splits rules into ``denied_*``
   (hard block) and ``ask_*`` (confirmable) tiers. Easy to extend without
   code changes.
2. **Procedural** — rules awkward to express in YAML: the universal
   ``sudo`` ban, destructive redirections into guarded system paths
   (hard deny), and truncate-writes (``>``) over existing files (ask).

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

# Redirections into any of these prefixes are always hard-denied, regardless
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


def evaluate(leaf: CanonicalLeaf) -> tuple[str | None, str | None]:
    """Evaluate a leaf against the deny + ask tiers.

    Returns ``("deny", reason)`` for hard blocks, ``("ask", reason)`` for
    user-confirmable operations, or ``(None, None)`` when no rule fires.
    Deny is always checked before ask so a catastrophic pattern can never
    be downgraded by an overlapping ask rule.
    """
    config = _load_config()

    # --- Hard deny tier ---

    # Procedural: sudo is never allowed.
    if leaf.verb == "sudo":
        return "deny", "verb 'sudo' is never auto-allowed"

    denied_verbs = config.get("denied_verbs") or []
    if isinstance(denied_verbs, list) and leaf.verb in denied_verbs:
        return "deny", f"verb '{leaf.verb}' is in deny list"

    denied_subcommands = config.get("denied_subcommands") or {}
    if isinstance(denied_subcommands, dict):
        subs = denied_subcommands.get(leaf.verb) or []
        if (
            isinstance(subs, list)
            and leaf.subcommand is not None
            and leaf.subcommand in subs
        ):
            return (
                "deny",
                f"subcommand '{leaf.verb} {leaf.subcommand}' is in deny list",
            )

    denied_flag_patterns = config.get("denied_flag_patterns") or {}
    if isinstance(denied_flag_patterns, dict):
        patterns = denied_flag_patterns.get(leaf.verb) or []
        if isinstance(patterns, list):
            for pattern in patterns:
                if pattern in leaf.flags:
                    return (
                        "deny",
                        f"flag '{pattern}' on '{leaf.verb}' is in deny list",
                    )

    # Procedural: redirections into guarded system paths are hard-denied.
    for redir in leaf.redirections:
        if redir.op in (">", ">>"):
            for prefix in _GUARDED_WRITE_PREFIXES:
                if redir.target.startswith(prefix):
                    return (
                        "deny",
                        f"redirection '{redir.op} {redir.target}' targets guarded path",
                    )

    # --- Ask tier ---

    ask_verbs = config.get("ask_verbs") or []
    if isinstance(ask_verbs, list) and leaf.verb in ask_verbs:
        return "ask", f"verb '{leaf.verb}' needs confirmation"

    ask_subcommands = config.get("ask_subcommands") or {}
    if isinstance(ask_subcommands, dict):
        subs = ask_subcommands.get(leaf.verb) or []
        if (
            isinstance(subs, list)
            and leaf.subcommand is not None
            and leaf.subcommand in subs
        ):
            return (
                "ask",
                f"subcommand '{leaf.verb} {leaf.subcommand}' needs confirmation",
            )

    ask_flag_patterns = config.get("ask_flag_patterns") or {}
    if isinstance(ask_flag_patterns, dict):
        patterns = ask_flag_patterns.get(leaf.verb) or []
        if isinstance(patterns, list):
            for pattern in patterns:
                if pattern in leaf.flags:
                    return (
                        "ask",
                        f"flag '{pattern}' on '{leaf.verb}' needs confirmation",
                    )

    # Procedural: truncate over an existing file — user can confirm.
    for redir in leaf.redirections:
        if redir.op == ">":
            try:
                if os.path.isfile(redir.target):
                    return (
                        "ask",
                        f"redirection '> {redir.target}' would overwrite existing file",
                    )
            except OSError:
                return (
                    "ask",
                    f"redirection '> {redir.target}' could not be stat'd",
                )

    return None, None


def is_denied(leaf: CanonicalLeaf) -> tuple[bool, str | None]:
    """Backward-compat wrapper: True iff ``evaluate()`` returns ``"deny"``.

    Ask-tier matches return ``(False, None)`` — callers that only care about
    hard blocks (e.g. the learner's candidate filter) keep their old semantics.
    """
    decision, reason = evaluate(leaf)
    if decision == "deny":
        return True, reason
    return False, None
