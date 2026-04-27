"""Bash tool-class matcher.

Reuses the ``canonicalize`` + ``to_pattern_form`` helpers to look up
``permissions`` rows for each leaf command in the Bash payload.

Returns
-------
Verdict.Allow       — all leaves have an ``approved`` permission row.
Verdict.Deny        — at least one leaf has a ``rejected`` permission row.
Verdict.Ask         — at least one leaf has no DB match but triggers an
                      ask-tier rule in ``deny.py``.
Verdict.NoOpinion   — empty command, unparseable input, or no DB data to
                      base a decision on.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from nephoscope.learners.permission.match._types import Verdict  # type: ignore[import-untyped]
from nephoscope.learners.permission.canonicalize import (  # type: ignore[import-untyped]
    CanonicalLeaf,
    PatternVariant,
    parse_command,
    to_pattern_form,
)
from nephoscope.learners.permission.deny import evaluate  # type: ignore[import-untyped]


def _flags_key(flags: frozenset[str]) -> str:
    return json.dumps(sorted(flags), ensure_ascii=False, separators=(",", ":"))


def _lookup_rule_shape_id(
    conn: sqlite3.Connection, variant: PatternVariant
) -> int | None:
    """Return the rule_shapes.id for a matching rule, or None.

    The ``context`` filter uses ``IN ('any', ?)`` so that rules with
    ``context='any'`` match every leaf (regardless of whether the leaf is
    top-level or inside a substitution), while rules with
    ``context='toplevel'`` or ``context='substitution'`` only match the
    corresponding leaf context.
    """
    row = conn.execute(
        "SELECT id FROM rule_shapes"
        " WHERE verb = ?"
        "   AND IFNULL(subcommand, '') = IFNULL(?, '')"
        "   AND flags = ?"
        "   AND IFNULL(path_spec, '') = IFNULL(?, '')"
        "   AND context IN ('any', ?);",
        (
            variant.verb,
            variant.subcommand,
            variant.flags,
            variant.path_spec,
            variant.context,
        ),
    ).fetchone()
    return int(row[0]) if row is not None else None


def _decision_for_leaf(
    conn: sqlite3.Connection,
    leaf: CanonicalLeaf,
    ctx: dict[str, str],
    session_id: int | None,
    project_id: int | None,
    additional_dirs: list[str] | None = None,
) -> str | None:
    """Return the first permissions decision for *leaf*, or None."""
    from nephoscope.lib.db import lookup_permissions  # type: ignore[import-untyped]

    for variant in to_pattern_form(leaf, ctx, additional_dirs):
        shape_id = _lookup_rule_shape_id(conn, variant)
        if shape_id is None:
            continue
        rows = lookup_permissions(conn, shape_id, session_id, project_id)
        if rows:
            return rows[0]["decision"]  # first = highest-priority tier
    return None


def match(
    tool_name: str,
    tool_input: dict[str, Any],
    conn: sqlite3.Connection,
    session_id: int | None,
    project_id: int | None,
    ctx: dict[str, str],
    additional_dirs: list[str] | None = None,
) -> Verdict:
    """Match a Bash tool invocation against the permissions DB.

    ``tool_name`` must be ``"Bash"`` (or the internal shell verb); callers
    should already have classified the tool before routing here.

    ``additional_dirs`` is the merged list of ``permissions.additionalDirectories``
    entries (global + project) for the current session, sourced from the
    mtime-gated DB cache in ``scope.get_additional_dirs``.
    """
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return Verdict.NoOpinion

    leaves = parse_command(command)
    if not leaves:
        return Verdict.NoOpinion

    # Resolve permissions for each leaf.
    leaf_decisions: list[str | None] = [
        _decision_for_leaf(conn, leaf, ctx, session_id, project_id, additional_dirs)
        for leaf in leaves
    ]

    # Any rejected leaf → Deny.
    if any(d == "rejected" for d in leaf_decisions):
        return Verdict.Deny

    # All approved → Allow.
    if all(d == "approved" for d in leaf_decisions):
        return Verdict.Allow

    # Unresolved leaves: check ask tier.
    for leaf, decision in zip(leaves, leaf_decisions):
        if decision is not None:
            continue  # already approved — no ask needed
        outcome, _reason = evaluate(leaf)
        if outcome == "ask":
            return Verdict.Ask

    return Verdict.NoOpinion
