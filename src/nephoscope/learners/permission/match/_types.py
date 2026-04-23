"""Verdict enum — the return type of every per-tool-class matcher."""

from __future__ import annotations

from enum import Enum, auto


class Verdict(Enum):
    """Hook decision for a single tool invocation.

    Allow       — DB has an approved permission for this tool call; emit
                  ``permissionDecision=allow``.
    Deny        — DB has a rejected permission (or procedural deny fires);
                  emit ``permissionDecision=deny``.
    Ask         — No DB approval but an ask-tier rule applies; emit
                  ``permissionDecision=ask``.
    NoOpinion   — The matcher has no data to base a decision on; hook emits
                  ``{}`` and Claude Code's native gate takes over.
    """

    Allow = auto()
    Deny = auto()
    Ask = auto()
    NoOpinion = auto()
