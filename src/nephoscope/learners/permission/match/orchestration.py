"""Orchestration tool-class matcher.

Orchestration tools (Agent, TaskCreate, SendMessage, …) are always
default-allow and never appear in the JSON mirror's deny/ask lists.  When
``HOOK_FULL_MATCH`` is enabled, this matcher returns ``Verdict.Allow``
unconditionally — no DB lookup is needed or performed.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from nephoscope.learners.permission.match._types import Verdict  # type: ignore[import-untyped]


def match(
    tool_name: str,
    tool_input: dict[str, Any],
    conn: sqlite3.Connection,
    session_id: int | None,
    project_id: int | None,
    ctx: dict[str, str],
    additional_dirs: list[str] | None = None,  # noqa: ARG001 — unused; Bash-only feature
) -> Verdict:
    """Return :attr:`Verdict.Allow` unconditionally — orchestration tools are
    default-allow."""
    return Verdict.Allow
