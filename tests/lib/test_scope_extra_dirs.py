"""TDD tests for scope.get_additional_dirs — mtime-gated additionalDirectories cache.

Tests cover the full unit surface of get_additional_dirs:
- cache hit (mtime matches) → file NOT re-read
- cache miss (mtime differs) → file re-parsed, cache updated
- first-call (cached_mtime IS NULL) → always slow path
- missing settings file → returns []
- malformed JSON → returns [], cache unchanged
- non-UTF-8 bytes → same as malformed
- no permissions.additionalDirectories key → returns [], cache updated to empty
- non-string entries → coerced via str(), returned intact
- both global_mirror and projects scopes work
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nephoscope.lib.scope import Scope, get_additional_dirs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    """In-memory SQLite DB with the four new columns (minimal schema)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE global_mirror (
            id                  INTEGER PRIMARY KEY CHECK (id = 1),
            settings_json_path  TEXT NOT NULL,
            settings_json_sha256 TEXT,
            settings_json_last_synced TEXT,
            settings_json_mtime REAL,
            additional_dirs     TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE projects (
            id                        INTEGER PRIMARY KEY AUTOINCREMENT,
            cwd                       TEXT UNIQUE NOT NULL,
            name                      TEXT,
            root                      TEXT,
            first_seen                TEXT NOT NULL,
            last_seen                 TEXT NOT NULL,
            settings_json_path        TEXT,
            settings_json_sha256      TEXT,
            settings_json_last_synced TEXT,
            settings_json_mtime       REAL,
            additional_dirs           TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_uuid    TEXT UNIQUE NOT NULL,
            project_id      INTEGER REFERENCES projects(id),
            started_at      TEXT NOT NULL,
            last_activity   TEXT NOT NULL,
            transcript_path TEXT,
            extra_dirs      TEXT NOT NULL DEFAULT '[]'
        )
    """)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture()
def settings_file(tmp_path):
    """A writable settings.json in tmp_path. Returns the Path."""
    p = tmp_path / "settings.json"
    return p


def _write_settings(p: Path, dirs: list) -> None:
    """Write a canonical settings.json with given additionalDirectories."""
    p.write_text(json.dumps({"permissions": {"additionalDirectories": dirs}}))


def _seed_global(conn, path_str: str, mtime: float | None, dirs_json: str | None):
    """Insert or replace the global_mirror singleton row."""
    conn.execute(
        "INSERT OR REPLACE INTO global_mirror"
        " (id, settings_json_path, settings_json_mtime, additional_dirs)"
        " VALUES (1, ?, ?, ?)",
        (path_str, mtime, dirs_json),
    )
    conn.commit()


def _seed_project(
    conn, path_str: str, mtime: float | None, dirs_json: str | None
) -> int:
    """Insert a project row and return its id."""
    cur = conn.execute(
        "INSERT INTO projects"
        " (cwd, name, root, first_seen, last_seen,"
        "  settings_json_path, settings_json_mtime, additional_dirs)"
        " VALUES ('/some/cwd', 'test', '/some', '2026-01-01', '2026-01-01',"
        "  ?, ?, ?)",
        (path_str, mtime, dirs_json),
    )
    conn.commit()
    return cur.lastrowid


def _assert_global_cache_unchanged(
    conn, expected_mtime: float, expected_json: str
) -> None:
    """Assert that the global_mirror cache columns were NOT overwritten."""
    row = conn.execute(
        "SELECT settings_json_mtime, additional_dirs FROM global_mirror WHERE id = 1"
    ).fetchone()
    assert row[0] == expected_mtime
    assert row[1] == expected_json


# ---------------------------------------------------------------------------
# Cache-hit: mtime matches → file NOT re-parsed
# ---------------------------------------------------------------------------


def test_cache_hit_global_returns_cached_dirs_without_reading_file(
    db, settings_file, monkeypatch
):
    """Cache hit (global_mirror): mtime matches, returns cached dirs, file NOT re-read."""
    _write_settings(settings_file, ["/cached/path"])
    on_disk_mtime = settings_file.stat().st_mtime
    cached_json = json.dumps(["/cached/path"])
    _seed_global(db, str(settings_file), on_disk_mtime, cached_json)

    # If read_bytes is called, the test fails — cache should be used.
    monkeypatch.setattr(
        Path, "read_bytes", lambda self: pytest.fail("file was re-read on cache hit")
    )

    result = get_additional_dirs(db, Scope("global_mirror", 1))
    assert result == ["/cached/path"]


def test_cache_hit_project_returns_cached_dirs_without_reading_file(
    db, settings_file, monkeypatch
):
    """Cache hit (projects): mtime matches, returns cached dirs, file NOT re-read."""
    _write_settings(settings_file, ["/proj/extra"])
    on_disk_mtime = settings_file.stat().st_mtime
    cached_json = json.dumps(["/proj/extra"])
    proj_id = _seed_project(db, str(settings_file), on_disk_mtime, cached_json)

    monkeypatch.setattr(
        Path, "read_bytes", lambda self: pytest.fail("file was re-read on cache hit")
    )

    result = get_additional_dirs(db, Scope("projects", proj_id))
    assert result == ["/proj/extra"]


# ---------------------------------------------------------------------------
# Cache-miss: mtime differs → file re-parsed, cache updated
# ---------------------------------------------------------------------------


def test_cache_miss_global_reparses_file_and_updates_cache(db, settings_file):
    """Cache miss (global_mirror): stale mtime forces re-parse; cache row updated."""
    _write_settings(settings_file, ["/new/dir"])
    on_disk_mtime = settings_file.stat().st_mtime
    # Seed with a different (old) mtime so cache is stale.
    _seed_global(db, str(settings_file), on_disk_mtime - 1.0, json.dumps(["/old/dir"]))

    result = get_additional_dirs(db, Scope("global_mirror", 1))
    assert result == ["/new/dir"]

    # Verify DB cache updated.
    row = db.execute(
        "SELECT settings_json_mtime, additional_dirs FROM global_mirror WHERE id = 1"
    ).fetchone()
    assert row[0] == on_disk_mtime
    assert json.loads(row[1]) == ["/new/dir"]


def test_cache_miss_project_reparses_file_and_updates_cache(db, settings_file):
    """Cache miss (projects): stale mtime forces re-parse; cache row updated."""
    _write_settings(settings_file, ["/proj/new"])
    on_disk_mtime = settings_file.stat().st_mtime
    proj_id = _seed_project(
        db, str(settings_file), on_disk_mtime - 1.0, json.dumps(["/proj/old"])
    )

    result = get_additional_dirs(db, Scope("projects", proj_id))
    assert result == ["/proj/new"]

    row = db.execute(
        "SELECT settings_json_mtime, additional_dirs FROM projects WHERE id = ?",
        (proj_id,),
    ).fetchone()
    assert row[0] == on_disk_mtime
    assert json.loads(row[1]) == ["/proj/new"]


# ---------------------------------------------------------------------------
# First-call: cached_mtime IS NULL → always slow path
# ---------------------------------------------------------------------------


def test_first_call_null_mtime_always_slow_path(db, settings_file):
    """First call (cached_mtime NULL): slow path fires; cache populated."""
    _write_settings(settings_file, ["/first/dir"])
    on_disk_mtime = settings_file.stat().st_mtime
    _seed_global(db, str(settings_file), None, None)

    result = get_additional_dirs(db, Scope("global_mirror", 1))
    assert result == ["/first/dir"]

    row = db.execute(
        "SELECT settings_json_mtime, additional_dirs FROM global_mirror WHERE id = 1"
    ).fetchone()
    assert row[0] == on_disk_mtime
    assert json.loads(row[1]) == ["/first/dir"]


# ---------------------------------------------------------------------------
# Missing settings file → []
# ---------------------------------------------------------------------------


def test_missing_settings_file_returns_empty(db, tmp_path):
    """Missing settings file returns [], no crash."""
    _seed_global(db, str(tmp_path / "nonexistent.json"), None, None)
    assert get_additional_dirs(db, Scope("global_mirror", 1)) == []


def test_no_settings_json_path_returns_empty(db):
    """Row with empty settings_json_path returns []."""
    db.execute(
        "INSERT OR REPLACE INTO global_mirror"
        " (id, settings_json_path, settings_json_mtime, additional_dirs)"
        " VALUES (1, '', NULL, NULL)",
    )
    db.commit()
    assert get_additional_dirs(db, Scope("global_mirror", 1)) == []


def test_no_row_returns_empty(db):
    """Missing row (id not in table) returns []."""
    assert get_additional_dirs(db, Scope("global_mirror", 1)) == []


# ---------------------------------------------------------------------------
# Malformed JSON → [], cache unchanged
# ---------------------------------------------------------------------------


def test_malformed_json_returns_empty_and_leaves_cache_unchanged(db, settings_file):
    """Malformed JSON: returns [], does NOT overwrite previous good cache."""
    settings_file.write_bytes(b"{not valid json}")
    on_disk_mtime = settings_file.stat().st_mtime
    good_cache = json.dumps(["/good/cached"])
    _seed_global(db, str(settings_file), on_disk_mtime - 1.0, good_cache)

    assert get_additional_dirs(db, Scope("global_mirror", 1)) == []
    _assert_global_cache_unchanged(db, on_disk_mtime - 1.0, good_cache)


# ---------------------------------------------------------------------------
# Non-UTF-8 bytes → same as malformed
# ---------------------------------------------------------------------------


def test_non_utf8_bytes_returns_empty_and_leaves_cache_unchanged(db, settings_file):
    """Non-UTF-8 bytes: returns [], cache left intact."""
    settings_file.write_bytes(b"\xff\xfe invalid utf8")
    on_disk_mtime = settings_file.stat().st_mtime
    good_cache = json.dumps(["/safe/cache"])
    _seed_global(db, str(settings_file), on_disk_mtime - 1.0, good_cache)

    assert get_additional_dirs(db, Scope("global_mirror", 1)) == []
    _assert_global_cache_unchanged(db, on_disk_mtime - 1.0, good_cache)


# ---------------------------------------------------------------------------
# No permissions.additionalDirectories key → [], cache updated to empty
# ---------------------------------------------------------------------------


def test_no_additional_directories_key_returns_empty_and_updates_cache(
    db, settings_file
):
    """Missing additionalDirectories key: returns [], updates cache to '[]'."""
    settings_file.write_text(json.dumps({"permissions": {}}))
    on_disk_mtime = settings_file.stat().st_mtime
    _seed_global(db, str(settings_file), None, None)

    result = get_additional_dirs(db, Scope("global_mirror", 1))
    assert result == []

    row = db.execute(
        "SELECT settings_json_mtime, additional_dirs FROM global_mirror WHERE id = 1"
    ).fetchone()
    assert row[0] == on_disk_mtime
    # Cache updated to empty array, not NULL.
    assert json.loads(row[1]) == []


def test_null_permissions_key_returns_empty_and_updates_cache(db, settings_file):
    """NULL permissions value: returns [], updates cache to '[]'."""
    settings_file.write_text(json.dumps({"permissions": None}))
    on_disk_mtime = settings_file.stat().st_mtime
    _seed_global(db, str(settings_file), None, None)

    result = get_additional_dirs(db, Scope("global_mirror", 1))
    assert result == []

    row = db.execute(
        "SELECT settings_json_mtime, additional_dirs FROM global_mirror WHERE id = 1"
    ).fetchone()
    assert row[0] == on_disk_mtime
    assert json.loads(row[1]) == []


# ---------------------------------------------------------------------------
# Non-string entries → coerced via str()
# ---------------------------------------------------------------------------


def test_non_string_entries_coerced_via_str(db, settings_file):
    """Non-string entries in additionalDirectories are coerced via str()."""
    # Mix of int, string, None entries.
    settings_file.write_text(
        json.dumps({"permissions": {"additionalDirectories": [1, "/a", True]}})
    )
    _seed_global(db, str(settings_file), None, None)

    result = get_additional_dirs(db, Scope("global_mirror", 1))
    assert result == ["1", "/a", "True"]

    # Cache should reflect the coerced values.
    row = db.execute(
        "SELECT additional_dirs FROM global_mirror WHERE id = 1"
    ).fetchone()
    assert json.loads(row[0]) == ["1", "/a", "True"]


# ---------------------------------------------------------------------------
# TOCTOU fix: file deleted between exists() and stat() returns []
# ---------------------------------------------------------------------------


def test_file_deleted_between_exists_and_stat_returns_empty(
    db, settings_file, monkeypatch
):
    """TOCTOU guard: Path.stat() raising FileNotFoundError mid-flow returns [].

    Simulates a file that passes the exists() guard but disappears before
    stat() completes — the EAFP rewrite must catch this and return [].
    """
    _write_settings(settings_file, ["/some/dir"])
    _seed_global(db, str(settings_file), None, None)

    monkeypatch.setattr(
        Path, "stat", lambda self: (_ for _ in ()).throw(FileNotFoundError("gone"))
    )

    assert get_additional_dirs(db, Scope("global_mirror", 1)) == []


def test_partial_write_self_heals_on_next_read(db, settings_file):
    """Partial write (only hash updated, mtime/dirs stale) self-heals on next read.

    Simulates a process crash between the two separate UPDATEs in _atomic_write:
    only the hash column is advanced while settings_json_mtime and additional_dirs
    retain their old values.  The next call to get_additional_dirs sees a mtime
    mismatch (file's st_mtime > cached_mtime), fires the slow path, re-parses,
    and restamps the cache to a consistent state.
    """
    # Step 1: initial sync — populate cache with known-good state.
    _write_settings(settings_file, ["/original/dir"])
    on_disk_mtime_v1 = settings_file.stat().st_mtime
    _seed_global(
        db, str(settings_file), on_disk_mtime_v1, json.dumps(["/original/dir"])
    )

    # Verify fast path works before simulating partial write.
    result_v1 = get_additional_dirs(db, Scope("global_mirror", 1))
    assert result_v1 == ["/original/dir"]

    # Step 2: modify file — advance mtime and change additionalDirectories.
    _write_settings(settings_file, ["/new/dir"])
    new_on_disk_mtime = settings_file.stat().st_mtime
    if new_on_disk_mtime == on_disk_mtime_v1:
        import os

        os.utime(settings_file, (new_on_disk_mtime + 1.0, new_on_disk_mtime + 1.0))
        new_on_disk_mtime = settings_file.stat().st_mtime

    # Step 3: simulate partial write — only hash advanced, mtime/dirs still old.
    # (Real crash scenario: _stamp_hash ran, _stamp_cache did not.)
    db.execute(
        "UPDATE global_mirror SET settings_json_sha256 = 'deadbeef' WHERE id = 1"
    )
    # Leave settings_json_mtime = on_disk_mtime_v1 and additional_dirs = ["/original/dir"]
    db.commit()

    # Confirm the partial-write state: cached mtime is stale.
    row = db.execute(
        "SELECT settings_json_mtime, additional_dirs FROM global_mirror WHERE id = 1"
    ).fetchone()
    assert row[0] == on_disk_mtime_v1  # stale
    assert json.loads(row[1]) == ["/original/dir"]  # stale

    # Step 4: next read fires slow path (mtime mismatch) and self-heals.
    result_v2 = get_additional_dirs(db, Scope("global_mirror", 1))
    assert result_v2 == ["/new/dir"], "slow path did not return updated dirs"

    # Cache must now be consistent with the new file state.
    row = db.execute(
        "SELECT settings_json_mtime, additional_dirs FROM global_mirror WHERE id = 1"
    ).fetchone()
    assert row[0] == new_on_disk_mtime, "cache mtime not updated after self-heal"
    assert json.loads(row[1]) == ["/new/dir"], "cache dirs not updated after self-heal"


# ---------------------------------------------------------------------------
# Behavior demo (regression guard): external edit invalidates cache on next read
# ---------------------------------------------------------------------------


def test_external_edit_invalidates_cache_on_next_read(db, settings_file):
    """Regression guard: external write to settings.json is picked up by the next read.

    Sequence:
    1. Write an initial settings.json and call get_additional_dirs — cache populated.
    2. Externally append a new entry (write_text advances mtime).
    3. Call get_additional_dirs again — must return the new entry, cache must
       reflect the advanced mtime and the new state.
    """
    # Step 1: initial sync — cache populated.
    _write_settings(settings_file, ["/original/dir"])
    _seed_global(db, str(settings_file), None, None)

    first_result = get_additional_dirs(db, Scope("global_mirror", 1))
    assert first_result == ["/original/dir"]

    cached_mtime_after_first = db.execute(
        "SELECT settings_json_mtime FROM global_mirror WHERE id = 1"
    ).fetchone()[0]
    assert cached_mtime_after_first is not None

    # Step 2: external edit — write_text advances mtime naturally.
    _write_settings(settings_file, ["/original/dir", "/new/dir"])
    new_on_disk_mtime = settings_file.stat().st_mtime

    # Ensure mtime actually advanced (write_text on a fast filesystem may be
    # intra-second; if the OS rounds to-second the test would be a false hit.
    # Force an advance by touching the mtime one second forward when needed).
    if new_on_disk_mtime == cached_mtime_after_first:
        import os

        os.utime(settings_file, (new_on_disk_mtime + 1.0, new_on_disk_mtime + 1.0))
        new_on_disk_mtime = settings_file.stat().st_mtime

    # Step 3: next read must pick up the new entry.
    second_result = get_additional_dirs(db, Scope("global_mirror", 1))
    assert "/new/dir" in second_result, (
        "get_additional_dirs did not pick up externally-added entry after mtime advanced"
    )
    assert "/original/dir" in second_result

    # Cache mtime must have advanced to match the new file mtime.
    cached_mtime_after_second = db.execute(
        "SELECT settings_json_mtime FROM global_mirror WHERE id = 1"
    ).fetchone()[0]
    assert cached_mtime_after_second == new_on_disk_mtime, (
        "cache mtime was not updated after slow-path re-parse"
    )

    # Cache JSON must now reflect the new state.
    cached_dirs_after_second = json.loads(
        db.execute("SELECT additional_dirs FROM global_mirror WHERE id = 1").fetchone()[
            0
        ]
    )
    assert cached_dirs_after_second == ["/original/dir", "/new/dir"]


# ---------------------------------------------------------------------------
# OSError variants → [], cache unchanged
# ---------------------------------------------------------------------------


def test_permission_error_on_stat_returns_empty(db, settings_file, monkeypatch):
    """PermissionError from Path.stat() returns [] without crashing."""
    _write_settings(settings_file, ["/some/dir"])
    good_cache = json.dumps(["/cached/dir"])
    _seed_global(db, str(settings_file), 999.0, good_cache)

    monkeypatch.setattr(
        Path, "stat", lambda self: (_ for _ in ()).throw(PermissionError("denied"))
    )

    assert get_additional_dirs(db, Scope("global_mirror", 1)) == []
    # Cache must remain untouched — stat failure is not a parse failure.
    _assert_global_cache_unchanged(db, 999.0, good_cache)


def test_permission_error_on_read_returns_empty_and_leaves_cache_unchanged(
    db, settings_file, monkeypatch
):
    """PermissionError from Path.read_bytes() on slow path returns [] and leaves cache intact."""
    _write_settings(settings_file, ["/some/dir"])
    on_disk_mtime = settings_file.stat().st_mtime
    good_cache = json.dumps(["/previous/good"])
    # Seed with stale mtime so slow path fires.
    _seed_global(db, str(settings_file), on_disk_mtime - 1.0, good_cache)

    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda self: (_ for _ in ()).throw(PermissionError("denied")),
    )

    assert get_additional_dirs(db, Scope("global_mirror", 1)) == []
    _assert_global_cache_unchanged(db, on_disk_mtime - 1.0, good_cache)


# ---------------------------------------------------------------------------
# Scope validation
# ---------------------------------------------------------------------------


def test_invalid_scope_table_raises():
    """Scope with an unrecognised table name must raise ValueError immediately."""
    with pytest.raises(ValueError, match="not_a_table"):
        Scope("not_a_table", 1)


# ---------------------------------------------------------------------------
# Sessions scope — per-session extra_dirs
# ---------------------------------------------------------------------------


def _seed_session(conn, uuid: str, extra_dirs_json: str = "[]") -> int:
    """Insert a session row with the given extra_dirs JSON, return its id."""
    cur = conn.execute(
        "INSERT INTO sessions"
        " (session_uuid, project_id, started_at, last_activity, extra_dirs)"
        " VALUES (?, NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', ?)",
        (uuid, extra_dirs_json),
    )
    conn.commit()
    return cur.lastrowid


def test_sessions_scope_constructs_without_raising():
    """Scope('sessions', N) is a valid construction (sessions reader extension)."""
    s = Scope("sessions", 42)
    assert s.table == "sessions"
    assert s.id == 42


def test_sessions_scope_absent_row_returns_empty(db):
    """No row for the session id returns [], not raises."""
    result = get_additional_dirs(db, Scope("sessions", 999))
    assert result == []


def test_sessions_scope_default_extra_dirs_returns_empty(db):
    """A session row with the default '[]' extra_dirs returns []."""
    sid = _seed_session(db, "test-uuid-default")
    assert get_additional_dirs(db, Scope("sessions", sid)) == []


def test_sessions_scope_populated_returns_list(db):
    """A session row with populated extra_dirs returns the parsed list."""
    sid = _seed_session(
        db,
        "test-uuid-populated",
        json.dumps(["/tmp/a", "/var/tmp"]),
    )
    assert get_additional_dirs(db, Scope("sessions", sid)) == ["/tmp/a", "/var/tmp"]


def test_sessions_scope_malformed_json_returns_empty(db):
    """Malformed JSON in extra_dirs returns [] without crashing."""
    sid = _seed_session(db, "test-uuid-malformed", "{not valid json")
    assert get_additional_dirs(db, Scope("sessions", sid)) == []


def test_sessions_scope_does_not_touch_settings_files(db, tmp_path):
    """Sessions branch must NOT read any settings.json — pure column SELECT.

    Sets up a session with extra_dirs but no settings.json reference; the
    function must succeed regardless of whether any settings file exists.
    """
    sid = _seed_session(db, "test-uuid-no-settings", json.dumps(["/some/path"]))
    assert get_additional_dirs(db, Scope("sessions", sid)) == ["/some/path"]
