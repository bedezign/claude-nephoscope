"""Fixture seeding and export for permission rules.

This module manages the round-trip of permission rules between YAML fixtures
and the database. It supports loading fixtures (applying them to the DB as
rule_shapes + permissions rows) and exporting the current permissions state
back to YAML.

Fixture YAML schema:

    - verb: str (required)
      subcommand: str? (optional)
      flags: list[str] | "*" (required)
      path_spec: str? (optional, one of: NULL, "", "$VAR/**", "$VAR/<tail>")
      tier: str (optional, default "global", one of: "session", "project", "global")
      decision: str (required, one of: "approved", "rejected")
      reason: str? (optional)

Round-trip idempotency: applying a fixture to an empty DB and exporting should
yield equivalent YAML (field order may differ, but content is identical).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from nephoscope.lib.db import _now, insert_permission, minify_json, upsert_rule_shape
from nephoscope.lib.mirror.writer import MirrorHashMismatch, sync_global, sync_project


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
        return minify_json(sorted(flags))
    raise ValueError(f"invalid flags: {flags!r}")


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

    now = _now()
    shapes_created = 0
    perms_created = 0

    # Track which mirrors need syncing after all DB inserts succeed.
    needs_global_sync = False
    project_ids_to_sync: set[int] = set()

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"entry {idx} is not a dict: {entry!r}")

        # Required fields
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

        # Optional fields
        subcommand = entry.get("subcommand")
        path_spec = entry.get("path_spec")
        tier = entry.get("tier", "global")
        reason = entry.get("reason")

        if tier not in ("session", "project", "global"):
            raise ValueError(f"entry {idx} invalid tier: {tier!r}")

        # Convert flags
        flags_json = _yaml_to_flags(flags_raw)

        # Upsert rule_shape
        shape_id = upsert_rule_shape(
            conn,
            verb=verb,
            subcommand=subcommand,
            flags_json=flags_json,
            path_spec=path_spec,
            ts=now,
        )
        shapes_created += 1

        # Determine session_id / project_id based on tier
        session_id: int | None = None
        project_id: int | None = None
        # For seed fixtures, we don't have actual session/project ids,
        # so they're only created as global tier
        if tier != "global":
            raise NotImplementedError(
                f"Seed fixtures are currently global-tier only; "
                f"entry {idx} requested {tier!r}"
            )

        # Insert permission row
        insert_permission(
            conn,
            rule_shape_id=shape_id,
            session_id=session_id,
            project_id=project_id,
            decision=decision,
            source="seed",
            ts=now,
            reason=reason,
        )
        perms_created += 1

        # Track which mirrors need syncing (session-tier: DB-only, no mirror).
        if session_id is None:
            if project_id is None:
                needs_global_sync = True
            else:
                project_ids_to_sync.add(project_id)

    # Sync mirrors for all affected scopes after DB inserts complete.
    # RuntimeError means the mirror singleton is not yet configured (no path
    # registered); treat as "mirror not set up" and skip silently so the seed
    # still works in environments without a configured mirror.
    if needs_global_sync:
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

    for pid in sorted(project_ids_to_sync):
        try:
            sync_project(conn, pid)
        except MirrorHashMismatch as exc:
            path = str(exc).split(":")[0]
            raise ValueError(
                f"settings file at {path} was edited externally — "
                f"run '/nephoscope:permissions reconcile' and retry"
            ) from exc
        except (RuntimeError, ValueError):
            pass  # project mirror not configured — skip sync

    return shapes_created, perms_created


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
        SELECT verb, subcommand, flags, path_spec, tier, decision, reason
          FROM v_permissions
         ORDER BY tier, decision, verb, COALESCE(subcommand, ''), flags, COALESCE(path_spec, '')
        """
    ).fetchall()

    entries = []
    for row in rows:
        verb, subcommand, flags_str, path_spec, tier, decision, reason = row

        # Reconstruct flags: "*" stays as string, JSON array becomes list
        if flags_str == "*":
            flags = "*"
        else:
            flags = json.loads(flags_str)

        entry: dict[str, Any] = {
            "verb": verb,
            "flags": flags,
            "decision": decision,
        }

        if subcommand is not None:
            entry["subcommand"] = subcommand
        if path_spec is not None:
            entry["path_spec"] = path_spec
        if tier != "global":
            entry["tier"] = tier
        if reason is not None:
            entry["reason"] = reason

        entries.append(entry)

    yaml_str = yaml.dump(entries, default_flow_style=False, sort_keys=False)

    if output_path is not None:
        Path(output_path).write_text(yaml_str, encoding="utf-8")

    return yaml_str
