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


def _lookup_rule_shape_id(
    conn: sqlite3.Connection, variant: PatternVariant
) -> int | None:
    """Return the rule_shapes.id for the best matching rule, or None.

    The ``context`` filter uses ``IN ('any', ?)`` so that rules with
    ``context='any'`` match every leaf regardless of context, while
    ``context='toplevel'`` / ``context='substitution'`` only match the
    corresponding leaf context.

    For the flags-wildcard lookup variant (``variant.flags == '*'``), only
    rules stored with ``flags='*'`` are considered — this is the step-4
    fallback from ``to_pattern_form`` and preserves the original semantics.

    For all other variants, the ``flags`` constraint uses **allowlist
    (subset) semantics**: a rule matches when the actual flag set is a
    subset of the rule's stored flag set.  Among matching rules the most
    specific one wins (fewest extra flags beyond the actual set); wildcard
    rules (``flags='*'``) are used only when no specific rule matches.
    """
    if variant.flags == "*":
        # Wildcard lookup variant: find only wildcard-stored rules.
        row = conn.execute(
            "SELECT id FROM rule_shapes"
            " WHERE verb = ?"
            "   AND IFNULL(subcommand, '') = IFNULL(?, '')"
            "   AND flags = '*'"
            "   AND IFNULL(path_spec, '') = IFNULL(?, '')"
            "   AND context IN ('any', ?);",
            (variant.verb, variant.subcommand, variant.path_spec, variant.context),
        ).fetchone()
        return int(row[0]) if row is not None else None

    rows = conn.execute(
        "SELECT id, flags FROM rule_shapes"
        " WHERE verb = ?"
        "   AND IFNULL(subcommand, '') = IFNULL(?, '')"
        "   AND IFNULL(path_spec, '') = IFNULL(?, '')"
        "   AND context IN ('any', ?);",
        (variant.verb, variant.subcommand, variant.path_spec, variant.context),
    ).fetchall()

    actual: set[str] = set(json.loads(variant.flags))
    best_id: int | None = None
    best_extra: int = 0x7FFF_FFFF
    wildcard_id: int | None = None

    for row_id, rule_flags in rows:
        if rule_flags == "*":
            wildcard_id = int(row_id)
            continue
        rule_set: set[str] = set(json.loads(rule_flags))
        if not actual.issubset(rule_set):
            continue
        extra = len(rule_set) - len(actual)
        if extra < best_extra:
            best_id = int(row_id)
            best_extra = extra

    return best_id if best_id is not None else wildcard_id


def _decision_for_leaf(
    conn: sqlite3.Connection,
    leaf: CanonicalLeaf,
    ctx: dict[str, str],
    session_id: int | None,
    project_id: int | None,
    additional_dirs: list[str] | None = None,
    trusted_dirs: list[str] | None = None,
) -> tuple[str | None, int | None]:
    """Return (decision, permission_id) for *leaf*, or (None, None)."""
    from nephoscope.lib.db import lookup_permissions  # type: ignore[import-untyped]

    for variant in to_pattern_form(leaf, ctx, additional_dirs, trusted_dirs):
        shape_id = _lookup_rule_shape_id(conn, variant)
        if shape_id is None:
            continue
        rows = lookup_permissions(conn, shape_id, session_id, project_id)
        if rows:
            return rows[0]["decision"], rows[0]["id"]  # first = highest-priority tier
    return None, None


def match(
    tool_name: str,
    tool_input: dict[str, Any],
    conn: sqlite3.Connection,
    session_id: int | None,
    project_id: int | None,
    ctx: dict[str, str],
    additional_dirs: list[str] | None = None,
    trusted_dirs: list[str] | None = None,
) -> tuple[Verdict, int | None]:
    """Match a Bash tool invocation against the permissions DB.

    Returns ``(Verdict, permission_id)`` where ``permission_id`` is the
    ``permissions.id`` of the decisive row (the first Deny row or the last
    Allow row), or ``None`` when no row was matched.

    ``tool_name`` must be ``"Bash"`` (or the internal shell verb); callers
    should already have classified the tool before routing here.

    ``additional_dirs`` is the merged list of ``permissions.additionalDirectories``
    entries (global + project) for the current session, sourced from the
    mtime-gated DB cache in ``scope.get_additional_dirs``.

    ``trusted_dirs`` is the list of configured trusted directories from
    ``get_config().trusted_dirs``.  Positional paths under these directories
    are emitted with the ``$TRUSTED_DIR/**`` / ``$TRUSTED_DIR/<tail>``
    placeholder forms so that a single seeded rule covers any trusted dir.
    """
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return Verdict.NoOpinion, None

    leaves = parse_command(command)
    if not leaves:
        return Verdict.NoOpinion, None

    # Resolve permissions for each leaf.
    leaf_results: list[tuple[str | None, int | None]] = [
        _decision_for_leaf(
            conn, leaf, ctx, session_id, project_id, additional_dirs, trusted_dirs
        )
        for leaf in leaves
    ]
    leaf_decisions = [d for d, _ in leaf_results]

    # Any rejected leaf → Deny.  Return the first reject's perm_id.
    for decision, perm_id in leaf_results:
        if decision == "rejected":
            return Verdict.Deny, perm_id

    # All approved → Allow.  Return the last approved perm_id.
    if all(d == "approved" for d in leaf_decisions):
        last_perm_id = next(
            (perm_id for _, perm_id in reversed(leaf_results) if perm_id is not None),
            None,
        )
        return Verdict.Allow, last_perm_id

    # Unresolved leaves: check ask tier.
    for leaf, (decision, _perm_id) in zip(leaves, leaf_results):
        if decision is not None:
            continue  # already approved — no ask needed
        outcome, _reason = evaluate(leaf)
        if outcome == "ask":
            return Verdict.Ask, None

    return Verdict.NoOpinion, None
