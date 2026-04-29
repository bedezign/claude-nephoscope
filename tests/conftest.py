"""Shared pytest configuration for nephoscope tests.

Adds the project ``src/`` root to ``sys.path`` so tests can import
``nephoscope.learners.permission.*``, ``nephoscope.lib.*``, and
``nephoscope.recorder.*`` regardless of where pytest is invoked from — and
regardless of whether the package has been ``pip install -e``'d. Anchored to
``__file__`` so tests always target the real code tree.

Provides a ``tmp_db`` fixture that applies ``nephoscope/lib/schema.sql`` to an
isolated SQLite database.  Fresh DBs start at the current schema version;
migration tests that need an older schema shape build their own DB directly.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


_LIVE_DB = Path.home() / ".cache" / "claude" / "observability" / "observations.db"


def pytest_configure(config):
    """Record the live DB's sha256 at collection time so the isolation canary
    (``tests/test_live_db_isolation.py``) can detect any write that happens
    between collection and the canary test."""
    if _LIVE_DB.exists():
        h = hashlib.sha256()
        with _LIVE_DB.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        os.environ["_LIVE_DB_SHA_AT_COLLECT"] = h.hexdigest()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """An isolated observations DB with the current schema applied.

    ``lib.db._db_path()`` reads ``OBSERVABILITY_DB`` on each call, so the
    ``monkeypatch.setenv`` below is sufficient — there is no cached module
    global to patch.
    """
    db_path = tmp_path / "observations.db"
    monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

    schema_sql = (SRC_ROOT / "nephoscope" / "lib" / "schema.sql").read_text()
    conn = sqlite3.connect(str(db_path))
    conn.executescript(schema_sql)
    conn.execute(
        "INSERT OR IGNORE INTO permission_modes (name) VALUES ('default'),('acceptEdits'),('bypassPermissions'),('plan'),('auto')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO call_statuses (name) VALUES ('pending'),('ok'),('err'),('denied'),('orphan')"
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()
