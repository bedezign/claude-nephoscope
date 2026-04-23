"""Tests for W3B: learner + seed mirror-sync wiring.

Covers:
- promote CLI → global mirror updated, hash stamped, source='learner'
- promote CLI with tampered on-disk hash → MirrorHashMismatch surfaces as CLI
  error on stderr; DB row IS created (DB commit stands)
- reject CLI → mirror reflects rejected decision (deny list)
- unpermit CLI → mirror loses the line
- apply_fixtures on empty JSON → all rules written to mirror
- apply_fixtures on non-empty pre-existing JSON (NULL hash) → first-touch
  writes succeed + hash stamped
- apply_fixtures on tampered JSON (hash set, content changed) → ValueError
  with reconcile message raised

All writes go to tmp_path.  Real paths (~/.claude/settings.json) are never
touched.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest
import yaml

import nephoscope.lib.db as db
from nephoscope.learners.permission.learner import main as learner_main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _serialize_bash(row: dict) -> str | None:
    """Minimal serializer stub: render Bash-like rule as 'Bash(<verb> *)'."""
    return f"Bash({row['verb']} *)"


def _null_serialize(row: dict) -> str | None:
    """Serializer stub that drops every row (empty mirror)."""
    return None


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _seed_global_mirror(
    conn: sqlite3.Connection, settings_path: Path, stored_hash: str | None = None
) -> None:
    """Insert (or replace) the global_mirror singleton pointing at settings_path."""
    conn.execute(
        """
        INSERT OR REPLACE INTO global_mirror
          (id, settings_json_path, settings_json_sha256, settings_json_last_synced)
        VALUES (1, ?, ?, NULL);
        """,
        (str(settings_path), stored_hash),
    )


def _read_global_hash(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT settings_json_sha256 FROM global_mirror WHERE id = 1;"
    ).fetchone()
    return row[0] if row else None


@contextmanager
def _mock_connect(conn: sqlite3.Connection):
    """Patch _connect() to return a non-closing wrapper around conn."""

    class _NonClose:
        def __init__(self, c: sqlite3.Connection) -> None:
            self._c = c

        def close(self) -> None:
            pass  # keep fixture alive

        def __getattr__(self, name: str):
            return getattr(self._c, name)

    with mock.patch(
        "nephoscope.learners.permission.learner._connect",
        side_effect=lambda: _NonClose(conn),
    ):
        yield


# ---------------------------------------------------------------------------
# Tests: promote
# ---------------------------------------------------------------------------


class TestPromoteMirrorSync:
    """promote CLI → DB insert + global mirror sync."""

    def test_promote_writes_mirror_and_stamps_hash(self, tmp_db, tmp_path, capsys):
        """Promoting a global rule writes it to the mirror file and stamps the hash."""
        fake_settings = tmp_path / "settings.json"
        _seed_global_mirror(tmp_db, fake_settings)
        tmp_db.commit()

        with mock.patch("nephoscope.lib.mirror.serializer.serialize", side_effect=_serialize_bash):
            with _mock_connect(tmp_db):
                rc = learner_main(
                    [
                        "promote",
                        "--verb",
                        "git",
                        "--subcommand",
                        "status",
                        "--flags",
                        "[]",
                    ]
                )

        assert rc == 0
        assert fake_settings.exists(), "mirror file must be created"

        data = json.loads(fake_settings.read_bytes())
        allow_list = data["permissions"]["allow"]
        assert any("git" in entry for entry in allow_list), (
            f"promoted rule should appear in allow list; got {allow_list}"
        )

        stored_hash = _read_global_hash(tmp_db)
        assert stored_hash is not None, "hash must be stamped after promote"
        assert stored_hash == _sha256(fake_settings.read_bytes())

    def test_promote_db_row_has_source_learner(self, tmp_db, tmp_path):
        """Promote inserts a permissions row with source='learner'."""
        fake_settings = tmp_path / "settings.json"
        _seed_global_mirror(tmp_db, fake_settings)
        tmp_db.commit()

        with mock.patch("nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize):
            with _mock_connect(tmp_db):
                learner_main(
                    [
                        "promote",
                        "--verb",
                        "git",
                        "--subcommand",
                        "push",
                        "--flags",
                        "[]",
                    ]
                )

        row = tmp_db.execute(
            "SELECT p.source FROM permissions p"
            " JOIN rule_shapes rs ON rs.id = p.rule_shape_id"
            " WHERE rs.verb = 'git' AND IFNULL(rs.subcommand,'') = 'push';"
        ).fetchone()
        assert row is not None, "permissions row must exist"
        assert row[0] == "learner"

    def test_promote_tampered_hash_surfaces_cli_error(self, tmp_db, tmp_path, capsys):
        """Promoting when the mirror was tampered yields exit code 1 and stderr message."""
        fake_settings = tmp_path / "settings.json"

        # First sync: write file + stamp hash.
        with mock.patch("nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize):
            _seed_global_mirror(tmp_db, fake_settings)
            tmp_db.commit()
            from nephoscope.lib.mirror.writer import sync_global

            sync_global(tmp_db)

        # Tamper the file so hash no longer matches.
        fake_settings.write_text('{"tampered": true}')

        with mock.patch("nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize):
            with _mock_connect(tmp_db):
                rc = learner_main(["promote", "--verb", "curl", "--flags", "[]"])

        assert rc == 1, "exit code must be non-zero on hash mismatch"
        err = capsys.readouterr().err
        assert "edited externally" in err, (
            f"stderr should mention external edit; got: {err!r}"
        )
        assert "reconcile" in err, f"stderr should mention reconcile; got: {err!r}"

    def test_promote_tampered_hash_db_row_persists(self, tmp_db, tmp_path):
        """DB row is visible after promote even when mirror sync fails with hash mismatch.

        Note: the test uses _mock_connect which forwards to tmp_db (non-autocommit).
        The INSERT is part of the ongoing transaction and is visible to the same
        connection — verifying that the code path does not roll back the row.
        """
        fake_settings = tmp_path / "settings.json"

        # Stamp an initial hash.
        with mock.patch("nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize):
            _seed_global_mirror(tmp_db, fake_settings)
            tmp_db.commit()
            from nephoscope.lib.mirror.writer import sync_global

            sync_global(tmp_db)

        # Tamper.
        fake_settings.write_text('{"tampered": true}')

        # Run promote — mirror sync will fail, but the DB row must still exist.
        with mock.patch("nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize):
            with _mock_connect(tmp_db):
                rc = learner_main(["promote", "--verb", "curl", "--flags", "[]"])

        assert rc == 1, "exit code must be non-zero on hash mismatch"

        # Row must exist — DB row is not rolled back when mirror sync fails.
        row = tmp_db.execute(
            "SELECT p.id FROM permissions p"
            " JOIN rule_shapes rs ON rs.id = p.rule_shape_id"
            " WHERE rs.verb = 'curl';"
        ).fetchone()
        assert row is not None, "DB row must persist despite mirror hash mismatch"

    def test_session_tier_promote_skips_mirror_sync(self, tmp_db, tmp_path):
        """Session-tier promote must not touch the mirror file.

        The guard is ``if session_id is None`` in _cmd_promote; for session-tier
        the id is not None, so sync_affected is never imported or called.
        """
        fake_settings = tmp_path / "settings.json"
        _seed_global_mirror(tmp_db, fake_settings)

        # Insert a session row.
        import datetime as _dt

        now = (
            _dt.datetime.now(tz=_dt.timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        tmp_db.execute(
            "INSERT INTO sessions(session_uuid, project_id, started_at, last_activity)"
            " VALUES ('sess-abc', NULL, ?, ?)",
            (now, now),
        )
        tmp_db.commit()
        sess_row = tmp_db.execute(
            "SELECT id FROM sessions WHERE session_uuid='sess-abc'"
        ).fetchone()
        sess_id = int(sess_row[0])

        # Patch at the writer module level so we can detect any call.
        with mock.patch("nephoscope.lib.mirror.writer.sync_global") as mock_sync:
            with mock.patch("nephoscope.lib.mirror.writer.sync_affected") as mock_sync_affected:
                with _mock_connect(tmp_db):
                    rc = learner_main(
                        [
                            "promote",
                            "--verb",
                            "make",
                            "--flags",
                            "[]",
                            "--tier",
                            "session",
                            "--session-id",
                            str(sess_id),
                        ]
                    )

        assert rc == 0
        # Mirror writer functions must NOT be called for session-tier.
        mock_sync.assert_not_called()
        mock_sync_affected.assert_not_called()
        assert not fake_settings.exists(), "mirror must not be written for session-tier"


# ---------------------------------------------------------------------------
# Tests: reject
# ---------------------------------------------------------------------------


class TestRejectMirrorSync:
    """reject CLI → DB insert + mirror sync."""

    def test_reject_rule_appears_in_deny_list(self, tmp_db, tmp_path, capsys):
        """Rejecting a rule writes it to the deny list in the mirror."""
        fake_settings = tmp_path / "settings.json"
        _seed_global_mirror(tmp_db, fake_settings)
        tmp_db.commit()

        with mock.patch("nephoscope.lib.mirror.serializer.serialize", side_effect=_serialize_bash):
            with _mock_connect(tmp_db):
                rc = learner_main(
                    [
                        "reject",
                        "--verb",
                        "rm",
                        "--flags",
                        '["-rf"]',
                        "--reason",
                        "dangerous",
                    ]
                )

        assert rc == 0
        assert fake_settings.exists()
        data = json.loads(fake_settings.read_bytes())
        deny_list = data["permissions"]["deny"]
        assert any("rm" in entry for entry in deny_list), (
            f"rejected rule should appear in deny list; got {deny_list}"
        )

    def test_reject_tampered_hash_surfaces_error(self, tmp_db, tmp_path, capsys):
        """Reject with tampered mirror file yields exit 1 + stderr message."""
        fake_settings = tmp_path / "settings.json"
        with mock.patch("nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize):
            _seed_global_mirror(tmp_db, fake_settings)
            tmp_db.commit()
            from nephoscope.lib.mirror.writer import sync_global

            sync_global(tmp_db)

        fake_settings.write_text('{"tampered": true}')

        with mock.patch("nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize):
            with _mock_connect(tmp_db):
                rc = learner_main(["reject", "--verb", "rm", "--flags", "[]"])

        assert rc == 1
        err = capsys.readouterr().err
        assert "edited externally" in err
        assert "reconcile" in err


# ---------------------------------------------------------------------------
# Tests: unpermit
# ---------------------------------------------------------------------------


class TestUnpermitMirrorSync:
    """unpermit CLI → DB delete + mirror sync."""

    def _insert_global_rule(
        self,
        conn: sqlite3.Connection,
        verb: str,
        subcommand: str | None,
        flags: str,
    ) -> tuple[int, int]:
        """Insert a rule_shape + global approved permission, return (shape_id, perm_id)."""
        import datetime as _dt

        now = (
            _dt.datetime.now(tz=_dt.timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        shape_id = db.upsert_rule_shape(conn, verb, subcommand, flags, None, now)
        perm_id = db.insert_permission(
            conn, shape_id, None, None, "approved", "seed", now
        )
        conn.commit()
        return shape_id, perm_id

    def test_unpermit_removes_rule_from_mirror(self, tmp_db, tmp_path, capsys):
        """After unpermit, the rule no longer appears in the mirror allow list."""
        fake_settings = tmp_path / "settings.json"
        _seed_global_mirror(tmp_db, fake_settings)

        # Insert a rule.
        self._insert_global_rule(tmp_db, "git", "status", "[]")

        # Sync the rule into the mirror first.
        with mock.patch("nephoscope.lib.mirror.serializer.serialize", side_effect=_serialize_bash):
            from nephoscope.lib.mirror.writer import sync_global

            sync_global(tmp_db)

        pre_data = json.loads(fake_settings.read_bytes())
        assert any("git" in e for e in pre_data["permissions"]["allow"]), (
            "rule must be in mirror before unpermit"
        )

        # Unpermit the rule.
        with mock.patch("nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize):
            with _mock_connect(tmp_db):
                rc = learner_main(
                    [
                        "unpermit",
                        "--verb",
                        "git",
                        "--subcommand",
                        "status",
                        "--flags",
                        "[]",
                    ]
                )

        assert rc == 0
        post_data = json.loads(fake_settings.read_bytes())
        assert not any("git" in e for e in post_data["permissions"]["allow"]), (
            "rule must be absent from mirror after unpermit"
        )

    def test_unpermit_no_match_does_not_crash_mirror(self, tmp_db, tmp_path):
        """Unpermitting a non-existent rule returns 0 and does not touch the mirror."""
        fake_settings = tmp_path / "settings.json"
        _seed_global_mirror(tmp_db, fake_settings)
        tmp_db.commit()

        with mock.patch("nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize):
            with _mock_connect(tmp_db):
                rc = learner_main(
                    ["unpermit", "--verb", "nonexistent", "--flags", "[]"]
                )

        assert rc in (0, 1)  # either "no match" (0) or "rule_shape not found" (1)
        assert not fake_settings.exists(), (
            "mirror should not be created for no-op unpermit"
        )


# ---------------------------------------------------------------------------
# Tests: apply_fixtures
# ---------------------------------------------------------------------------


class TestApplyFixturesMirrorSync:
    """apply_fixtures routes global-tier rows through mirror writer."""

    def test_empty_fixture_no_mirror_write(self, tmp_db, tmp_path):
        """An empty fixture list leaves the mirror untouched."""
        from nephoscope.learners.permission.seed import apply_fixtures

        fake_settings = tmp_path / "settings.json"
        _seed_global_mirror(tmp_db, fake_settings)
        tmp_db.commit()

        fixture = tmp_path / "empty.yaml"
        fixture.write_text("[]")

        with mock.patch("nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize):
            shapes, perms = apply_fixtures(tmp_db, fixture)

        assert shapes == 0
        assert perms == 0
        # Mirror not created because no rows were inserted.
        assert not fake_settings.exists()

    def test_apply_fixtures_writes_to_mirror(self, tmp_db, tmp_path):
        """apply_fixtures on empty settings.json writes rules and stamps hash."""
        from nephoscope.learners.permission.seed import apply_fixtures

        fake_settings = tmp_path / "settings.json"
        _seed_global_mirror(tmp_db, fake_settings)
        tmp_db.commit()

        fixture = tmp_path / "rules.yaml"
        fixture.write_text(
            yaml.dump(
                [
                    {"verb": "git", "flags": [], "decision": "approved"},
                    {"verb": "make", "flags": [], "decision": "approved"},
                ]
            )
        )

        with mock.patch("nephoscope.lib.mirror.serializer.serialize", side_effect=_serialize_bash):
            shapes, perms = apply_fixtures(tmp_db, fixture)

        assert perms == 2
        assert fake_settings.exists(), "mirror must be written after apply_fixtures"

        data = json.loads(fake_settings.read_bytes())
        allow_list = data["permissions"]["allow"]
        assert len(allow_list) == 2, f"expected 2 allow entries; got {allow_list}"

        stored_hash = _read_global_hash(tmp_db)
        assert stored_hash is not None, "hash must be stamped"
        assert stored_hash == _sha256(fake_settings.read_bytes())

    def test_apply_fixtures_first_touch_null_hash(self, tmp_db, tmp_path):
        """First-touch: hash IS NULL + existing file → writes succeed and stamp hash."""
        from nephoscope.learners.permission.seed import apply_fixtures

        fake_settings = tmp_path / "settings.json"
        # Write a pre-existing file (user's hand-written settings).
        fake_settings.write_text(
            json.dumps({"permissions": {"allow": [], "deny": [], "ask": []}})
        )
        # Seed global_mirror with NULL hash (no previous sync from us).
        _seed_global_mirror(tmp_db, fake_settings, stored_hash=None)
        tmp_db.commit()

        fixture = tmp_path / "rules.yaml"
        fixture.write_text(
            yaml.dump([{"verb": "cargo", "flags": [], "decision": "approved"}])
        )

        with mock.patch("nephoscope.lib.mirror.serializer.serialize", side_effect=_serialize_bash):
            shapes, perms = apply_fixtures(tmp_db, fixture)

        assert perms == 1
        stored_hash = _read_global_hash(tmp_db)
        assert stored_hash is not None, "hash must be stamped after first-touch"
        assert stored_hash == _sha256(fake_settings.read_bytes())

    def test_apply_fixtures_tampered_json_raises(self, tmp_db, tmp_path):
        """apply_fixtures raises ValueError when mirror file was tampered after last sync."""
        from nephoscope.learners.permission.seed import apply_fixtures
        from nephoscope.lib.mirror.writer import sync_global

        fake_settings = tmp_path / "settings.json"
        _seed_global_mirror(tmp_db, fake_settings)
        tmp_db.commit()

        # Initial sync to stamp the hash.
        with mock.patch("nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize):
            sync_global(tmp_db)

        # Tamper the file after the sync.
        fake_settings.write_text('{"tampered": true}')

        fixture = tmp_path / "rules.yaml"
        fixture.write_text(
            yaml.dump([{"verb": "node", "flags": [], "decision": "approved"}])
        )

        with mock.patch("nephoscope.lib.mirror.serializer.serialize", side_effect=_null_serialize):
            with pytest.raises(ValueError) as exc_info:
                apply_fixtures(tmp_db, fixture)

        msg = str(exc_info.value)
        assert "edited externally" in msg, (
            f"error must mention external edit; got: {msg!r}"
        )
        assert "reconcile" in msg, f"error must mention reconcile; got: {msg!r}"
