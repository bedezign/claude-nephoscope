"""Fixture seeding and export for permission rules.

This module manages the round-trip of permission rules between YAML fixtures
and the database. It supports loading fixtures (applying them to the DB as
rule_shapes + permissions rows) and exporting the current permissions state
back to YAML.

Fixture YAML schema:

    - verb: str (required, or "*" wildcard)
      subcommand: str? (optional)
      flags: list[str] | "*" (required)
      path_spec: str? (optional, one of: NULL, "", "$VAR/**", "$VAR/<tail>", "$VAR/**/<filename>")
      tier: str (optional, default "global", one of: "session", "project", "global")
      decision: str (required, one of: "approved", "rejected")
      reason: str? (optional)
      context: str? (optional, default "any", one of: "any", "toplevel", "substitution")
      tool: str? (optional, default "Bash", one of: "Bash", "Read", "Write", "Edit")

Round-trip idempotency: applying a fixture to an empty DB and exporting should
yield equivalent YAML (field order may differ, but content is identical).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import yaml

import sys

from nephoscope.learners.permission.evaluate import evaluate
from nephoscope.lib.db import _now, insert_permission, minify_json, upsert_rule_shape
from nephoscope.lib.mirror.writer import MirrorHashMismatch, sync_global


def _yaml_to_flags(flags: Any) -> str:
    """Convert flags from YAML (list or "*" string) to minified JSON.

    Args:
        flags: either a list of strings (e.g., ["-q", "--verbose"]) or
               the sentinel string "*" (wildcard)

    Returns: minified JSON array string or "*"
    """
    if isinstance(flags, str) and flags == "*":
        return "*"
    if isinstance(flags, list):
        from nephoscope.learners.permission.canonicalize import normalize_flags

        return minify_json(normalize_flags(flags))
    raise ValueError(f"invalid flags: {flags!r}")


_VALID_CONTEXTS: frozenset[str] = frozenset({"any", "toplevel", "substitution"})
_VALID_TOOLS: frozenset[str] = frozenset({"Bash", "Read", "Write", "Edit"})
_VALID_VERB_CATEGORIES: frozenset[str] = frozenset(
    {"task_runner", "two_word_subcommand", "content_verb", "script_runner"}
)


def _validate_entry(
    idx: int, entry: Any
) -> tuple[
    str, str, Any, str | None, str | None, str, str | None, str, str, str | None
]:
    """Validate a single fixture entry and return its fields.

    Returns (verb, decision, flags_raw, subcommand, path_spec, tier, reason,
    context, tool, danger_accepted).
    Raises ValueError on invalid schema.
    """
    if not isinstance(entry, dict):
        raise ValueError(f"entry {idx} is not a dict: {entry!r}")

    verb = entry.get("verb")
    decision = entry.get("decision")
    flags_raw = entry.get("flags")

    if not verb:
        raise ValueError(f"entry {idx} missing 'verb'")
    if not decision:
        raise ValueError(f"entry {idx} missing 'decision'")
    if flags_raw is None:
        raise ValueError(f"entry {idx} missing 'flags'")
    if decision not in ("approved", "rejected"):
        raise ValueError(f"entry {idx} invalid decision: {decision!r}")

    subcommand = entry.get("subcommand")
    path_spec = entry.get("path_spec")
    tier = entry.get("tier", "global")
    reason = entry.get("reason")
    context = entry.get("context", "any")
    tool = entry.get("tool", "Bash")
    danger_accepted: str | None = entry.get("danger_accepted")

    if tier not in ("session", "project", "global"):
        raise ValueError(f"entry {idx} invalid tier: {tier!r}")

    if context not in _VALID_CONTEXTS:
        raise ValueError(
            f"entry {idx} invalid context: {context!r} "
            f"(must be one of: {sorted(_VALID_CONTEXTS)})"
        )

    if tool not in _VALID_TOOLS:
        raise ValueError(
            f"entry {idx} invalid tool: {tool!r} "
            f"(must be one of: {sorted(_VALID_TOOLS)})"
        )

    return (
        verb,
        decision,
        flags_raw,
        subcommand,
        path_spec,
        tier,
        reason,
        context,
        tool,
        danger_accepted,
    )


def _apply_entry(
    conn: sqlite3.Connection,
    idx: int,
    verb: str,
    decision: str,
    flags_raw: Any,
    subcommand: str | None,
    path_spec: str | None,
    tier: str,
    reason: str | None,
    now: str,
    context: str = "any",
    tool: str = "Bash",
    danger_accepted: str | None = None,
) -> None:
    """Upsert one rule_shape + permission row for a fixture entry."""
    if tier != "global":
        raise NotImplementedError(
            f"Seed fixtures are currently global-tier only; "
            f"entry {idx} requested {tier!r}"
        )

    flags_json = _yaml_to_flags(flags_raw)
    findings = evaluate(verb, flags_json, subcommand, path_spec)
    danger_codes = {f.code for f in findings if f.severity == "DANGER"}

    if danger_codes:
        if danger_accepted not in danger_codes:
            for f in findings:
                if f.severity == "DANGER":
                    print(f"DANGER [{f.code}]: {f.message}", file=sys.stderr)
                    print(f"See: {f.guide_anchor}", file=sys.stderr)
            n = len(danger_codes)
            print(
                f"Skipping entry {idx} (verb={verb!r}): {n} DANGER finding(s). "
                f"Set danger_accepted: <code> in the fixture to override.",
                file=sys.stderr,
            )
            return
        print(
            f"Accepted DANGER [{danger_accepted}] for verb={verb!r} — writing anyway.",
            file=sys.stderr,
        )

    for f in findings:
        if f.severity == "WARN":
            print(f"WARN [{f.code}]: {f.message}", file=sys.stderr)

    shape_id = upsert_rule_shape(
        conn,
        verb=verb,
        subcommand=subcommand,
        flags_json=flags_json,
        path_spec=path_spec,
        ts=now,
        context=context,
        tool=tool,
    )
    insert_permission(
        conn,
        rule_shape_id=shape_id,
        session_id=None,
        project_id=None,
        decision=decision,
        source="seed",
        ts=now,
        reason=reason,
        danger_accepted=danger_accepted,
    )


def _sync_global_mirror(conn: sqlite3.Connection) -> None:
    """Sync the global mirror, ignoring 'not configured' errors."""
    try:
        sync_global(conn)
    except MirrorHashMismatch as exc:
        path = str(exc).split(":")[0]
        raise ValueError(
            f"settings file at {path} was edited externally — "
            f"run '/nephoscope:permissions reconcile' and retry"
        ) from exc
    except RuntimeError:
        pass  # global_mirror singleton not configured — skip sync


def _apply_permission_list(
    conn: sqlite3.Connection,
    entries: list[Any],
    now: str,
) -> int:
    """Apply a list of raw permission dicts to the DB.

    Inner loop extracted from ``apply_fixtures`` so ``profiles.py`` can call it
    without going through a file path. Does NOT call ``_sync_global_mirror`` —
    the caller decides.

    Validates all entries first, then writes — so a bad entry never leaves the
    DB in a half-applied state.

    Each entry produces exactly one rule_shape upsert and one permission insert,
    so only a single count is returned.

    Returns: number of entries applied.
    Raises: ValueError on invalid schema.
    """
    validated: list[
        tuple[
            str, str, Any, str | None, str | None, str, str | None, str, str, str | None
        ]
    ] = []
    for idx, entry in enumerate(entries):
        validated.append(_validate_entry(idx, entry))

    for idx, (
        verb,
        decision,
        flags_raw,
        subcommand,
        path_spec,
        tier,
        reason,
        context,
        tool,
        danger_accepted,
    ) in enumerate(validated):
        _apply_entry(
            conn,
            idx,
            verb,
            decision,
            flags_raw,
            subcommand,
            path_spec,
            tier,
            reason,
            now,
            context=context or "any",
            tool=tool,
            danger_accepted=danger_accepted,
        )

    return len(validated)


def apply_fixtures(
    conn: sqlite3.Connection,
    fixture_path: str | Path,
) -> tuple[int, int]:
    """Load and apply fixtures from a YAML file.

    For each fixture entry:
    1. Upsert rule_shape with the given (verb, subcommand, flags, path_spec)
    2. Insert a permissions row with decision/source='seed'/reason

    Session-tier rows are written to DB only (no JSON analogue).
    Global and project-tier rows are additionally synced to their JSON mirror
    via ``lib.mirror.writer``.

    Returns: (rule_shapes_count, permissions_count) inserted or upserted.

    Args:
        conn: SQLite connection
        fixture_path: path to YAML file

    Raises:
        ValueError: if fixture schema is invalid or a mirror hash mismatch occurs.
    """
    fixture_path = Path(fixture_path)
    content = fixture_path.read_text(encoding="utf-8")
    entries = yaml.safe_load(content) or []

    if not isinstance(entries, list):
        raise ValueError(f"fixture must be a YAML list, got {type(entries).__name__}")

    entries_count = _apply_permission_list(conn, entries, _now())

    if entries:
        _sync_global_mirror(conn)

    return entries_count, entries_count


def _build_entry(row: tuple[Any, ...]) -> dict[str, Any]:
    """Convert a v_permissions row to a YAML-ready dict."""
    verb, subcommand, flags_str, path_spec, context, tier, decision, reason = row

    flags: Any = "*" if flags_str == "*" else json.loads(flags_str)
    entry: dict[str, Any] = {"verb": verb, "flags": flags, "decision": decision}

    if subcommand is not None:
        entry["subcommand"] = subcommand
    if path_spec is not None:
        entry["path_spec"] = path_spec
    # Omit context when it is 'any' (the default) to keep YAML lean.
    if context is not None and context != "any":
        entry["context"] = context
    if tier != "global":
        entry["tier"] = tier
    if reason is not None:
        entry["reason"] = reason

    return entry


def export_permissions(
    conn: sqlite3.Connection,
    output_path: str | Path | None = None,
) -> str:
    """Export current permissions from v_permissions view to YAML.

    Returns the YAML string. If output_path is provided, also writes to that file.

    Args:
        conn: SQLite connection
        output_path: optional path to write the YAML file to

    Returns: YAML string suitable for round-trip via apply_fixtures
    """
    rows = conn.execute(
        """
        SELECT verb, subcommand, flags, path_spec, context, tier, decision, reason
          FROM v_permissions
         ORDER BY tier, decision, verb, COALESCE(subcommand, ''), flags, COALESCE(path_spec, '')
        """
    ).fetchall()

    entries = [_build_entry(row) for row in rows]
    yaml_str = yaml.dump(entries, default_flow_style=False, sort_keys=False)

    if output_path is not None:
        Path(output_path).write_text(yaml_str, encoding="utf-8")

    return yaml_str


def _apply_verb_type_list(
    conn: sqlite3.Connection,
    entries: list[Any],
) -> int:
    """Apply a list of raw verb-type dicts to the DB.

    Inner loop extracted from ``apply_verb_types`` so ``profiles.py`` can call it
    without going through a file path. Validates all entries before touching the DB
    to prevent partial writes on error.

    Returns: len(entries).
    Raises: ValueError on invalid entry shape or unknown category.
    """
    validated: list[tuple[str, str, str | None]] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"entry {idx} is not a dict: {entry!r}")
        verb = entry.get("verb")
        category = entry.get("category")
        if not verb:
            raise ValueError(f"entry {idx} missing verb")
        if not category:
            raise ValueError(f"entry {idx} missing category")
        if category not in _VALID_VERB_CATEGORIES:
            raise ValueError(f"entry {idx} invalid category {category!r}")
        validated.append((verb, category, entry.get("second_word")))
    for verb, category, second_word in validated:
        conn.execute(
            "INSERT OR IGNORE INTO verb_categories (verb, category, second_word)"
            " VALUES (?, ?, ?);",
            (verb, category, second_word),
        )
    return len(entries)


def apply_verb_types(
    conn: sqlite3.Connection,
    fixture_path: str | Path,
) -> int:
    """Load verb category entries from a YAML profile file and insert into verb_categories.

    Each entry must have: verb (str), category (str). second_word (str) is optional.
    Existing rows are left unchanged (INSERT OR IGNORE). Returns the number of rows
    in the fixture (not the number actually inserted — duplicates are silently skipped).

    Does NOT commit — the caller is responsible, matching apply_fixtures convention.

    Raises ValueError on invalid entry shape or unknown category.
    """
    path = Path(fixture_path)
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(entries, list):
        raise ValueError(
            f"verb_types fixture must be a YAML list, got {type(entries).__name__}"
        )
    return _apply_verb_type_list(conn, entries)
