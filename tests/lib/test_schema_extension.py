"""Tests for Phase 8.5 schema extension: projects mirror columns + global_mirror singleton table."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def fresh_db():
    """Create a fresh sandbox DB with the extended schema."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        schema_path = Path(__file__).parent.parent.parent / "lib" / "schema.sql"

        # Apply schema.
        with sqlite3.connect(db_path) as conn:
            with open(schema_path) as f:
                conn.executescript(f.read())
            # Seed global_mirror.
            conn.execute(
                "INSERT OR IGNORE INTO global_mirror (id, settings_json_path, settings_json_sha256, settings_json_last_synced) "
                "VALUES (1, '~/.claude/settings.json', NULL, NULL)"
            )
            conn.commit()

        yield db_path


def test_projects_table_has_settings_json_path_column(fresh_db):
    """Verify projects.settings_json_path column exists."""
    with sqlite3.connect(fresh_db) as conn:
        cursor = conn.execute("PRAGMA table_info(projects)")
        cols = {row[1] for row in cursor}
        assert "settings_json_path" in cols


def test_projects_table_has_settings_json_sha256_column(fresh_db):
    """Verify projects.settings_json_sha256 column exists."""
    with sqlite3.connect(fresh_db) as conn:
        cursor = conn.execute("PRAGMA table_info(projects)")
        cols = {row[1] for row in cursor}
        assert "settings_json_sha256" in cols


def test_projects_table_has_settings_json_last_synced_column(fresh_db):
    """Verify projects.settings_json_last_synced column exists."""
    with sqlite3.connect(fresh_db) as conn:
        cursor = conn.execute("PRAGMA table_info(projects)")
        cols = {row[1] for row in cursor}
        assert "settings_json_last_synced" in cols


def test_global_mirror_table_exists(fresh_db):
    """Verify global_mirror table exists."""
    with sqlite3.connect(fresh_db) as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='global_mirror'"
        )
        assert cursor.fetchone() is not None


def test_global_mirror_singleton_row_exists(fresh_db):
    """Verify global_mirror singleton row (id=1) exists with correct data."""
    with sqlite3.connect(fresh_db) as conn:
        cursor = conn.execute(
            "SELECT id, settings_json_path, settings_json_sha256, settings_json_last_synced "
            "FROM global_mirror WHERE id = 1"
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == 1  # id
        assert row[1] == "~/.claude/settings.json"  # settings_json_path
        assert row[2] is None  # settings_json_sha256
        assert row[3] is None  # settings_json_last_synced


def test_global_mirror_check_constraint_blocks_second_row(fresh_db):
    """Verify CHECK (id = 1) constraint prevents inserting id=2."""
    with sqlite3.connect(fresh_db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO global_mirror (id, settings_json_path) "
                "VALUES (2, '/some/path')"
            )
            conn.commit()


def test_global_mirror_allows_updating_singleton(fresh_db):
    """Verify the singleton row can be updated (hash, last_synced, etc.)."""
    with sqlite3.connect(fresh_db) as conn:
        # Update hash and last_synced on the singleton.
        conn.execute(
            "UPDATE global_mirror SET settings_json_sha256 = 'abc123', "
            "settings_json_last_synced = '2026-04-21T10:00:00Z' WHERE id = 1"
        )
        conn.commit()

        # Verify the update took.
        cursor = conn.execute(
            "SELECT settings_json_sha256, settings_json_last_synced FROM global_mirror WHERE id = 1"
        )
        row = cursor.fetchone()
        assert row[0] == "abc123"
        assert row[1] == "2026-04-21T10:00:00Z"
