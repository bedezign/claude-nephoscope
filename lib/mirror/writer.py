"""Atomic JSON mirror writer: DB permissions → settings.json / settings.local.json.

Public API
----------
sync_global(conn)            Rebuild ~/.claude/settings.json from global rows.
sync_project(conn, pid)      Rebuild <project>/.claude/settings.local.json.
sync_affected(conn, perm_id) Dispatch to global or project sync.
MirrorHashMismatch           Raised when on-disk hash ≠ stored DB hash.
cleanup_stale_tmp(dir, age)  Remove .tmp siblings older than age seconds.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class MirrorHashMismatch(Exception):
    """On-disk hash differs from the hash stored in the DB.

    The message contains the file path and the first 8 characters of both
    hashes so a human can diagnose out-of-band edits.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    """UTC timestamp in ISO-8601 millisecond precision, Z-suffixed."""
    return (
        _dt.datetime.now(tz=_dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _read_global_meta(conn: sqlite3.Connection) -> tuple[Path, str | None]:
    """Return (target_path, stored_hash) for the global mirror singleton.

    Raises RuntimeError if the global_mirror singleton row is missing.
    """
    row = conn.execute(
        "SELECT settings_json_path, settings_json_sha256"
        " FROM global_mirror WHERE id = 1;"
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "global_mirror singleton (id=1) is missing — run setup to seed it"
        )
    return Path(row[0]).expanduser(), row[1]


def _read_project_meta(
    conn: sqlite3.Connection, project_id: int
) -> tuple[Path, str | None]:
    """Return (target_path, stored_hash) for a project row.

    Raises ValueError if the project is unknown or has no settings_json_path.
    """
    row = conn.execute(
        "SELECT settings_json_path, settings_json_sha256 FROM projects WHERE id = ?;",
        (project_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"project {project_id} not found in DB")
    if row[0] is None:
        raise ValueError(
            f"project {project_id} has no settings_json_path — set it before syncing"
        )
    return Path(row[0]).expanduser(), row[1]


def _read_stored_hash(conn: sqlite3.Connection, project_id: int | None) -> str | None:
    """Re-read the stored hash from DB inside the flock (picks up concurrent updates)."""
    if project_id is None:
        row = conn.execute(
            "SELECT settings_json_sha256 FROM global_mirror WHERE id = 1;"
        ).fetchone()
        return row[0] if row else None
    row = conn.execute(
        "SELECT settings_json_sha256 FROM projects WHERE id = ?;",
        (project_id,),
    ).fetchone()
    return row[0] if row else None


def _stamp_hash(
    conn: sqlite3.Connection, project_id: int | None, new_hash: str, now: str
) -> None:
    """Persist the new hash and last_synced timestamp to the DB."""
    if project_id is None:
        conn.execute(
            "UPDATE global_mirror"
            " SET settings_json_sha256 = ?, settings_json_last_synced = ?"
            " WHERE id = 1;",
            (new_hash, now),
        )
    else:
        conn.execute(
            "UPDATE projects"
            " SET settings_json_sha256 = ?, settings_json_last_synced = ?"
            " WHERE id = ?;",
            (new_hash, now, project_id),
        )


def _build_content(
    conn: sqlite3.Connection,
    project_id: int | None,
    target: Path | None = None,
) -> bytes:
    """Query permission rows and render them into JSON mirror bytes.

    Calls ``serializer.serialize(row)`` for each row; rows that return None
    (orchestration rules, default-allow, never written to JSON) are skipped.
    Decisions map as: approved → allow, rejected → deny, ask → ask.

    Read-merge-write: if *target* exists its contents are parsed and the new
    ``allow``/``deny``/``ask`` lists are merged in.  All other top-level keys
    (``attribution``, ``model``, ``hooks``, ``tui``, …) and any other keys
    inside ``permissions`` (e.g. ``defaultMode``) are left untouched.

    Raises ``ValueError`` when *target* exists but cannot be parsed as JSON —
    we never silently overwrite a file we cannot understand.
    """
    from lib.mirror import serializer  # late import: serializer lives in same package

    if project_id is None:
        rows = conn.execute(
            """
            SELECT p.id, p.decision, p.source, p.reason, p.decided_at,
                   rs.verb, rs.subcommand, rs.flags, rs.path_spec,
                   p.session_id, p.project_id
              FROM permissions p
              JOIN rule_shapes rs ON rs.id = p.rule_shape_id
             WHERE p.project_id IS NULL AND p.session_id IS NULL
             ORDER BY p.decided_at ASC, p.id ASC;
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT p.id, p.decision, p.source, p.reason, p.decided_at,
                   rs.verb, rs.subcommand, rs.flags, rs.path_spec,
                   p.session_id, p.project_id
              FROM permissions p
              JOIN rule_shapes rs ON rs.id = p.rule_shape_id
             WHERE p.project_id = ? AND p.session_id IS NULL
             ORDER BY p.decided_at ASC, p.id ASC;
            """,
            (project_id,),
        ).fetchall()

    allow: list[str] = []
    deny: list[str] = []
    ask: list[str] = []

    for r in rows:
        row_dict = {
            "id": r[0],
            "decision": r[1],
            "source": r[2],
            "reason": r[3],
            "decided_at": r[4],
            "verb": r[5],
            "subcommand": r[6],
            "flags": r[7],
            "path_spec": r[8],
            "session_id": r[9],
            "project_id": r[10],
        }
        canonical = serializer.serialize(row_dict)
        if canonical is None:
            continue  # orchestration row — default-allow, never written to JSON

        decision = row_dict["decision"]
        if decision == "approved":
            allow.append(canonical)
        elif decision == "rejected":
            deny.append(canonical)
        elif decision == "ask":
            ask.append(canonical)

    # Read-merge-write: load existing file to preserve foreign top-level keys
    # and any permissions sub-keys we don't own (e.g. defaultMode).
    if target is not None and target.exists():
        raw = target.read_bytes()
        try:
            existing: dict = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{target}: cannot parse existing JSON — {exc}") from exc
    else:
        existing = {}

    existing.setdefault("permissions", {})
    existing["permissions"]["allow"] = allow
    existing["permissions"]["deny"] = deny
    existing["permissions"]["ask"] = ask

    return json.dumps(existing, indent=2).encode("utf-8")


def _atomic_write(
    conn: sqlite3.Connection,
    target: Path,
    project_id: int | None,
    max_retries: int = 3,
) -> None:
    """Core atomic write: flock → hash-check → build → tmp/fsync/rename → stamp.

    The lock file is a sibling `<target>.lock`; the temp file is `<target>.tmp`.
    Both are on the same filesystem as the target, guaranteeing that the POSIX
    rename is atomic.

    Hash-check semantics
    --------------------
    - stored hash IS NULL  → first-touch; proceed without checking.
    - target does not exist → create-from-empty; proceed without checking.
    - stored hash matches on-disk hash → proceed.
    - stored hash differs from on-disk hash → hash mismatch.

    On mismatch the stored hash is re-read from DB and re-verified up to
    ``max_retries`` times (catches the race window where another process
    updated both the file and the DB hash between our read and the flock).
    After all retries are exhausted, MirrorHashMismatch is raised.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / (target.name + ".lock")
    tmp_path = target.parent / (target.name + ".tmp")

    lock_fd = open(lock_path, "w")  # noqa: WPS515 — kept open for the lock lifetime
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        for attempt in range(max_retries):
            # Re-read stored hash on every attempt (picks up concurrent DB updates).
            stored_hash = _read_stored_hash(conn, project_id)

            # Hash check: skip when stored hash is NULL (first-touch) or file absent.
            if stored_hash is not None and target.exists():
                on_disk_hash = _sha256_file(target)
                if on_disk_hash != stored_hash:
                    if attempt < max_retries - 1:
                        # Brief pause before retry — another writer may have
                        # updated both the file and the DB hash.
                        time.sleep(0.005 * (attempt + 1))
                        continue
                    raise MirrorHashMismatch(
                        f"{target}: on-disk hash {on_disk_hash[:8]!r}"
                        f" ≠ stored hash {stored_hash[:8]!r}"
                    )

            # Build JSON content from DB (read-merge-write preserves foreign keys).
            content = _build_content(conn, project_id, target)

            # Write .tmp → fsync → rename (atomic on POSIX).
            with open(tmp_path, "wb") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())

            os.rename(tmp_path, target)

            # Re-hash the written content and persist to DB.
            new_hash = _sha256_bytes(content)
            _stamp_hash(conn, project_id, new_hash, _now())
            return  # success — release flock in finally

        # Unreachable: the loop always either returns or raises inside.
        raise MirrorHashMismatch(  # pragma: no cover
            f"{target}: hash mismatch persisted after {max_retries} retries"
        )
    finally:
        lock_fd.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sync_global(conn: sqlite3.Connection) -> None:
    """Rebuild the global mirror file from all permissions rows where project_id IS NULL.

    Reads the target path from ``global_mirror.settings_json_path``.
    Raises MirrorHashMismatch when the on-disk file was edited externally.
    Raises RuntimeError when the global_mirror singleton row is absent.
    """
    target, _ = _read_global_meta(conn)
    _atomic_write(conn, target, project_id=None)


def sync_project(conn: sqlite3.Connection, project_id: int) -> None:
    """Rebuild a project's settings.local.json from its permission rows.

    Reads the target path from ``projects.settings_json_path``.
    Raises MirrorHashMismatch when the on-disk file was edited externally.
    Raises ValueError when the project is unknown or has no path configured.
    """
    target, _ = _read_project_meta(conn, project_id)
    _atomic_write(conn, target, project_id=project_id)


def sync_affected(conn: sqlite3.Connection, permission_id: int) -> None:
    """Dispatch to the correct sync based on the permission row's project_id.

    If ``permissions.project_id IS NULL`` → global sync.
    Otherwise                             → project sync for that project_id.

    Raises ValueError when the permission_id does not exist.
    """
    row = conn.execute(
        "SELECT project_id FROM permissions WHERE id = ?;",
        (permission_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"permission {permission_id} not found")
    project_id: int | None = row[0]
    if project_id is None:
        sync_global(conn)
    else:
        sync_project(conn, project_id)


def cleanup_stale_tmp(parent_dir: Path, max_age_seconds: int = 300) -> None:
    """Remove .tmp files in parent_dir that are older than max_age_seconds.

    Silently skips files that disappear between the glob and the unlink
    (another process may have claimed them).  Only considers files whose
    names end with ``.tmp`` — does not recurse into subdirectories.
    """
    cutoff = time.time() - max_age_seconds
    for tmp_file in parent_dir.glob("*.tmp"):
        try:
            if tmp_file.stat().st_mtime < cutoff:
                tmp_file.unlink()
        except OSError:
            pass  # already removed or no permission — not our concern
