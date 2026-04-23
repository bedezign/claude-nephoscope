"""Tests for lookup-table seed rows in ``schema.sql``.

The conftest ``tmp_db`` fixture also inserts these seeds, which hid the fact
that production bootstraps (which apply ``schema.sql`` directly, without the
fixture) were leaving ``permission_modes`` and ``call_statuses`` empty. These
tests bootstrap a fresh DB from ``schema.sql`` alone — no fixture — and
assert the canonical rows are present.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

SCHEMA_PATH = (
    Path(__file__).parent.parent.parent / "src" / "nephoscope" / "lib" / "schema.sql"
)

EXPECTED_PERMISSION_MODES = {
    "default",
    "acceptEdits",
    "bypassPermissions",
    "plan",
    "auto",
}

EXPECTED_CALL_STATUSES = {
    "pending",
    "ok",
    "err",
    "denied",
    "orphan",
}


@pytest.fixture
def raw_schema_db():
    """A fresh DB with only ``schema.sql`` applied — no fixture seeding."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        with sqlite3.connect(db_path) as conn:
            conn.executescript(SCHEMA_PATH.read_text())
            conn.commit()
        yield db_path


def test_permission_modes_seeded_from_schema(raw_schema_db):
    """All canonical permission_modes rows are present after applying schema.sql."""
    with sqlite3.connect(raw_schema_db) as conn:
        rows = {name for (name,) in conn.execute("SELECT name FROM permission_modes")}
    assert rows == EXPECTED_PERMISSION_MODES


def test_call_statuses_seeded_from_schema(raw_schema_db):
    """All canonical call_statuses rows are present after applying schema.sql."""
    with sqlite3.connect(raw_schema_db) as conn:
        rows = {name for (name,) in conn.execute("SELECT name FROM call_statuses")}
    assert rows == EXPECTED_CALL_STATUSES


def test_seed_inserts_are_idempotent_on_rerun(raw_schema_db):
    """Re-running the seed ``INSERT OR IGNORE`` statements keeps row count constant.

    Scope: the seed block alone, not a full ``schema.sql`` re-apply. The DDL
    statements upstream of the seed block are bare ``CREATE TABLE`` (no
    ``IF NOT EXISTS``) and production never re-applies ``schema.sql`` against an
    existing file — the recorder's lazy bootstrap is gated on DB-file absence.
    """
    with sqlite3.connect(raw_schema_db) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO permission_modes (name) VALUES "
            "('default'),('acceptEdits'),('bypassPermissions'),('plan'),('auto')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO call_statuses (name) VALUES "
            "('pending'),('ok'),('err'),('denied'),('orphan')"
        )
        conn.commit()

        (pm_count,) = conn.execute("SELECT COUNT(*) FROM permission_modes").fetchone()
        (cs_count,) = conn.execute("SELECT COUNT(*) FROM call_statuses").fetchone()

    assert pm_count == len(EXPECTED_PERMISSION_MODES)
    assert cs_count == len(EXPECTED_CALL_STATUSES)
