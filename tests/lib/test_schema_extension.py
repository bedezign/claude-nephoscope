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
        schema_path = (
            Path(__file__).parent.parent.parent
            / "src"
            / "nephoscope"
            / "lib"
            / "schema.sql"
        )

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


# ===========================================================================
# Phase 2 — rule_shapes.context column + idempotent migration
# ===========================================================================


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _index_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[1]
        for row in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type='index'"
        )
    }


def test_fresh_db_has_rule_shapes_context_column(fresh_db):
    """A DB created from the current schema.sql has rule_shapes.context."""
    with sqlite3.connect(fresh_db) as conn:
        assert "context" in _columns(conn, "rule_shapes"), (
            "rule_shapes.context column missing from fresh schema"
        )


def test_fresh_db_context_default_is_any(fresh_db):
    """Default value of rule_shapes.context is 'any'."""
    with sqlite3.connect(fresh_db) as conn:
        conn.execute(
            "INSERT INTO rule_shapes (verb, subcommand, flags, first_seen, last_seen)"
            " VALUES ('git', NULL, '[]', '2025-01-01Z', '2025-01-01Z');"
        )
        row = conn.execute(
            "SELECT context FROM rule_shapes WHERE verb='git';"
        ).fetchone()
        assert row is not None
        assert row[0] == "any", f"Expected default context='any', got {row[0]!r}"


def test_fresh_db_context_unique_index_includes_context(fresh_db):
    """The unique index on rule_shapes includes the context column."""
    with sqlite3.connect(fresh_db) as conn:
        # Inserting two rows with same (v,s,f,p) but different context must succeed.
        conn.execute(
            "INSERT INTO rule_shapes (verb, subcommand, flags, path_spec, context, first_seen, last_seen)"
            " VALUES ('op', 'read', '*', NULL, 'any', '2025-01-01Z', '2025-01-01Z');"
        )
        conn.execute(
            "INSERT INTO rule_shapes (verb, subcommand, flags, path_spec, context, first_seen, last_seen)"
            " VALUES ('op', 'read', '*', NULL, 'toplevel', '2025-01-01Z', '2025-01-01Z');"
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM rule_shapes WHERE verb='op';"
        ).fetchone()[0]
        assert count == 2, (
            f"Expected 2 rows for different contexts, got {count} "
            "(unique index may not include context)"
        )


def test_idempotent_migration_fresh_db(tmp_path, monkeypatch):
    """_open() on a fresh DB produces the context column without error."""
    import nephoscope.lib.db as db_module

    db_path = tmp_path / "fresh.db"
    monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

    conn = db_module._open()
    try:
        assert "context" in _columns(conn, "rule_shapes"), (
            "rule_shapes.context missing after _open() on fresh DB"
        )
    finally:
        conn.close()


def test_idempotent_migration_legacy_db(tmp_path, monkeypatch):
    """_open() on an old-schema DB (no context column) adds the column idempotently.

    Simulates a pre-Phase-2 install by creating the DB manually without the
    context column, then calling _open() and verifying the column is present.
    """
    import nephoscope.lib.db as db_module

    schema_path = (
        Path(__file__).parent.parent.parent
        / "src"
        / "nephoscope"
        / "lib"
        / "schema.sql"
    )
    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

    # Build a "legacy" DB: apply schema but immediately drop the context column
    # (SQLite doesn't support DROP COLUMN before 3.35; we use a two-step workaround
    # via CREATE TABLE + INSERT SELECT + DROP + RENAME — but actually SQLite 3.35+
    # does support it, and the test env is 3.40+).
    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema_path.read_text())
        # Drop the context column to simulate a pre-migration install.
        try:
            conn.execute("ALTER TABLE rule_shapes DROP COLUMN context;")
        except sqlite3.OperationalError:
            # Fallback: rebuild the table without context.
            conn.execute("ALTER TABLE rule_shapes RENAME TO rule_shapes_old;")
            conn.execute(
                "CREATE TABLE rule_shapes ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  verb TEXT NOT NULL,"
                "  subcommand TEXT,"
                "  flags TEXT NOT NULL,"
                "  path_spec TEXT,"
                "  first_seen TEXT NOT NULL,"
                "  last_seen TEXT NOT NULL"
                ");"
            )
            conn.execute(
                "INSERT INTO rule_shapes (id, verb, subcommand, flags, path_spec, first_seen, last_seen)"
                " SELECT id, verb, subcommand, flags, path_spec, first_seen, last_seen"
                " FROM rule_shapes_old;"
            )
            conn.execute("DROP TABLE rule_shapes_old;")

    # Verify context column is absent before migration.
    with sqlite3.connect(db_path) as pre_conn:
        assert "context" not in _columns(pre_conn, "rule_shapes"), (
            "Setup error: context column still present after drop — test is invalid"
        )

    # Now call _open() which should run the idempotent migration.
    conn2 = db_module._open()
    try:
        assert "context" in _columns(conn2, "rule_shapes"), (
            "rule_shapes.context missing after _open() on legacy DB — migration did not run"
        )
    finally:
        conn2.close()


def test_idempotent_migration_already_migrated(tmp_path, monkeypatch):
    """Calling _open() twice on an already-migrated DB does not error or duplicate columns."""
    import nephoscope.lib.db as db_module

    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

    # First open — creates schema.
    conn1 = db_module._open()
    conn1.close()

    # Second open — migration is a no-op (column already exists).
    conn2 = db_module._open()
    try:
        cols = _columns(conn2, "rule_shapes")
        context_count = sum(1 for c in cols if c == "context")
        assert context_count == 1, (
            f"Expected exactly one 'context' column, got {context_count} "
            "(double migration may have duplicated the column)"
        )
    finally:
        conn2.close()
