"""Integration tests: seed → mirror → promote → tamper → MirrorHashMismatch.

All writes go to tmp_path. Zero tolerance for writes to real paths
(~/.claude/settings.json, ~/.cache/claude/observability/, etc.).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def mirror_db(tmp_path):
    """Isolated DB with schema + global_mirror pointing to tmp_path/settings.json.

    Returns (conn, fake_settings_path).  Connection is in autocommit mode
    (isolation_level=None) so every write is immediately visible without an
    explicit commit — mirrors how the production writer uses the connection.
    """
    db_path = tmp_path / "observations.db"
    fake_settings = tmp_path / "settings.json"

    schema_sql = (PROJECT_ROOT / "src" / "nephoscope" / "lib" / "schema.sql").read_text()
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript(schema_sql)
    conn.execute(
        "INSERT OR IGNORE INTO permission_modes (name)"
        " VALUES ('default'),('acceptEdits'),('bypassPermissions'),('plan'),('auto');"
    )
    conn.execute(
        "INSERT OR IGNORE INTO call_statuses (name)"
        " VALUES ('pending'),('ok'),('err'),('denied'),('orphan');"
    )
    # Register the global_mirror singleton pointing at the fake settings file.
    conn.execute(
        "INSERT INTO global_mirror"
        " (id, settings_json_path, settings_json_sha256, settings_json_last_synced)"
        " VALUES (1, ?, NULL, NULL);",
        (str(fake_settings),),
    )
    try:
        yield conn, fake_settings
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scenario 1: seed → mirror created, hash stamped
# ---------------------------------------------------------------------------


def test_seed_creates_mirror_and_stamps_hash(mirror_db, tmp_path):
    """Applying a seed fixture creates settings.json and stamps the DB hash.

    Covers the full path: apply_fixtures → sync_global → atomic write →
    hash stamp.  No real ~/.claude/settings.json is touched.
    """
    conn, fake_settings = mirror_db

    fixture_path = tmp_path / "seed.yaml"
    fixture_path.write_text(
        yaml.dump(
            [
                {"verb": "Read", "flags": [], "decision": "approved"},
                {"verb": "Write", "flags": [], "decision": "approved"},
            ]
        )
    )

    from nephoscope.learners.permission.seed import apply_fixtures

    _shapes, perms_created = apply_fixtures(conn, fixture_path)

    # DB rows were created.
    assert perms_created == 2

    # Mirror file must exist now.
    assert fake_settings.exists(), "mirror file was not created by apply_fixtures"

    # Mirror content must be valid JSON with the expected allow entries.
    data = json.loads(fake_settings.read_bytes())
    allow = data["permissions"]["allow"]
    assert "Read" in allow, f"'Read' missing from allow list: {allow}"
    assert "Write" in allow, f"'Write' missing from allow list: {allow}"

    # Hash must be stamped in the DB (non-NULL).
    row = conn.execute(
        "SELECT settings_json_sha256 FROM global_mirror WHERE id = 1;"
    ).fetchone()
    assert row is not None
    assert row[0] is not None, "settings_json_sha256 not stamped after seed"


# ---------------------------------------------------------------------------
# Scenario 2: promote → mirror updated, hash re-stamped
# ---------------------------------------------------------------------------


def test_promote_updates_mirror_and_restamps_hash(mirror_db, tmp_path):
    """Adding a new rule via the promote path updates the mirror and changes the hash.

    Flow:
    1. Seed one rule (Read approved).
    2. Record the post-seed hash.
    3. Promote a second rule (Bash ls *) using direct DB helpers + sync_affected.
    4. Assert: new rule appears in allow list; hash changed.
    """
    conn, fake_settings = mirror_db

    # Step 1 — seed.
    fixture_path = tmp_path / "seed.yaml"
    fixture_path.write_text(
        yaml.dump([{"verb": "Read", "flags": [], "decision": "approved"}])
    )

    from nephoscope.learners.permission.seed import apply_fixtures

    apply_fixtures(conn, fixture_path)

    # Step 2 — record post-seed hash.
    post_seed_hash = conn.execute(
        "SELECT settings_json_sha256 FROM global_mirror WHERE id = 1;"
    ).fetchone()[0]
    assert post_seed_hash is not None, "post-seed hash should be stamped"

    # Step 3 — promote a new rule directly (mirrors _cmd_promote logic).
    from nephoscope.lib.db import _now, insert_permission, upsert_rule_shape
    from nephoscope.lib.mirror.writer import sync_affected

    now = _now()
    flags_json = "*"  # wildcard → Bash(ls *)
    shape_id = upsert_rule_shape(conn, "ls", None, flags_json, None, now)
    perm_id = insert_permission(conn, shape_id, None, None, "approved", "learner", now)
    sync_affected(conn, perm_id)

    # Step 4 — assertions.
    data = json.loads(fake_settings.read_bytes())
    allow = data["permissions"]["allow"]
    assert "Read" in allow, f"seeded 'Read' disappeared after promote: {allow}"
    assert "Bash(ls *)" in allow, f"promoted 'Bash(ls *)' not in allow list: {allow}"

    post_promote_hash = conn.execute(
        "SELECT settings_json_sha256 FROM global_mirror WHERE id = 1;"
    ).fetchone()[0]
    assert post_promote_hash != post_seed_hash, "hash was not re-stamped after promote"


# ---------------------------------------------------------------------------
# Scenario 3: tamper → MirrorHashMismatch on next sync
# ---------------------------------------------------------------------------


def test_tamper_raises_mirror_hash_mismatch(mirror_db, tmp_path):
    """Editing settings.json externally causes MirrorHashMismatch on next write.

    Flow:
    1. Seed one rule so the mirror is written and the hash is stamped.
    2. Tamper with settings.json directly (bypass the writer).
    3. Attempt sync_global — must raise MirrorHashMismatch with the fake path.
    """
    conn, fake_settings = mirror_db

    # Step 1 — seed to establish the mirror and stamp the hash.
    fixture_path = tmp_path / "seed.yaml"
    fixture_path.write_text(
        yaml.dump([{"verb": "Read", "flags": [], "decision": "approved"}])
    )

    from nephoscope.learners.permission.seed import apply_fixtures

    apply_fixtures(conn, fixture_path)

    assert fake_settings.exists(), "precondition: mirror must exist after seed"
    stored_hash = conn.execute(
        "SELECT settings_json_sha256 FROM global_mirror WHERE id = 1;"
    ).fetchone()[0]
    assert stored_hash is not None, "precondition: hash must be stamped after seed"

    # Step 2 — tamper: write junk directly without going through the writer.
    fake_settings.write_text('{"tampered": true}')

    # Step 3 — attempt sync; must raise MirrorHashMismatch.
    from nephoscope.lib.mirror.writer import MirrorHashMismatch, sync_global

    with pytest.raises(MirrorHashMismatch) as exc_info:
        sync_global(conn)

    error_message = str(exc_info.value)
    assert str(fake_settings) in error_message, (
        f"MirrorHashMismatch message should contain the fake path.\n"
        f"  path: {fake_settings}\n"
        f"  message: {error_message}"
    )
