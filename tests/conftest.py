"""Shared pytest configuration for nephoscope tests.

Adds the project ``src/`` root to ``sys.path`` so tests can import
``nephoscope.learners.permission.*``, ``nephoscope.lib.*``, and
``nephoscope.recorder.*`` regardless of where pytest is invoked from — and
regardless of whether the package has been ``pip install -e``'d. Anchored to
``__file__`` so tests always target the real code tree.

Provides a ``tmp_db`` fixture that applies ``nephoscope/lib/schema.sql`` to an
isolated SQLite database.  Fresh DBs start at the current schema version;
migration tests that need an older schema shape build their own DB directly.

Provides a global ``patch_verb_categories`` autouse fixture so all tests that
exercise ``parse_command`` see a full verb category set without requiring a DB.
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


def _canonical_verb_categories() -> dict:
    """Full verb category dict matching the combined profile fixtures."""
    return {
        "content_verb": frozenset(
            {
                "echo",
                "printf",
                "cat",
                "head",
                "tail",
                "grep",
                "egrep",
                "fgrep",
                "zgrep",
                "wc",
                "sort",
                "uniq",
                "tr",
                "cut",
                "tac",
                "paste",
                "sed",
                "awk",
                "ls",
                "find",
                "ps",
                "df",
                "du",
                "free",
                "pwd",
                "stat",
                "file",
                "readlink",
                "realpath",
                "which",
                "type",
                "command",
                "whereis",
                "basename",
                "dirname",
                "date",
                "uname",
                "uptime",
                "whoami",
                "hostname",
                "id",
                "groups",
                "rm",
                "mv",
                "cp",
                "ln",
                "touch",
                "mkdir",
                "rmdir",
                "chmod",
                "chown",
                "chgrp",
                "sqlite3",
                "cd",
            }
        ),
        "script_runner": frozenset({"python3", "python", "bash", "sh", "node", "deno"}),
        "task_runner_pairs": {
            ("npm", "run"): None,
            ("pnpm", "run"): None,
            ("yarn", "run"): None,
            ("pdm", "run"): None,
            ("uv", "run"): None,
            ("cargo", "run"): None,
            ("make",): None,
            ("just",): None,
        },
        "two_word_subcommand": {
            ("vault", "kv"): None,
            ("vault", "auth"): None,
            ("vault", "secrets"): None,
            ("doppler", "secrets"): None,
        },
    }


_LIVE_DB = Path.home() / ".cache" / "claude" / "observability" / "observations.db"


@pytest.fixture(autouse=True)
def patch_verb_categories(monkeypatch):
    """Patch _load_verb_categories globally so tests work without a live DB."""
    from nephoscope.learners.permission.canonicalize import _load_verb_categories

    cats = _canonical_verb_categories()
    monkeypatch.setattr(
        "nephoscope.learners.permission.canonicalize._load_verb_categories",
        lambda: cats,
    )
    _load_verb_categories.cache_clear()
    yield
    _load_verb_categories.cache_clear()


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
        try:
            from nephoscope.learners.permission.canonicalize import (
                _load_verb_categories,
            )

            _load_verb_categories.cache_clear()
        except Exception:
            pass
