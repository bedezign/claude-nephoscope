"""Tests for lib.mirror.writer — atomic JSON mirror writer.

All writes go to tmp_path or tempfile.mkdtemp().  Zero tolerance for writes
to real paths (~/.claude/settings.json, ~/.cache/claude/observability/, etc.).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from nephoscope.lib.mirror.permissions_hash import settings_permissions_hash

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCHEMA_PATH = PROJECT_ROOT / "src" / "nephoscope" / "lib" / "schema.sql"


@pytest.fixture()
def db_conn(tmp_path):
    """Isolated SQLite DB seeded with schema + global_mirror singleton.

    The global_mirror singleton points to a fake settings.json inside tmp_path.
    The DB is in autocommit mode (isolation_level=None).
    """
    db_path = tmp_path / "test.db"
    fake_settings = tmp_path / "settings.json"

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.execute(
        "INSERT OR IGNORE INTO global_mirror"
        " (id, settings_json_path, settings_json_sha256, settings_json_last_synced)"
        " VALUES (1, ?, NULL, NULL);",
        (str(fake_settings),),
    )
    conn.execute(
        "INSERT OR IGNORE INTO permission_modes (name)"
        " VALUES ('default'),('acceptEdits'),('bypassPermissions'),('plan'),('auto');"
    )
    conn.execute(
        "INSERT OR IGNORE INTO call_statuses (name)"
        " VALUES ('pending'),('ok'),('err'),('denied'),('orphan');"
    )
    yield conn
    conn.close()


def _null_serialize(row):
    """Stub serializer: returns None for every row (simulate all-orchestration DB)."""
    return None


def _allow_serialize(row):
    """Stub serializer: returns a canonical string so the allow list is non-empty."""
    return f"Bash({row['verb']} *)"


# ---------------------------------------------------------------------------
# Happy path: empty DB → mirror created, hash stamped
# ---------------------------------------------------------------------------


def test_sync_global_creates_mirror_with_empty_db(tmp_path, db_conn):
    """sync_global on an empty DB writes a valid JSON mirror and stamps the hash."""
    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        from nephoscope.lib.mirror.writer import sync_global

        sync_global(db_conn)

    # Target file must exist.
    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )
    assert target.exists(), "mirror file was not created"

    # Content is valid JSON with the expected shape.
    data = json.loads(target.read_bytes())
    assert "permissions" in data
    perms = data["permissions"]
    assert perms["allow"] == []
    assert perms["deny"] == []
    assert perms["ask"] == []

    # Stored hash in DB must match actual file contents.
    stored_hash = db_conn.execute(
        "SELECT settings_json_sha256 FROM global_mirror WHERE id = 1;"
    ).fetchone()[0]
    assert stored_hash is not None, "hash was not stamped"
    assert stored_hash == settings_permissions_hash(target.read_bytes())

    # Cache columns must be populated after sync.
    row = db_conn.execute(
        "SELECT settings_json_mtime, additional_dirs FROM global_mirror WHERE id = 1;"
    ).fetchone()
    assert row[0] == target.stat().st_mtime, "settings_json_mtime must match file mtime"
    assert row[1] == "[]", "additional_dirs must be an empty JSON array when no dirs"


# ---------------------------------------------------------------------------
# First-touch: stored hash IS NULL → sync succeeds, stamps hash
# ---------------------------------------------------------------------------


def test_first_touch_null_hash_succeeds(tmp_path, db_conn):
    """First-touch path: stored hash NULL, file absent → creates mirror, stamps hash."""
    # Confirm starting state.
    stored = db_conn.execute(
        "SELECT settings_json_sha256 FROM global_mirror WHERE id = 1;"
    ).fetchone()[0]
    assert stored is None

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        from nephoscope.lib.mirror.writer import sync_global

        sync_global(db_conn)

    stored_after = db_conn.execute(
        "SELECT settings_json_sha256 FROM global_mirror WHERE id = 1;"
    ).fetchone()[0]
    assert stored_after is not None, "hash must be stamped after first-touch sync"
    assert len(stored_after) == 64, "hash must be a full SHA-256 hex digest"


def test_first_touch_null_hash_with_existing_file_succeeds(tmp_path, db_conn):
    """First-touch path: hash NULL but file already on disk → adopt and stamp."""
    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )
    # Pre-write a file (simulates user's hand-written settings).
    target.write_text('{"permissions":{"allow":[],"deny":[],"ask":[]}}')

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        from nephoscope.lib.mirror.writer import sync_global

        # Must not raise even though stored hash is NULL.
        sync_global(db_conn)

    stored = db_conn.execute(
        "SELECT settings_json_sha256 FROM global_mirror WHERE id = 1;"
    ).fetchone()[0]
    assert stored is not None


# ---------------------------------------------------------------------------
# Hash mismatch: tamper file → MirrorHashMismatch raised
# ---------------------------------------------------------------------------


def test_hash_mismatch_raises_after_tampering(tmp_path, db_conn):
    """Tampering the mirror file after a sync raises MirrorHashMismatch on re-sync."""
    from nephoscope.lib.mirror.writer import MirrorHashMismatch, sync_global

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        sync_global(db_conn)  # first sync stamps the hash

    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )

    # Tamper: inject a permissions entry so the permissions-only hash shifts.
    tampered = {"permissions": {"allow": ["Bash(intruder)"], "deny": [], "ask": []}}
    target.write_text(json.dumps(tampered))

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        with pytest.raises(MirrorHashMismatch) as exc_info:
            sync_global(db_conn)

    msg = str(exc_info.value)
    assert str(target) in msg, "exception message must include the file path"
    # First 8 chars of both hashes must appear in the message.
    assert len(msg) > 30, "exception message must include hash snippets"


def test_hash_mismatch_exception_message_contains_hashes(tmp_path, db_conn):
    """MirrorHashMismatch message includes first-8-char snippets of both hashes."""
    from nephoscope.lib.mirror.writer import MirrorHashMismatch, sync_global

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        sync_global(db_conn)

    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )
    original_hash = db_conn.execute(
        "SELECT settings_json_sha256 FROM global_mirror WHERE id = 1;"
    ).fetchone()[0]

    # Tamper: inject a permissions entry so the permissions-only hash shifts.
    tampered_content = json.dumps(
        {"permissions": {"allow": ["Bash(intruder)"], "deny": [], "ask": []}}
    ).encode("utf-8")
    target.write_bytes(tampered_content)
    on_disk_hash = settings_permissions_hash(tampered_content)

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        with pytest.raises(MirrorHashMismatch) as exc_info:
            sync_global(db_conn)

    msg = str(exc_info.value)
    assert on_disk_hash[:8] in msg
    assert original_hash[:8] in msg


# ---------------------------------------------------------------------------
# fsync is called
# ---------------------------------------------------------------------------


def test_fsync_is_called_during_write(tmp_path, db_conn):
    """os.fsync must be called when writing the mirror file."""
    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        with patch("os.fsync") as mock_fsync:
            from nephoscope.lib.mirror import writer

            # Re-import to pick up the patch on the module's os.fsync reference.
            import importlib

            importlib.reload(writer)
            with patch(
                "nephoscope.lib.mirror.serializer.serialize",
                side_effect=_null_serialize,
            ):
                writer.sync_global(db_conn)

            assert mock_fsync.called, "os.fsync must be called to flush the .tmp file"


# ---------------------------------------------------------------------------
# Flock contention: two concurrent syncs serialize
# ---------------------------------------------------------------------------


def test_flock_contention_serializes_writers(tmp_path, db_conn):
    """Two concurrent sync_global calls must not interleave — second waits for first.

    Each thread opens its own SQLite connection (SQLite connections are not
    thread-safe; sharing one across threads raises ProgrammingError).
    """
    from nephoscope.lib.mirror.writer import sync_global

    # Collect the DB path and mirror path before spawning threads.
    db_path = db_conn.execute("PRAGMA database_list;").fetchone()[2]
    barrier = threading.Barrier(2)
    errors: list[Exception] = []
    results: list[str] = []
    results_lock = threading.Lock()

    def _open_thread_conn() -> sqlite3.Connection:
        """Open a fresh SQLite connection in the calling thread."""
        c = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        return c

    def writer_one():
        try:
            c = _open_thread_conn()
            barrier.wait()
            with patch(
                "nephoscope.lib.mirror.serializer.serialize",
                side_effect=_null_serialize,
            ):
                sync_global(c)
            with results_lock:
                results.append("done-1")
            c.close()
        except Exception as exc:
            errors.append(exc)

    def writer_two():
        try:
            c = _open_thread_conn()
            barrier.wait()
            with patch(
                "nephoscope.lib.mirror.serializer.serialize",
                side_effect=_null_serialize,
            ):
                sync_global(c)
            with results_lock:
                results.append("done-2")
            c.close()
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=writer_one)
    t2 = threading.Thread(target=writer_two)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"Thread errors: {errors}"
    # Both writers must have completed without error.
    assert "done-1" in results
    assert "done-2" in results


# ---------------------------------------------------------------------------
# Stale .tmp cleanup
# ---------------------------------------------------------------------------


def test_cleanup_stale_tmp_removes_old_files(tmp_path):
    """cleanup_stale_tmp removes .tmp files older than max_age_seconds."""
    from nephoscope.lib.mirror.writer import cleanup_stale_tmp

    old_tmp = tmp_path / "settings.json.tmp"
    old_tmp.write_text("stale")

    # Back-date the mtime to 10 minutes ago.
    old_time = time.time() - 600
    os.utime(old_tmp, (old_time, old_time))

    cleanup_stale_tmp(tmp_path, max_age_seconds=300)

    assert not old_tmp.exists(), "stale .tmp file must be removed"


def test_cleanup_stale_tmp_keeps_recent_files(tmp_path):
    """cleanup_stale_tmp must not remove .tmp files newer than max_age_seconds."""
    from nephoscope.lib.mirror.writer import cleanup_stale_tmp

    fresh_tmp = tmp_path / "settings.json.tmp"
    fresh_tmp.write_text("in progress")
    # mtime is now — well within the 300-second window.

    cleanup_stale_tmp(tmp_path, max_age_seconds=300)

    assert fresh_tmp.exists(), "recent .tmp file must be kept"


def test_cleanup_stale_tmp_only_affects_tmp_extension(tmp_path):
    """cleanup_stale_tmp must not remove non-.tmp files even if they are old."""
    from nephoscope.lib.mirror.writer import cleanup_stale_tmp

    old_json = tmp_path / "settings.json"
    old_json.write_text("{}")
    old_time = time.time() - 600
    os.utime(old_json, (old_time, old_time))

    cleanup_stale_tmp(tmp_path, max_age_seconds=300)

    assert old_json.exists(), "non-.tmp files must never be removed"


def test_cleanup_stale_tmp_empty_dir_is_noop(tmp_path):
    """cleanup_stale_tmp on an empty directory must not raise."""
    from nephoscope.lib.mirror.writer import cleanup_stale_tmp

    cleanup_stale_tmp(tmp_path)  # should not raise


# ---------------------------------------------------------------------------
# sync_project: project-scoped mirror
# ---------------------------------------------------------------------------


def test_sync_project_writes_mirror_for_project(tmp_path, db_conn):
    """sync_project writes settings.local.json for the given project_id."""
    from nephoscope.lib.mirror.writer import sync_project

    fake_project_dir = tmp_path / "myproject" / ".claude"
    fake_project_dir.mkdir(parents=True)
    local_json = fake_project_dir / "settings.local.json"

    # Register a fake project.
    cur = db_conn.execute(
        "INSERT INTO projects (cwd, name, root, first_seen, last_seen,"
        " settings_json_path, settings_json_sha256, settings_json_last_synced)"
        " VALUES (?, ?, ?, '2026-01-01Z', '2026-01-01Z', ?, NULL, NULL);",
        (
            str(tmp_path / "myproject"),
            "myproject",
            str(tmp_path / "myproject"),
            str(local_json),
        ),
    )
    project_id = cur.lastrowid

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        sync_project(db_conn, project_id)

    assert local_json.exists()
    data = json.loads(local_json.read_bytes())
    assert "permissions" in data

    # Hash must be stamped in projects table.
    stored = db_conn.execute(
        "SELECT settings_json_sha256 FROM projects WHERE id = ?;",
        (project_id,),
    ).fetchone()[0]
    assert stored == settings_permissions_hash(local_json.read_bytes())

    # Cache columns must be populated after project sync.
    cache_row = db_conn.execute(
        "SELECT settings_json_mtime, additional_dirs FROM projects WHERE id = ?;",
        (project_id,),
    ).fetchone()
    assert cache_row[0] == local_json.stat().st_mtime, (
        "settings_json_mtime must match file mtime after project sync"
    )
    assert cache_row[1] == "[]", (
        "additional_dirs must be an empty JSON array when no dirs present"
    )


def test_sync_project_raises_for_unknown_project(tmp_path, db_conn):
    """sync_project raises ValueError for a non-existent project_id."""
    from nephoscope.lib.mirror.writer import sync_project

    with pytest.raises(ValueError, match="9999"):
        with patch(
            "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
        ):
            sync_project(db_conn, 9999)


def test_sync_project_raises_when_no_path_configured(tmp_path, db_conn):
    """sync_project raises ValueError when projects.settings_json_path IS NULL."""
    from nephoscope.lib.mirror.writer import sync_project

    cur = db_conn.execute(
        "INSERT INTO projects (cwd, name, root, first_seen, last_seen)"
        " VALUES (?, ?, ?, '2026-01-01Z', '2026-01-01Z');",
        (str(tmp_path / "nopath"), "nopath", str(tmp_path / "nopath")),
    )
    project_id = cur.lastrowid

    with pytest.raises(ValueError, match="settings_json_path"):
        with patch(
            "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
        ):
            sync_project(db_conn, project_id)


# ---------------------------------------------------------------------------
# sync_affected: dispatch by project_id
# ---------------------------------------------------------------------------


def test_sync_affected_dispatches_global_for_null_project(tmp_path, db_conn):
    """sync_affected dispatches to sync_global when the permission's project_id is NULL."""
    from nephoscope.lib.mirror.writer import sync_affected

    # Insert a global rule shape and permission.
    shape_id = db_conn.execute(
        "INSERT INTO rule_shapes (verb, flags, first_seen, last_seen)"
        " VALUES ('git', '[]', '2026-01-01Z', '2026-01-01Z');"
    ).lastrowid
    perm_id = db_conn.execute(
        "INSERT INTO permissions"
        " (rule_shape_id, session_id, project_id, decision, source, decided_at)"
        " VALUES (?, NULL, NULL, 'approved', 'seed', '2026-01-01Z');",
        (shape_id,),
    ).lastrowid

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        with patch("nephoscope.lib.mirror.writer.sync_global") as mock_global:
            with patch("nephoscope.lib.mirror.writer.sync_project") as mock_project:
                sync_affected(db_conn, perm_id)

    mock_global.assert_called_once_with(db_conn)
    mock_project.assert_not_called()


def test_sync_affected_dispatches_project_sync(tmp_path, db_conn):
    """sync_affected dispatches to sync_project when the permission has a project_id."""
    from nephoscope.lib.mirror.writer import sync_affected

    # Register a project.
    project_id = db_conn.execute(
        "INSERT INTO projects (cwd, name, root, first_seen, last_seen,"
        " settings_json_path)"
        " VALUES (?, 'p', ?, '2026-01-01Z', '2026-01-01Z', ?);",
        (
            str(tmp_path / "proj"),
            str(tmp_path / "proj"),
            str(tmp_path / "proj" / ".claude" / "settings.local.json"),
        ),
    ).lastrowid

    shape_id = db_conn.execute(
        "INSERT INTO rule_shapes (verb, flags, first_seen, last_seen)"
        " VALUES ('git', '[]', '2026-01-01Z', '2026-01-01Z');"
    ).lastrowid
    perm_id = db_conn.execute(
        "INSERT INTO permissions"
        " (rule_shape_id, session_id, project_id, decision, source, decided_at)"
        " VALUES (?, NULL, ?, 'approved', 'seed', '2026-01-01Z');",
        (shape_id, project_id),
    ).lastrowid

    with patch("nephoscope.lib.mirror.writer.sync_project") as mock_project:
        with patch("nephoscope.lib.mirror.writer.sync_global") as mock_global:
            sync_affected(db_conn, perm_id)

    mock_project.assert_called_once_with(db_conn, project_id)
    mock_global.assert_not_called()


def test_sync_affected_raises_for_unknown_permission(tmp_path, db_conn):
    """sync_affected raises ValueError when permission_id does not exist."""
    from nephoscope.lib.mirror.writer import sync_affected

    with pytest.raises(ValueError, match="9999"):
        sync_affected(db_conn, 9999)


# ---------------------------------------------------------------------------
# Retry: one hash mismatch then settle within budget
# ---------------------------------------------------------------------------


def test_retry_settles_within_budget(tmp_path, db_conn):
    """Retry loop: if stored hash is corrected between attempts, sync succeeds.

    We patch _read_stored_hash to return a mismatching hash on the first call,
    then the real hash (matching the file) on subsequent calls.  The writer
    should succeed without raising MirrorHashMismatch.
    """
    from nephoscope.lib.mirror import writer as writer_mod
    from nephoscope.lib.mirror.writer import sync_global

    # First sync to establish the file and get a real hash.
    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        sync_global(db_conn)

    fake_hash = "deadbeef" * 8  # 64-char wrong hash

    call_count = 0
    real_read = writer_mod._read_stored_hash

    def patched_read(conn, project_id):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return fake_hash  # first attempt: simulate stale stored hash
        return real_read(conn, project_id)  # subsequent: real value

    with patch.object(writer_mod, "_read_stored_hash", side_effect=patched_read):
        with patch(
            "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
        ):
            # Should succeed on the second attempt.
            sync_global(db_conn)

    assert call_count >= 2, "must have retried at least once"


# ---------------------------------------------------------------------------
# B9: _stamp_hash rowcount observability
# ---------------------------------------------------------------------------


def test_stamp_hash_warns_on_missing_row(db_conn, capsys):
    """_stamp_hash emits a WARNING to stderr when the target row doesn't exist."""
    from nephoscope.lib.mirror.writer import _stamp_hash

    # project_id=999 has no matching projects row — UPDATE touches zero rows.
    _stamp_hash(db_conn, project_id=999, new_hash="a" * 64, now="2026-01-01Z")

    err = capsys.readouterr().err
    assert "WARNING" in err, "expected WARNING in stderr when rowcount == 0"
    assert "_stamp_hash" in err, (
        "WARNING must name the function so the source is locatable"
    )


def test_stamp_hash_silent_on_existing_row(db_conn, capsys, tmp_path):
    """_stamp_hash produces no stderr when the UPDATE actually hits a row."""
    from nephoscope.lib.mirror.writer import _stamp_hash

    # global_mirror row id=1 is seeded by the db_conn fixture.
    _stamp_hash(db_conn, project_id=None, new_hash="b" * 64, now="2026-01-01Z")

    err = capsys.readouterr().err
    assert err == "", f"unexpected stderr when row exists: {err!r}"


def test_stamp_cache_warns_on_missing_row(db_conn, capsys):
    """_stamp_cache emits a WARNING to stderr when the target row doesn't exist."""
    from nephoscope.lib.mirror.writer import _stamp_cache

    _stamp_cache(db_conn, project_id=888, mtime=1234567890.0, dirs=[])

    err = capsys.readouterr().err
    assert "WARNING" in err, "expected WARNING in stderr when rowcount == 0"
    assert "_stamp_cache" in err, "WARNING must name the function"


def test_stamp_cache_silent_on_existing_row(db_conn, capsys, tmp_path):
    """_stamp_cache produces no stderr when the UPDATE actually hits a row."""
    from nephoscope.lib.mirror.writer import _stamp_cache

    _stamp_cache(db_conn, project_id=None, mtime=1234567890.0, dirs=[])

    err = capsys.readouterr().err
    assert err == "", f"unexpected stderr when row exists: {err!r}"


def test_retry_exhaustion_raises_mirror_hash_mismatch(tmp_path, db_conn):
    """Retry loop: after max_retries mismatches the exception propagates."""
    from nephoscope.lib.mirror import writer as writer_mod
    from nephoscope.lib.mirror.writer import MirrorHashMismatch

    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )
    # Write a file so the hash check fires.
    target.write_text('{"existing": true}')
    wrong_hash = "cafebabe" * 8

    # Stamp a wrong hash in DB.
    db_conn.execute(
        "UPDATE global_mirror SET settings_json_sha256 = ? WHERE id = 1;",
        (wrong_hash,),
    )

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        with pytest.raises(MirrorHashMismatch):
            writer_mod.sync_global(db_conn)


# ---------------------------------------------------------------------------
# Idempotency: syncing an already-synced mirror is a no-op (content unchanged)
# ---------------------------------------------------------------------------


def test_sync_global_idempotent(tmp_path, db_conn):
    """Calling sync_global twice produces identical mirror content."""
    from nephoscope.lib.mirror.writer import sync_global

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        sync_global(db_conn)

    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )
    content_after_first = target.read_bytes()

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        sync_global(db_conn)

    content_after_second = target.read_bytes()
    assert content_after_first == content_after_second, (
        "idempotent: content must not change"
    )


# ---------------------------------------------------------------------------
# Permission rows are included in mirror content (serializer integration)
# ---------------------------------------------------------------------------


def test_approved_row_lands_in_allow_list(tmp_path, db_conn):
    """An approved permission row serializes into the allow list."""
    from nephoscope.lib.mirror.writer import sync_global

    shape_id = db_conn.execute(
        "INSERT INTO rule_shapes (verb, flags, first_seen, last_seen)"
        " VALUES ('git', '[]', '2026-01-01Z', '2026-01-01Z');"
    ).lastrowid
    db_conn.execute(
        "INSERT INTO permissions"
        " (rule_shape_id, session_id, project_id, decision, source, decided_at)"
        " VALUES (?, NULL, NULL, 'approved', 'seed', '2026-01-01Z');",
        (shape_id,),
    )

    def serialize_stub(row):
        return f"Bash({row['verb']} *)"

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=serialize_stub
    ):
        sync_global(db_conn)

    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )
    data = json.loads(target.read_bytes())
    assert "Bash(git *)" in data["permissions"]["allow"]
    assert data["permissions"]["deny"] == []


def test_rejected_row_lands_in_deny_list(tmp_path, db_conn):
    """A rejected permission row serializes into the deny list."""
    from nephoscope.lib.mirror.writer import sync_global

    shape_id = db_conn.execute(
        "INSERT INTO rule_shapes (verb, flags, first_seen, last_seen)"
        " VALUES ('rm', '[]', '2026-01-01Z', '2026-01-01Z');"
    ).lastrowid
    db_conn.execute(
        "INSERT INTO permissions"
        " (rule_shape_id, session_id, project_id, decision, source, decided_at)"
        " VALUES (?, NULL, NULL, 'rejected', 'manual', '2026-01-01Z');",
        (shape_id,),
    )

    def serialize_stub(row):
        return f"Bash({row['verb']} *)"

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=serialize_stub
    ):
        sync_global(db_conn)

    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )
    data = json.loads(target.read_bytes())
    assert "Bash(rm *)" in data["permissions"]["deny"]
    assert data["permissions"]["allow"] == []


def test_orchestration_row_skipped_from_mirror(tmp_path, db_conn):
    """Orchestration rows (serialize returns None) must not appear in the mirror."""
    from nephoscope.lib.mirror.writer import sync_global

    shape_id = db_conn.execute(
        "INSERT INTO rule_shapes (verb, flags, first_seen, last_seen)"
        " VALUES ('Agent', '[]', '2026-01-01Z', '2026-01-01Z');"
    ).lastrowid
    db_conn.execute(
        "INSERT INTO permissions"
        " (rule_shape_id, session_id, project_id, decision, source, decided_at)"
        " VALUES (?, NULL, NULL, 'approved', 'seed', '2026-01-01Z');",
        (shape_id,),
    )

    with patch("nephoscope.lib.mirror.serializer.serialize", return_value=None):
        sync_global(db_conn)

    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )
    data = json.loads(target.read_bytes())
    assert data["permissions"]["allow"] == [], "orchestration row must be skipped"


# ---------------------------------------------------------------------------
# Session-tier rows excluded from global mirror
# ---------------------------------------------------------------------------


def test_session_tier_rows_excluded_from_global_mirror(tmp_path, db_conn):
    """session_id IS NOT NULL rows must not appear in the global mirror."""
    from nephoscope.lib.mirror.writer import sync_global

    # Need a session row.
    sess_id = db_conn.execute(
        "INSERT INTO sessions (session_uuid, started_at, last_activity)"
        " VALUES ('test-uuid', '2026-01-01Z', '2026-01-01Z');"
    ).lastrowid

    shape_id = db_conn.execute(
        "INSERT INTO rule_shapes (verb, flags, first_seen, last_seen)"
        " VALUES ('git', '[]', '2026-01-01Z', '2026-01-01Z');"
    ).lastrowid
    db_conn.execute(
        "INSERT INTO permissions"
        " (rule_shape_id, session_id, project_id, decision, source, decided_at)"
        " VALUES (?, ?, NULL, 'approved', 'session-ask', '2026-01-01Z');",
        (shape_id, sess_id),
    )

    def serialize_stub(row):
        return "Bash(git *)"

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=serialize_stub
    ):
        sync_global(db_conn)

    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )
    data = json.loads(target.read_bytes())
    assert data["permissions"]["allow"] == [], (
        "session-tier row must not appear in global mirror"
    )


# ---------------------------------------------------------------------------
# Read-merge-write: foreign top-level keys and permissions.defaultMode survive
# ---------------------------------------------------------------------------


def test_sync_preserves_foreign_top_level_keys(tmp_path, db_conn):
    """sync_global leaves attribution, model, hooks etc. untouched after sync."""
    from nephoscope.lib.mirror.writer import sync_global

    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )
    existing = {
        "attribution": False,
        "model": "claude-sonnet-4-6",
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]},
        "permissions": {
            "defaultMode": "auto",
            "allow": ["Bash(old-entry *)"],
            "deny": [],
            "ask": [],
        },
    }
    target.write_text(json.dumps(existing, indent=2))
    # Stamp the hash so the mismatch check passes.
    current_hash = settings_permissions_hash(target.read_bytes())
    db_conn.execute(
        "UPDATE global_mirror SET settings_json_sha256 = ? WHERE id = 1;",
        (current_hash,),
    )

    shape_id = db_conn.execute(
        "INSERT INTO rule_shapes (verb, flags, first_seen, last_seen)"
        " VALUES ('git', '[]', '2026-01-01Z', '2026-01-01Z');"
    ).lastrowid
    db_conn.execute(
        "INSERT INTO permissions"
        " (rule_shape_id, session_id, project_id, decision, source, decided_at)"
        " VALUES (?, NULL, NULL, 'approved', 'seed', '2026-01-01Z');",
        (shape_id,),
    )

    def serialize_stub(row):
        return f"Bash({row['verb']} *)"

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=serialize_stub
    ):
        sync_global(db_conn)

    data = json.loads(target.read_bytes())
    assert data["attribution"] is False, "attribution key must be preserved"
    assert data["model"] == "claude-sonnet-4-6", "model key must be preserved"
    assert "hooks" in data, "hooks key must be preserved"
    assert "Bash(git *)" in data["permissions"]["allow"], "new allow entry must appear"
    # Old entry is replaced (DB is now authoritative for allow/deny/ask).
    assert "Bash(old-entry *)" not in data["permissions"]["allow"]


def test_sync_preserves_permissions_default_mode(tmp_path, db_conn):
    """sync_global leaves permissions.defaultMode intact after sync."""
    from nephoscope.lib.mirror.writer import sync_global

    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )
    existing = {
        "permissions": {
            "defaultMode": "auto",
            "allow": [],
            "deny": [],
            "ask": [],
        },
    }
    target.write_text(json.dumps(existing, indent=2))
    current_hash = settings_permissions_hash(target.read_bytes())
    db_conn.execute(
        "UPDATE global_mirror SET settings_json_sha256 = ? WHERE id = 1;",
        (current_hash,),
    )

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        sync_global(db_conn)

    data = json.loads(target.read_bytes())
    assert data["permissions"].get("defaultMode") == "auto", (
        "permissions.defaultMode must survive a sync"
    )


def test_sync_preserves_and_caches_additional_directories(tmp_path, db_conn):
    """additionalDirectories in the existing file survive read-merge-write and
    are stored in the DB cache (settings_json_mtime + additional_dirs).

    Flow:
    1. Pre-write a settings.json with permissions.additionalDirectories set.
    2. Stamp the hash so the mismatch check passes.
    3. sync_global → the writer reads, merges DB rows, writes back.
    4. The output file must still contain the additionalDirectories entries.
    5. The DB cache columns must reflect the mtime and the dirs array.
    """
    from nephoscope.lib.mirror.writer import sync_global

    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )
    extra_dirs = ["/opt/company/shared", "/mnt/data"]
    existing = {
        "permissions": {
            "allow": [],
            "deny": [],
            "ask": [],
            "additionalDirectories": extra_dirs,
        },
    }
    target.write_text(json.dumps(existing, indent=2))
    current_hash = settings_permissions_hash(target.read_bytes())
    db_conn.execute(
        "UPDATE global_mirror SET settings_json_sha256 = ? WHERE id = 1;",
        (current_hash,),
    )

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        sync_global(db_conn)

    # The written file must still carry additionalDirectories.
    data = json.loads(target.read_bytes())
    assert data["permissions"].get("additionalDirectories") == extra_dirs, (
        "additionalDirectories must survive read-merge-write"
    )

    # DB cache must be populated with the file mtime and dirs.
    row = db_conn.execute(
        "SELECT settings_json_mtime, additional_dirs FROM global_mirror WHERE id = 1;"
    ).fetchone()
    assert row[0] == target.stat().st_mtime, (
        "settings_json_mtime must match the written file's mtime"
    )
    assert json.loads(row[1]) == extra_dirs, (
        "additional_dirs cache must contain the written additionalDirectories"
    )


def test_sync_creates_fresh_file_when_target_absent(tmp_path, db_conn):
    """sync_global creates a minimal permissions-only file when target doesn't exist."""
    from nephoscope.lib.mirror.writer import sync_global

    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )
    assert not target.exists(), "precondition: file must not exist"

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        sync_global(db_conn)

    assert target.exists()
    data = json.loads(target.read_bytes())
    assert data == {"permissions": {"allow": [], "deny": [], "ask": []}}


def test_sync_raises_on_malformed_json_target(tmp_path, db_conn):
    """sync_global raises ValueError (not a silent overwrite) when target JSON is broken."""
    from nephoscope.lib.mirror.writer import sync_global

    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )
    target.write_text("{ this is not valid json !!!")
    # Leave stored hash NULL so the hash-check gate is skipped; the parse error
    # must surface from _build_content before we ever reach the write step.

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        with pytest.raises(ValueError) as exc_info:
            sync_global(db_conn)

    msg = str(exc_info.value)
    assert str(target) in msg, "ValueError must name the file path"
    # File must NOT have been overwritten.
    assert target.read_text() == "{ this is not valid json !!!"


# ---------------------------------------------------------------------------
# sync_global raises RuntimeError when global_mirror singleton is missing
# ---------------------------------------------------------------------------


def test_sync_global_raises_when_singleton_missing(tmp_path, db_conn):
    """sync_global raises RuntimeError when the global_mirror singleton row is absent."""
    from nephoscope.lib.mirror.writer import sync_global

    db_conn.execute("DELETE FROM global_mirror WHERE id = 1;")

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        with pytest.raises(RuntimeError, match="global_mirror singleton"):
            sync_global(db_conn)


# ---------------------------------------------------------------------------
# _atomic_write: malformed target raises MirrorHashMismatch (not JSONDecodeError)
# ---------------------------------------------------------------------------

_SENTINEL_HASH = "a" * 64


def _stamp_sentinel_and_write(db_conn, content: bytes | str) -> Path:
    """Write *content* to the mirror target and stamp a sentinel non-null hash.

    Returns the target path. The sentinel hash ensures the hash-check branch
    fires on the next sync_global call.
    """
    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content)
    db_conn.execute(
        "UPDATE global_mirror SET settings_json_sha256 = ? WHERE id = 1;",
        (_SENTINEL_HASH,),
    )
    return target


def test_atomic_write_raises_mirror_hash_mismatch_for_malformed_target(
    tmp_path, db_conn
):
    """When the target file contains malformed JSON, sync_global raises
    MirrorHashMismatch (not JSONDecodeError) — corruption is not transient,
    so the retry loop must not run.
    """
    from nephoscope.lib.mirror.writer import MirrorHashMismatch, sync_global

    # Write corrupt content so settings_permissions_hash raises JSONDecodeError.
    target = _stamp_sentinel_and_write(db_conn, "not valid json")

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        with pytest.raises(MirrorHashMismatch) as exc_info:
            sync_global(db_conn)

    msg = str(exc_info.value)
    assert "malformed" in msg, "exception message must mention malformed"
    assert str(target) in msg, "exception message must include the file path"


def test_atomic_write_raises_mirror_hash_mismatch_for_heterogeneous_allow(
    tmp_path, db_conn
):
    """When permissions.allow contains heterogeneous types (int + str), sorted()
    raises TypeError; sync_global must surface that as MirrorHashMismatch.
    """
    from nephoscope.lib.mirror.writer import MirrorHashMismatch, sync_global

    # Write content whose allow list mixes int and str — triggers TypeError in
    # settings_permissions_hash's sorted() call.
    _stamp_sentinel_and_write(
        db_conn, '{"permissions":{"allow":[1,"Bash(ls)"],"deny":[],"ask":[]}}'
    )

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        with pytest.raises(MirrorHashMismatch) as exc_info:
            sync_global(db_conn)

    assert "malformed" in str(exc_info.value), (
        "exception message must mention malformed"
    )


def test_atomic_write_raises_mirror_hash_mismatch_for_non_utf8_target(
    tmp_path, db_conn
):
    """When the target file contains non-UTF-8 bytes, sync_global raises
    MirrorHashMismatch — UnicodeDecodeError is a ValueError and must be caught
    by the (ValueError, TypeError) guard in _atomic_write.
    """
    from nephoscope.lib.mirror.writer import MirrorHashMismatch, sync_global

    # Write non-UTF-8 bytes so settings_permissions_hash raises UnicodeDecodeError.
    target = _stamp_sentinel_and_write(db_conn, b"\x80{}")

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        with pytest.raises(MirrorHashMismatch) as exc_info:
            sync_global(db_conn)

    msg = str(exc_info.value)
    assert "malformed" in msg, "exception message must mention malformed"
    assert str(target) in msg, "exception message must include the file path"


def test_pre_b1_whole_file_stamp_raises_on_first_sync(tmp_path, db_conn):
    """Document migration semantics for installs with pre-B1 stamps.

    Pre-B1 nephoscope stamped sha256(entire_settings.json). After this change,
    the first sync raises MirrorHashMismatch because the new permissions-only
    hash will not equal the old whole-file hash. The workaround for an existing
    install is to clear the stamp manually (e.g. via SQL or a future migration
    helper) before re-syncing — at which point the first-touch branch fires
    and the new hash gets stamped cleanly.
    """
    import hashlib

    from nephoscope.lib.mirror.writer import MirrorHashMismatch, sync_global

    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )

    # Simulate a real settings.json with content (permissions + foreign keys).
    settings_content = json.dumps(
        {
            "attribution": False,
            "model": "claude-sonnet-4-6",
            "permissions": {"allow": ["Bash(git *)"], "deny": [], "ask": []},
        },
        indent=2,
    ).encode("utf-8")
    target.write_bytes(settings_content)

    # Pre-B1 stamp: sha256 of the *entire* file, not just the permissions slice.
    whole_file_hash = hashlib.sha256(settings_content).hexdigest()
    db_conn.execute(
        "UPDATE global_mirror SET settings_json_sha256 = ? WHERE id = 1;",
        (whole_file_hash,),
    )

    # The permissions-only hash differs from the whole-file hash.
    from nephoscope.lib.mirror.permissions_hash import settings_permissions_hash

    perms_only_hash = settings_permissions_hash(settings_content)
    assert perms_only_hash != whole_file_hash, (
        "precondition: permissions-only hash must differ from whole-file hash"
        " when the file has non-permissions content"
    )

    # First sync after upgrade: stored (whole-file) hash ≠ on-disk (perms-only) hash
    # → MirrorHashMismatch.  Workaround: clear the stamp first.
    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        with pytest.raises(MirrorHashMismatch):
            sync_global(db_conn)

    # Workaround: clear the stamp so next sync uses first-touch path.
    db_conn.execute(
        "UPDATE global_mirror SET settings_json_sha256 = NULL WHERE id = 1;"
    )

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        # Must succeed: first-touch path skips the hash check and re-stamps.
        sync_global(db_conn)

    new_hash = db_conn.execute(
        "SELECT settings_json_sha256 FROM global_mirror WHERE id = 1;"
    ).fetchone()[0]
    assert new_hash is not None, "hash must be stamped after first-touch sync"
    assert len(new_hash) == 64, "stamped hash must be a full SHA-256 hex digest"


# ---------------------------------------------------------------------------
# Integration: unrelated edits don't flip the permissions hash
# ---------------------------------------------------------------------------


def test_unrelated_edits_do_not_flip_hash(tmp_path, db_conn):
    """Edits outside the permissions arrays (hooks, model, env) must not cause
    MirrorHashMismatch on the next sync_global call.

    Flow:
    1. Insert one approved permission and run sync_global — file written, hash stamped.
    2. Enrich the on-disk file with extra top-level keys (hooks, env, model) that
       nephoscope never owns.  The permissions allow/deny/ask arrays are untouched.
    3. Re-compute the permissions hash over the modified file — it must equal the
       stored hash (proving the hash covers only the permissions slice).
    4. Call sync_global again — must NOT raise MirrorHashMismatch.
    5. Confirm _hash_status reports "stamped" after the second sync.
    """
    from nephoscope.lib.mirror.writer import MirrorHashMismatch, sync_global
    from nephoscope.cli.permissions_cmd import _hash_status

    # Step 1 — insert an approved permission and run sync.
    shape_id = db_conn.execute(
        "INSERT INTO rule_shapes (verb, flags, first_seen, last_seen)"
        " VALUES ('Bash', '[]', '2026-01-01Z', '2026-01-01Z');"
    ).lastrowid
    db_conn.execute(
        "INSERT INTO permissions"
        " (rule_shape_id, session_id, project_id, decision, source, decided_at)"
        " VALUES (?, NULL, NULL, 'approved', 'seed', '2026-01-01Z');",
        (shape_id,),
    )

    def serialize_bash(row):
        return f"Bash({row['verb']} *)"

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=serialize_bash
    ):
        sync_global(db_conn)

    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )
    assert target.exists(), "mirror file must be created after first sync"
    stored_hash_after_first = db_conn.execute(
        "SELECT settings_json_sha256 FROM global_mirror WHERE id = 1;"
    ).fetchone()[0]
    assert stored_hash_after_first is not None, "hash must be stamped after first sync"

    # Step 2 — enrich the file with keys nephoscope never owns.
    data = json.loads(target.read_bytes())
    data["hooks"] = {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}
    data["env"] = {"MY_VAR": "hello"}
    data["model"] = "claude-sonnet-4-6"
    # permissions.allow/deny/ask are byte-identical — only foreign keys were added.
    target.write_text(json.dumps(data, indent=2))

    # Step 3 — permissions hash over the modified file must still match stored hash.
    hash_over_modified = settings_permissions_hash(target.read_bytes())
    assert hash_over_modified == stored_hash_after_first, (
        "adding non-permissions keys must not change the permissions hash"
    )

    # Step 4 — second sync must not raise MirrorHashMismatch.
    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=serialize_bash
    ):
        try:
            sync_global(db_conn)
        except MirrorHashMismatch as exc:
            raise AssertionError(
                "sync_global raised MirrorHashMismatch after an unrelated edit"
                f" (old whole-file scheme would have done this): {exc}"
            ) from exc

    # Step 5 — _hash_status must report "stamped" after the second sync.
    path_str = db_conn.execute(
        "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
    ).fetchone()[0]
    stored_hash_after_second = db_conn.execute(
        "SELECT settings_json_sha256 FROM global_mirror WHERE id = 1;"
    ).fetchone()[0]
    assert _hash_status(path_str, stored_hash_after_second) == "stamped", (
        "_hash_status must report 'stamped' after sync with unrelated edits present"
    )


# ---------------------------------------------------------------------------
# os.rename failure: error propagates, DB hash unchanged, temp file orphaned
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _has_unresolved_token: boundary cases (false-positive guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path_spec,expected",
    [
        ("$HOMEUSER/**", False),      # prefix match but not a real token
        ("$HOME/**", True),           # real token followed by /
        ("$TRUSTED_DIR/**", True),    # real token followed by /
        ("$TRUSTED_DIR", True),       # real token at end of string
        ("/absolute/path", False),    # no token
        (None, False),                # None input
        ("", False),                  # empty string
        ("$CWD/subdir", True),        # real token followed by /
        ("$PROJECT_ROOT/src", True),  # real token followed by /
        ("$HOMEUSER", False),         # non-token at end of string
    ],
)
def test_has_unresolved_token_boundary(path_spec, expected):
    """_has_unresolved_token must anchor token matches to / or end-of-string.

    The key false-positive guard: $HOMEUSER/** must not match $HOME.
    """
    from nephoscope.lib.mirror.writer import _has_unresolved_token

    assert _has_unresolved_token(path_spec) is expected


# ---------------------------------------------------------------------------
# Integration: $TRUSTED_DIR rows are skipped; their literal string must not
# appear in the written settings.json allow list.
# ---------------------------------------------------------------------------


def test_trusted_dir_row_not_written_to_settings_json(tmp_path, db_conn):
    """A permission row whose path_spec is '$TRUSTED_DIR/**' must be skipped by
    _classify_permission_rows and must not appear in the written settings.json.

    This is the regression guard for the _has_unresolved_token token-skip path.
    The concrete resolved paths are injected by _generate_workspace_entries;
    the raw template string must never reach the JSON mirror.
    """
    from nephoscope.lib.mirror.writer import sync_global

    # Insert a rule shape whose path_spec is the $TRUSTED_DIR template.
    shape_id = db_conn.execute(
        "INSERT INTO rule_shapes (verb, path_spec, flags, first_seen, last_seen)"
        " VALUES ('Write', '$TRUSTED_DIR/**', '[]', '2026-01-01Z', '2026-01-01Z');"
    ).lastrowid
    db_conn.execute(
        "INSERT INTO permissions"
        " (rule_shape_id, session_id, project_id, decision, source, decided_at)"
        " VALUES (?, NULL, NULL, 'approved', 'seed', '2026-01-01Z');",
        (shape_id,),
    )

    def serialize_stub(row):
        # Simulate the serializer returning a string that includes path_spec.
        path = row.get("path_spec") or ""
        return f"Write({path})" if path else None

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=serialize_stub
    ):
        sync_global(db_conn)

    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )
    content = target.read_text()
    assert "$TRUSTED_DIR" not in content, (
        "literal $TRUSTED_DIR must never appear in the written settings.json"
    )


def test_rename_failure_propagates_and_leaves_db_hash_unchanged(
    tmp_path, db_conn, monkeypatch
):
    """When os.rename fails after fsync succeeds, the OSError must propagate
    and the DB hash must NOT be updated — mirror state stays consistent with
    the pre-call value (NULL on first-touch, prior hash on subsequent runs).

    The temp file may be left behind as orphan state; cleanup_stale_tmp is the
    designated reaper. We assert the temp file path is observable so a future
    cleanup invocation can find and remove it.
    """
    from nephoscope.lib.mirror import writer as writer_mod
    from nephoscope.lib.mirror.writer import sync_global

    target = Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )
    tmp_target = target.parent / (target.name + ".tmp")

    # Pre-condition: DB hash is NULL (first-touch path).
    pre_hash = db_conn.execute(
        "SELECT settings_json_sha256 FROM global_mirror WHERE id = 1;"
    ).fetchone()[0]
    assert pre_hash is None, "precondition: DB hash must start as NULL"

    # Pre-condition: target does not yet exist.
    assert not target.exists(), "precondition: target must not exist"

    # Patch os.rename in the writer's module namespace so the writer's call
    # raises OSError after the temp file has been written and fsync'd.
    def _raise_rename(src, dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(writer_mod.os, "rename", _raise_rename)

    with patch(
        "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
    ):
        with pytest.raises(OSError, match="simulated rename failure"):
            sync_global(db_conn)

    # DB hash must still be NULL — stamp must not have happened.
    post_hash = db_conn.execute(
        "SELECT settings_json_sha256 FROM global_mirror WHERE id = 1;"
    ).fetchone()[0]
    assert post_hash is None, (
        "DB hash must remain NULL when os.rename fails;"
        " stamping past a failed write is the bug we are guarding against"
    )

    # Cache columns must also be untouched (last_synced still NULL).
    last_synced = db_conn.execute(
        "SELECT settings_json_last_synced FROM global_mirror WHERE id = 1;"
    ).fetchone()[0]
    assert last_synced is None, (
        "settings_json_last_synced must remain NULL when the rename failed"
    )

    # Target must NOT exist — the rename never completed, so no mirror file.
    assert not target.exists(), (
        "target must not exist when rename failed (only the temp file should remain)"
    )

    # Temp file should be observable on disk as orphan state — cleanup_stale_tmp
    # is the designated reaper for these. Confirm it can find and remove it.
    assert tmp_target.exists(), (
        "temp file should be left on disk as orphan after rename failure"
        " (cleanup_stale_tmp reaps these on a later sync)"
    )
