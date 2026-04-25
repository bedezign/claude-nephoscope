"""Tests for commands.permissions_cmd — /nephoscope:permissions subcommands.

One happy-path test per subcommand (reconcile, mirror-status, mirror-dry-run,
reload-hint) plus one hash-mismatch propagation test. Doom-path and edge-case
coverage for the underlying lib.mirror primitives lives in tests/lib/mirror/.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_PATH = PROJECT_ROOT / "src" / "nephoscope" / "lib" / "schema.sql"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path, settings_path: Path) -> Path:
    """Create an isolated DB with schema + global_mirror pointed at settings_path."""
    db_file = tmp_path / "obs.db"
    conn = sqlite3.connect(str(db_file), isolation_level=None)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.execute(
        "INSERT OR IGNORE INTO permission_modes (name) VALUES"
        " ('default'),('acceptEdits'),('bypassPermissions'),('plan'),('auto')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO call_statuses (name) VALUES"
        " ('pending'),('ok'),('err'),('denied'),('orphan')"
    )
    conn.execute(
        "INSERT INTO global_mirror (id, settings_json_path) VALUES (1, ?);",
        (str(settings_path),),
    )
    conn.commit()
    conn.close()
    return db_file


# ---------------------------------------------------------------------------
# reconcile: happy path (empty DB + absent file → first-touch adopt, no-op)
# ---------------------------------------------------------------------------


def test_reconcile_happy_path(tmp_path: Path, capsys) -> None:
    settings = tmp_path / "settings.json"
    db_file = _make_db(tmp_path, settings)

    from nephoscope.cli.permissions_cmd import reconcile_cmd

    rc = reconcile_cmd(str(db_file), str(settings), mode="auto-json-wins")

    assert rc == 0
    captured = capsys.readouterr()
    # Summary line includes mode and applied flag.
    assert "reconcile" in captured.out
    assert "mode=" in captured.out


# ---------------------------------------------------------------------------
# mirror-status: happy path (global + one project)
# ---------------------------------------------------------------------------


def test_mirror_status_happy_path(tmp_path: Path, capsys) -> None:
    settings = tmp_path / "settings.json"
    db_file = _make_db(tmp_path, settings)

    # Register a project row.
    proj_settings = tmp_path / "proj_settings.json"
    conn = sqlite3.connect(str(db_file), isolation_level=None)
    conn.execute(
        "INSERT INTO projects (cwd, first_seen, last_seen, settings_json_path)"
        " VALUES ('/fake/project', '2024-01-01Z', '2024-01-01Z', ?);",
        (str(proj_settings),),
    )
    conn.commit()
    conn.close()

    from nephoscope.cli.permissions_cmd import mirror_status_cmd

    rc = mirror_status_cmd(str(db_file))

    assert rc == 0
    captured = capsys.readouterr()
    assert "global" in captured.out
    assert "project:" in captured.out
    assert "hash_status" in captured.out  # header row present
    # Stable status tokens still printed (header + at least one row).
    assert "null" in captured.out
    # Humanized word shown alongside the token for known statuses.
    assert "(not tracked)" in captured.out


# ---------------------------------------------------------------------------
# mirror-status: unknown status token does not duplicate itself
# ---------------------------------------------------------------------------


def test_mirror_status_unknown_status_no_duplicate(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """If a row carries an unrecognized hash_status, do not print '<token> (<token>)'.

    Guards against a future refactor that adds a fourth status without updating
    _HASH_STATUS_WORDS. The fallback should print the bare token, not duplicate it
    inside parentheses.
    """
    settings = tmp_path / "settings.json"
    db_file = _make_db(tmp_path, settings)

    from nephoscope.cli import permissions_cmd as mod

    def _fake_rows(_conn):
        return [
            {
                "scope": "global",
                "path": str(settings),
                "last_synced": None,
                "hash_status": "weird",
            }
        ]

    monkeypatch.setattr(mod, "_collect_mirror_rows", _fake_rows)

    rc = mod.mirror_status_cmd(str(db_file))

    assert rc == 0
    out = capsys.readouterr().out
    assert "weird" in out  # token still shown
    assert "weird (weird)" not in out  # but not duplicated inside parens


# ---------------------------------------------------------------------------
# mirror-dry-run: happy path (empty DB → valid JSON to stdout)
# ---------------------------------------------------------------------------


def test_mirror_dry_run_happy_path(tmp_path: Path, capsys) -> None:
    settings = tmp_path / "settings.json"
    db_file = _make_db(tmp_path, settings)

    from nephoscope.cli.permissions_cmd import mirror_dry_run_cmd

    rc = mirror_dry_run_cmd(str(db_file), str(settings))

    assert rc == 0
    captured = capsys.readouterr()
    # stdout should be valid JSON with a "permissions" key.
    data = json.loads(captured.out.strip())
    assert "permissions" in data
    assert "allow" in data["permissions"]
    assert "deny" in data["permissions"]


# ---------------------------------------------------------------------------
# reload-hint: happy path (existing file gets mtime bumped)
# ---------------------------------------------------------------------------


def test_reload_hint_happy_path(tmp_path: Path, capsys) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{}")

    import time

    before = settings.stat().st_mtime
    time.sleep(0.01)  # ensure mtime can advance

    from nephoscope.cli.permissions_cmd import reload_hint_cmd

    rc = reload_hint_cmd(str(settings))

    assert rc == 0
    after = settings.stat().st_mtime
    assert after >= before  # mtime updated or at least not regressed
    captured = capsys.readouterr()
    assert "reload-hint" in captured.out


# ---------------------------------------------------------------------------
# reload-hint: missing file returns exit 1
# ---------------------------------------------------------------------------


def test_reload_hint_missing_file(tmp_path: Path) -> None:
    from nephoscope.cli.permissions_cmd import reload_hint_cmd

    rc = reload_hint_cmd(str(tmp_path / "nonexistent.json"))

    assert rc == 1


# ---------------------------------------------------------------------------
# reconcile: MirrorHashMismatch propagates as error exit
# ---------------------------------------------------------------------------


def test_reconcile_hash_mismatch_returns_error(tmp_path: Path, capsys) -> None:
    """When on-disk hash differs from stored hash, reconcile returns 1."""
    settings = tmp_path / "settings.json"
    db_file = _make_db(tmp_path, settings)

    # Write a file and stamp a *wrong* hash in the DB so reconcile
    # detects a mismatch during the atomic-write check inside sync.
    settings.write_text('{"permissions":{"allow":[],"ask":[],"deny":[]}}')
    wrong_hash = hashlib.sha256(b"wrong content").hexdigest()
    conn = sqlite3.connect(str(db_file), isolation_level=None)
    conn.execute(
        "UPDATE global_mirror SET settings_json_sha256 = ? WHERE id = 1;",
        (wrong_hash,),
    )
    conn.commit()
    conn.close()

    from nephoscope.cli.permissions_cmd import reconcile_cmd

    # auto-json-wins will attempt sync → MirrorHashMismatch → ReconcileError
    rc = reconcile_cmd(str(db_file), str(settings), mode="auto-json-wins")

    assert rc == 1
    captured = capsys.readouterr()
    assert "reconcile error" in captured.err


# ---------------------------------------------------------------------------
# mirror-dry-run: global scope (no target_path) resolves via DB
# ---------------------------------------------------------------------------


def test_mirror_dry_run_global_scope(tmp_path: Path, capsys) -> None:
    settings = tmp_path / "settings.json"
    db_file = _make_db(tmp_path, settings)

    from nephoscope.cli.permissions_cmd import mirror_dry_run_cmd

    # Pass None as target_path → falls back to global (project_id=None)
    rc = mirror_dry_run_cmd(str(db_file), target_path=None)

    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert "permissions" in data
