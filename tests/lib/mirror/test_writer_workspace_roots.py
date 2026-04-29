"""Tests for workspace-root Write/Edit/Read entries in the global mirror.

Entries generated from config.workspace_roots are merged into
permissions.allow in the global settings.json.  A companion top-level key
``_nephoscopeAllowedTools`` records what we generated so re-syncs can replace
old entries without accumulating them.

These tests exercise only the global sync path (project_id is None).
Workspace-root entries are derived from global config and are meaningless at
the project level.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from nephoscope.config import NephoscopeConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCHEMA_PATH = PROJECT_ROOT / "src" / "nephoscope" / "lib" / "schema.sql"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_conn(tmp_path: Path):
    """Isolated SQLite DB seeded with schema + global_mirror singleton."""
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
    return None


def _cfg(trusted_dirs: list[str]) -> NephoscopeConfig:
    return NephoscopeConfig(trusted_dirs=trusted_dirs)


def _target_path(db_conn: sqlite3.Connection) -> Path:
    return Path(
        db_conn.execute(
            "SELECT settings_json_path FROM global_mirror WHERE id = 1;"
        ).fetchone()[0]
    )


# ---------------------------------------------------------------------------
# 0. _generate_workspace_entries unit-level: empty input returns empty list
# ---------------------------------------------------------------------------


def test_generate_workspace_entries_empty_list_returns_empty():
    """Calling _generate_workspace_entries with an empty list must return []
    without errors — no entries are generated for zero trusted dirs."""
    from nephoscope.lib.mirror.writer import _generate_workspace_entries

    result = _generate_workspace_entries([])

    assert result == []


# ---------------------------------------------------------------------------
# 1. Empty workspace_roots produces no generated entries
# ---------------------------------------------------------------------------


def test_empty_workspace_roots_produces_no_generated_entries(tmp_path, db_conn):
    """When workspace_roots is [], sync writes no Write/Edit/Read path entries
    and does not create the _nephoscopeAllowedTools marker key."""
    from nephoscope.lib.mirror.writer import sync_global

    with (
        patch(
            "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
        ),
        patch("nephoscope.lib.mirror.writer.get_config", return_value=_cfg([])),
    ):
        sync_global(db_conn)

    data = json.loads(_target_path(db_conn).read_bytes())
    assert data["permissions"]["allow"] == []
    assert "_nephoscopeAllowedTools" not in data


# ---------------------------------------------------------------------------
# 2. Single workspace root generates three entries
# ---------------------------------------------------------------------------


def test_single_workspace_root_generates_three_entries(tmp_path, db_conn):
    """A single workspace root /tmp/myproject produces three allow entries:
    Write, Edit, and Read for that path glob."""
    from nephoscope.lib.mirror.writer import sync_global

    with (
        patch(
            "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
        ),
        patch(
            "nephoscope.lib.mirror.writer.get_config",
            return_value=_cfg(["/tmp/myproject"]),
        ),
    ):
        sync_global(db_conn)

    data = json.loads(_target_path(db_conn).read_bytes())
    allow = data["permissions"]["allow"]
    assert "Write(/tmp/myproject/**)" in allow
    assert "Edit(/tmp/myproject/**)" in allow
    assert "Read(/tmp/myproject/**)" in allow


# ---------------------------------------------------------------------------
# 3. Tilde is expanded and realpath'd
# ---------------------------------------------------------------------------


def test_tilde_expanded_and_realpathd(tmp_path, db_conn):
    """workspace_roots entries containing ~ are expanded via expanduser + realpath
    before being written into the allow entries."""
    from nephoscope.lib.mirror.writer import sync_global

    expected = os.path.realpath(os.path.expanduser("~/projects"))

    with (
        patch(
            "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
        ),
        patch(
            "nephoscope.lib.mirror.writer.get_config",
            return_value=_cfg(["~/projects"]),
        ),
    ):
        sync_global(db_conn)

    data = json.loads(_target_path(db_conn).read_bytes())
    allow = data["permissions"]["allow"]
    assert f"Write({expected}/**)" in allow
    assert f"Edit({expected}/**)" in allow
    assert f"Read({expected}/**)" in allow


# ---------------------------------------------------------------------------
# 4. Re-sync replaces old entries (no accumulation)
# ---------------------------------------------------------------------------


def test_resync_replaces_old_entries(tmp_path, db_conn):
    """A second sync with different workspace_roots replaces the first sync's
    entries.  The old path must not appear; the new path must."""
    from nephoscope.lib.mirror.writer import sync_global

    with (
        patch(
            "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
        ),
        patch(
            "nephoscope.lib.mirror.writer.get_config",
            return_value=_cfg(["/old/path"]),
        ),
    ):
        sync_global(db_conn)

    # Verify first sync planted the entries.
    data_after_first = json.loads(_target_path(db_conn).read_bytes())
    assert "Write(/old/path/**)" in data_after_first["permissions"]["allow"]

    with (
        patch(
            "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
        ),
        patch(
            "nephoscope.lib.mirror.writer.get_config",
            return_value=_cfg(["/new/path"]),
        ),
    ):
        sync_global(db_conn)

    data_after_second = json.loads(_target_path(db_conn).read_bytes())
    allow = data_after_second["permissions"]["allow"]
    assert "Write(/new/path/**)" in allow
    assert "Edit(/new/path/**)" in allow
    assert "Read(/new/path/**)" in allow
    assert "Write(/old/path/**)" not in allow
    assert "Edit(/old/path/**)" not in allow
    assert "Read(/old/path/**)" not in allow


# ---------------------------------------------------------------------------
# 5. DB is authoritative for permissions.allow; pre-existing file entries
#    that the DB does not produce are replaced, not preserved.
# ---------------------------------------------------------------------------


def test_db_authoritative_preexisting_allow_entries_replaced(tmp_path, db_conn):
    """The DB is authoritative for permissions.allow.  Entries present in the
    on-disk file from a previous hand-edit that the DB does not produce are
    replaced on sync — only DB-derived entries and generated workspace-root
    entries appear in the final allow list."""
    from nephoscope.lib.mirror.writer import sync_global
    from nephoscope.lib.mirror.permissions_hash import settings_permissions_hash

    target = _target_path(db_conn)
    existing = {
        "permissions": {
            "allow": ["SomePreviousEntry(*)"],
            "deny": [],
            "ask": [],
        }
    }
    target.write_text(json.dumps(existing, indent=2))
    current_hash = settings_permissions_hash(target.read_bytes())
    db_conn.execute(
        "UPDATE global_mirror SET settings_json_sha256 = ? WHERE id = 1;",
        (current_hash,),
    )

    with (
        patch(
            "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
        ),
        patch(
            "nephoscope.lib.mirror.writer.get_config",
            return_value=_cfg(["/tmp/ws"]),
        ),
    ):
        sync_global(db_conn)

    data = json.loads(target.read_bytes())
    allow = data["permissions"]["allow"]
    assert "SomePreviousEntry(*)" not in allow
    assert "Write(/tmp/ws/**)" in allow
    assert "Edit(/tmp/ws/**)" in allow
    assert "Read(/tmp/ws/**)" in allow


# ---------------------------------------------------------------------------
# 6. Non-permissions keys (top-level) are preserved
# ---------------------------------------------------------------------------


def test_non_permissions_keys_preserved(tmp_path, db_conn):
    """Top-level keys like dontAskAboutTools or attribution survive a sync
    that injects workspace-root entries into permissions.allow."""
    from nephoscope.lib.mirror.writer import sync_global
    from nephoscope.lib.mirror.permissions_hash import settings_permissions_hash

    target = _target_path(db_conn)
    existing = {
        "dontAskAboutTools": True,
        "model": "claude-sonnet-4-6",
        "permissions": {
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

    with (
        patch(
            "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
        ),
        patch(
            "nephoscope.lib.mirror.writer.get_config",
            return_value=_cfg(["/tmp/ws"]),
        ),
    ):
        sync_global(db_conn)

    data = json.loads(target.read_bytes())
    assert data.get("dontAskAboutTools") is True
    assert data.get("model") == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# 7. _nephoscopeAllowedTools marker key records only generated entries
# ---------------------------------------------------------------------------


def test_marker_key_records_generated_entries(tmp_path, db_conn):
    """After sync, _nephoscopeAllowedTools contains exactly the three entries
    we generated — no user entries, no DB-derived entries."""
    from nephoscope.lib.mirror.writer import sync_global

    with (
        patch(
            "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
        ),
        patch(
            "nephoscope.lib.mirror.writer.get_config",
            return_value=_cfg(["/tmp/marker-test"]),
        ),
    ):
        sync_global(db_conn)

    data = json.loads(_target_path(db_conn).read_bytes())
    marker = data.get("_nephoscopeAllowedTools", [])
    assert sorted(marker) == sorted(
        [
            "Write(/tmp/marker-test/**)",
            "Edit(/tmp/marker-test/**)",
            "Read(/tmp/marker-test/**)",
        ]
    )


# ---------------------------------------------------------------------------
# 8. Multiple workspace roots each get three entries
# ---------------------------------------------------------------------------


def test_multiple_workspace_roots_each_get_three_entries(tmp_path, db_conn):
    """Two workspace roots produce six total generated entries (3 each)."""
    from nephoscope.lib.mirror.writer import sync_global

    with (
        patch(
            "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
        ),
        patch(
            "nephoscope.lib.mirror.writer.get_config",
            return_value=_cfg(["/tmp/proj-a", "/tmp/proj-b"]),
        ),
    ):
        sync_global(db_conn)

    data = json.loads(_target_path(db_conn).read_bytes())
    allow = data["permissions"]["allow"]
    for root in ["/tmp/proj-a", "/tmp/proj-b"]:
        assert f"Write({root}/**)" in allow
        assert f"Edit({root}/**)" in allow
        assert f"Read({root}/**)" in allow

    assert len(data["_nephoscopeAllowedTools"]) == 6


# ---------------------------------------------------------------------------
# 9. DB-derived allow entries coexist with workspace-root entries
# ---------------------------------------------------------------------------


def test_db_allow_entries_coexist_with_workspace_root_entries(tmp_path, db_conn):
    """DB-derived allow entries from permission rows survive alongside
    workspace-root generated entries in the final allow list."""
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

    with (
        patch("nephoscope.lib.mirror.serializer.serialize", side_effect=serialize_stub),
        patch(
            "nephoscope.lib.mirror.writer.get_config",
            return_value=_cfg(["/tmp/coexist"]),
        ),
    ):
        sync_global(db_conn)

    data = json.loads(_target_path(db_conn).read_bytes())
    allow = data["permissions"]["allow"]
    assert "Bash(git *)" in allow
    assert "Write(/tmp/coexist/**)" in allow


# ---------------------------------------------------------------------------
# 10. Project mirror is never contaminated by workspace-root entries
# ---------------------------------------------------------------------------


def test_project_mirror_not_affected_by_workspace_roots(tmp_path, db_conn):
    """sync_project must not inject workspace-root entries or the marker key
    into a project's settings.local.json, even when workspace_roots is
    configured in the global config."""
    from nephoscope.lib.mirror.writer import sync_project

    fake_project_dir = tmp_path / "myproject" / ".claude"
    fake_project_dir.mkdir(parents=True)
    local_json = fake_project_dir / "settings.local.json"

    project_id = db_conn.execute(
        "INSERT INTO projects"
        " (cwd, name, root, first_seen, last_seen,"
        "  settings_json_path, settings_json_sha256, settings_json_last_synced)"
        " VALUES (?, ?, ?, '2026-01-01Z', '2026-01-01Z', ?, NULL, NULL);",
        (
            str(tmp_path / "myproject"),
            "myproject",
            str(tmp_path / "myproject"),
            str(local_json),
        ),
    ).lastrowid

    with (
        patch(
            "nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize
        ),
        patch(
            "nephoscope.lib.mirror.writer.get_config",
            return_value=_cfg(["/tmp/ws"]),
        ),
    ):
        sync_project(db_conn, project_id)

    data = json.loads(local_json.read_bytes())
    assert "_nephoscopeAllowedTools" not in data
    allow = data["permissions"]["allow"]
    assert "Write(/tmp/ws/**)" not in allow
    assert "Edit(/tmp/ws/**)" not in allow
    assert "Read(/tmp/ws/**)" not in allow
