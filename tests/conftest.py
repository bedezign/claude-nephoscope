"""Shared pytest fixtures for permission-learner tests.

Adds ``/home/steve/.claude/observability`` to sys.path so tests can import
``learners.permission.*`` regardless of where pytest is invoked from, and
provides a ``tmp_db`` fixture that applies every schema migration found
under ``lib/schema/`` to an isolated SQLite database. New ``vN.sql``
files are picked up automatically — the fixture delegates discovery to
``lib.db._migrate``.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

OBS_ROOT = Path("/home/steve/.claude/observability")
if str(OBS_ROOT) not in sys.path:
    sys.path.insert(0, str(OBS_ROOT))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """An isolated observations DB with all ``lib/schema/v*.sql`` applied.

    Sets ``OBSERVABILITY_DB`` so any ``lib.db``-backed call in the code
    under test reaches this fixture's file.
    """
    db_path = tmp_path / "observations.db"
    monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

    # Force lib.db to re-resolve DB_PATH from the patched env. It captures
    # the value at import time, so we reload if it's already been imported.
    import importlib

    import lib.db as db_module

    importlib.reload(db_module)

    conn = db_module._open()
    db_module._migrate(conn)
    try:
        yield conn
    finally:
        conn.close()
