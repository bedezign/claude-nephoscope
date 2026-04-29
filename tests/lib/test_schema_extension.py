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


# ===========================================================================
# PRAGMA user_version migration runner
# ===========================================================================


def test_fresh_db_has_user_version_2(tmp_path, monkeypatch):
    """A DB opened via _open() on a brand-new file starts at user_version 2."""
    import nephoscope.lib.db as db_module

    db_path = tmp_path / "fresh_v2.db"
    monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

    conn = db_module._open()
    try:
        version = conn.execute("PRAGMA user_version;").fetchone()[0]
        assert version == 2, f"Expected user_version=2 on fresh DB, got {version}"
    finally:
        conn.close()


def test_v0_db_advanced_to_v2_on_open(tmp_path, monkeypatch):
    """A v0 DB (context column present, user_version=0) is advanced to v2 on _open().

    Simulates an old install where: the context column was already applied by the
    old _ensure_rule_shapes_context shim, permission_ask_pending is in its v1 shape
    (no audit-trail columns), and user_version=0.  After _open(): stamps to 1, then
    the v1→v2 migration adds the three audit columns.
    """
    import nephoscope.lib.db as db_module

    schema_path = (
        Path(__file__).parent.parent.parent
        / "src"
        / "nephoscope"
        / "lib"
        / "schema.sql"
    )
    db_path = tmp_path / "v0.db"
    monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

    # Build the DB from current schema, then recreate permission_ask_pending in its
    # v1 shape (no audit-trail columns) to faithfully simulate an old install, and
    # force user_version back to 0.
    with sqlite3.connect(db_path) as setup_conn:
        setup_conn.executescript(schema_path.read_text())
        setup_conn.executescript("""
            PRAGMA foreign_keys = OFF;
            DROP TABLE permission_ask_pending;
            CREATE TABLE permission_ask_pending (
              tool_use_id TEXT    PRIMARY KEY,
              session_id  INTEGER NOT NULL,
              verb        TEXT    NOT NULL,
              subcommand  TEXT,
              flags       TEXT    NOT NULL,
              asked_at    TEXT    NOT NULL
            );
            PRAGMA foreign_keys = ON;
            PRAGMA user_version = 0;
        """)

    # Verify the precondition.
    with sqlite3.connect(db_path) as pre_conn:
        pre_version = pre_conn.execute("PRAGMA user_version;").fetchone()[0]
        assert pre_version == 0, "Setup error: user_version should be 0 before _open()"
        assert "context" in _columns(pre_conn, "rule_shapes"), (
            "Setup error: context column must be present to simulate a shim-migrated DB"
        )
        assert "permission_mode" not in _columns(pre_conn, "permission_ask_pending"), (
            "Setup error: permission_mode should be absent in v1-shaped table"
        )

    conn = db_module._open()
    try:
        version = conn.execute("PRAGMA user_version;").fetchone()[0]
        assert version == 2, (
            f"Expected user_version=2 after _open() on v0 DB, got {version}"
        )
        assert "permission_mode" in _columns(conn, "permission_ask_pending"), (
            "v1→v2 migration must add permission_mode column"
        )
    finally:
        conn.close()


def test_migration_runner_applies_deltas_in_order(tmp_path, monkeypatch):
    """A fake v3 migration appended to _MIGRATIONS is applied to a v2 DB."""
    import nephoscope.lib.db as db_module

    db_path = tmp_path / "v2_to_v3.db"
    monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

    # Bootstrap a v2 DB (current baseline after all real migrations).
    conn = db_module._open()
    conn.close()

    # Append a fake migration to version 3 without replacing existing migrations.
    fake_migration = (3, "CREATE TABLE _test_migration (id INTEGER PRIMARY KEY);")
    monkeypatch.setattr(
        db_module, "_MIGRATIONS", db_module._MIGRATIONS + [fake_migration]
    )

    # Reopen — _apply_migrations should detect v2 < 3 and run the delta.
    conn2 = db_module._open()
    try:
        version = conn2.execute("PRAGMA user_version;").fetchone()[0]
        assert version == 3, (
            f"Expected user_version=3 after fake migration, got {version}"
        )
        tables = {
            row[0]
            for row in conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            )
        }
        assert "_test_migration" in tables, (
            "_test_migration table missing — migration delta was not applied"
        )
    finally:
        conn2.close()


def test_ensure_rule_shapes_context_removed():
    """_ensure_rule_shapes_context must not exist in nephoscope.lib.db (regression guard)."""
    import nephoscope.lib.db as db_module

    assert not hasattr(db_module, "_ensure_rule_shapes_context"), (
        "_ensure_rule_shapes_context still exists in nephoscope.lib.db; "
        "it must be deleted as part of the migration-runner implementation"
    )
