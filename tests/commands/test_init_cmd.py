"""Tests for cli.init_cmd — nephoscope-init bootstrap command."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_PATH = PROJECT_ROOT / "src" / "nephoscope" / "lib" / "schema.sql"

_CREDENTIAL_PATHS = [
    "$HOME/.aws/credentials",
    "$HOME/.kube/config",
    "$HOME/.docker/config.json",
    "$HOME/.npmrc",
    "$HOME/.netrc",
    "$HOME/.bash_history",
    "$HOME/.zsh_history",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    return conn


def _rule_shapes_for_wildcard_verb(conn: sqlite3.Connection) -> list[str]:
    """Return path_spec values of verb="*" rule shapes."""
    rows = conn.execute(
        "SELECT path_spec FROM rule_shapes WHERE verb = '*';"
    ).fetchall()
    return [row[0] for row in rows]


def _rejected_permissions_for_shape(conn: sqlite3.Connection, shape_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM permissions WHERE rule_shape_id = ? AND decision = 'rejected';",
        (shape_id,),
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Fresh-DB fixture loading
# ---------------------------------------------------------------------------


class TestInitCmdFixtureLoad:
    def test_fresh_db_gets_credential_rule_shapes(self, tmp_path, monkeypatch):
        """On a fresh DB, init_cmd loads the credential_leaks fixture → rule_shapes rows."""
        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        from nephoscope.cli.init_cmd import main

        rc = main(["--no-workspace-prompts"])
        assert rc == 0

        conn = _open_db(db_path)
        try:
            wildcard_specs = _rule_shapes_for_wildcard_verb(conn)
            for expected_spec in _CREDENTIAL_PATHS:
                assert expected_spec in wildcard_specs, (
                    f"Expected credential rule_shape for {expected_spec!r}, "
                    f"got: {wildcard_specs!r}"
                )
        finally:
            conn.close()

    def test_fresh_db_gets_rejected_permissions(self, tmp_path, monkeypatch):
        """Each credential rule_shape has a rejected global-tier permission row."""
        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        from nephoscope.cli.init_cmd import main

        rc = main(["--no-workspace-prompts"])
        assert rc == 0

        conn = _open_db(db_path)
        try:
            for expected_spec in _CREDENTIAL_PATHS:
                row = conn.execute(
                    "SELECT id FROM rule_shapes WHERE verb = '*' AND path_spec = ?;",
                    (expected_spec,),
                ).fetchone()
                assert row is not None, f"Expected rule_shape row for {expected_spec!r}"
                perm_count = _rejected_permissions_for_shape(conn, row[0])
                assert perm_count >= 1, (
                    f"Expected at least one rejected permission for {expected_spec!r}"
                )
        finally:
            conn.close()

    def test_already_existing_db_does_not_double_load_fixture(
        self, tmp_path, monkeypatch
    ):
        """Re-running init on an existing DB does not create duplicate rows."""
        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        from nephoscope.cli.init_cmd import main

        # First init — fresh DB.
        rc1 = main(["--no-workspace-prompts"])
        assert rc1 == 0

        # Second init — existing DB; fixture must NOT be loaded again.
        rc2 = main(["--no-workspace-prompts"])
        assert rc2 == 0

        conn = _open_db(db_path)
        try:
            # Count rule_shapes for a specific credential path.
            # Idempotent upsert means the count stays at 1.
            count = conn.execute(
                "SELECT COUNT(*) FROM rule_shapes"
                " WHERE verb = '*' AND path_spec = '$HOME/.aws/credentials';",
            ).fetchone()[0]
            # Should be exactly 1 — upsert is idempotent.
            assert count == 1, (
                f"Expected 1 rule_shape row after second init, got {count}"
            )
        finally:
            conn.close()

    def test_init_returns_zero_on_success(self, tmp_path, monkeypatch):
        """init_cmd exits 0 on a clean first run."""
        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        from nephoscope.cli.init_cmd import main

        rc = main(["--no-workspace-prompts"])
        assert rc == 0

    def test_init_idempotent_exit_zero(self, tmp_path, monkeypatch):
        """Re-running init on an existing DB also exits 0."""
        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        from nephoscope.cli.init_cmd import main

        main(["--no-workspace-prompts"])
        rc = main(["--no-workspace-prompts"])
        assert rc == 0


_SECRET_MANAGER_SHAPES = [
    ("op", "read"),
    ("vault", "kv get"),
    ("bw", "get"),
    ("doppler", "secrets get"),
    ("pass", "show"),
    ("gopass", "show"),
]


class TestInitCmdSecretManagerFixture:
    """secret_manager_standalones.yaml is loaded on fresh install."""

    def test_fresh_db_gets_secret_manager_rule_shapes(self, tmp_path, monkeypatch):
        """On a fresh DB, init_cmd loads the secret_manager_standalones fixture."""
        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        from nephoscope.cli.init_cmd import main

        rc = main(["--no-workspace-prompts"])
        assert rc == 0

        conn = _open_db(db_path)
        try:
            for verb, subcommand in _SECRET_MANAGER_SHAPES:
                row = conn.execute(
                    "SELECT id, context FROM rule_shapes"
                    " WHERE verb = ? AND IFNULL(subcommand, '') = IFNULL(?, '');",
                    (verb, subcommand),
                ).fetchone()
                assert row is not None, (
                    f"Expected rule_shape for ({verb!r}, {subcommand!r}) after init"
                )
                assert row[1] == "toplevel", (
                    f"Expected context='toplevel' for ({verb!r}, {subcommand!r}), "
                    f"got {row[1]!r}"
                )
        finally:
            conn.close()

    def test_secret_manager_rules_have_rejected_permission(self, tmp_path, monkeypatch):
        """Each secret manager rule_shape has a rejected global-tier permission row."""
        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        from nephoscope.cli.init_cmd import main

        main(["--no-workspace-prompts"])

        conn = _open_db(db_path)
        try:
            for verb, subcommand in _SECRET_MANAGER_SHAPES:
                row = conn.execute(
                    "SELECT id FROM rule_shapes"
                    " WHERE verb = ? AND IFNULL(subcommand, '') = IFNULL(?, '')"
                    " AND context = 'toplevel';",
                    (verb, subcommand),
                ).fetchone()
                assert row is not None, (
                    f"Expected toplevel rule_shape for ({verb!r}, {subcommand!r})"
                )
                perm_count = _rejected_permissions_for_shape(conn, row[0])
                assert perm_count >= 1, (
                    f"Expected rejected permission for ({verb!r}, {subcommand!r})"
                )
        finally:
            conn.close()


class TestEnsureDatabaseInConfig:
    @pytest.fixture(autouse=True)
    def _clear_config_cache(self) -> Generator[None, None, None]:
        from nephoscope.config import get_config

        get_config.cache_clear()
        yield
        get_config.cache_clear()

    def test_writes_database_key_when_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from nephoscope.cli.init_cmd import _ensure_database_in_config

        config_path = tmp_path / "nephoscope.toml"
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(config_path))

        _ensure_database_in_config(Path("/data/obs.db"))

        assert config_path.exists(), (
            "config file must be created by _ensure_database_in_config"
        )
        content = config_path.read_text()
        assert "database" in content, (
            f'expected "database" key in config, got: {content!r}'
        )
        assert "/data/obs.db" in content, (
            f'expected "/data/obs.db" value in config, got: {content!r}'
        )

    def test_idempotent_when_key_already_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from nephoscope.cli.init_cmd import _ensure_database_in_config

        config_path = tmp_path / "nephoscope.toml"
        config_path.write_text('database = "/data/existing.db"\n')
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(config_path))

        _ensure_database_in_config(Path("/data/new.db"))

        content = config_path.read_text()
        assert "/data/existing.db" in content, (
            f"existing database value must not be overwritten; got: {content!r}"
        )
        assert "/data/new.db" not in content, (
            f"new path must not replace existing database value; got: {content!r}"
        )

    def test_clears_get_config_cache(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from nephoscope.cli.init_cmd import _ensure_database_in_config
        from nephoscope.config import get_config

        config_path = tmp_path / "nephoscope.toml"
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(config_path))

        _ensure_database_in_config(Path("/data/obs.db"))

        config = get_config()
        assert config.database == "/data/obs.db", (
            f"get_config() must reflect freshly-written value after cache clear; "
            f"got {config.database!r}"
        )

    def test_preserves_existing_config_keys(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from nephoscope.cli.init_cmd import _ensure_database_in_config

        config_path = tmp_path / "nephoscope.toml"
        config_path.write_text('trusted_dirs = ["/tmp/ws"]\n')
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(config_path))

        _ensure_database_in_config(Path("/data/obs.db"))

        content = config_path.read_text()
        assert "/tmp/ws" in content, (
            f"pre-existing trusted_dirs must survive _ensure_database_in_config; got: {content!r}"
        )
        assert "/data/obs.db" in content, (
            f"new database value must be present after write; got: {content!r}"
        )
