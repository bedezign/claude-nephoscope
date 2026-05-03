"""Tests for DB-path bootstrap and database config write-through in session-start.

Covers two behaviors added in the config-db-path plan:

1. ``_ensure_db_bootstrapped()`` catches ``RuntimeError`` from
   ``observations_db_path()`` and returns ``None`` rather than crashing when no
   DB path is configured at all.

2. ``_handle_session_start()`` calls ``_ensure_database_in_config()`` (writing
   the ``database`` key into the TOML config) when ``CLAUDE_PLUGIN_DATA`` is set
   and the config has no existing ``database`` key.  When ``CLAUDE_PLUGIN_DATA``
   is absent, or when the key is already present, no write occurs.

These tests are intentionally RED until the implementation is added.
"""

from __future__ import annotations

import pathlib
import sqlite3
import threading
import tomllib
from unittest.mock import patch

import pytest

from nephoscope.config import get_config


@pytest.fixture(autouse=True)
def clear_config_cache():
    """Clear the ``get_config`` LRU cache before and after each test."""
    get_config.cache_clear()
    yield
    get_config.cache_clear()


class TestEnsureDbBootstrapped:
    def test_survives_no_path_configured(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_ensure_db_bootstrapped()`` must not raise when no DB path is configured.

        With all three resolution sources absent (env var, config file, CLAUDE_PLUGIN_DATA),
        ``observations_db_path()`` raises ``RuntimeError``.  The function must catch
        that and return ``None``.
        """
        monkeypatch.delenv("OBSERVABILITY_DB", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        # Point config at a non-existent file so no ``database`` key is found.
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(tmp_path / "config.toml"))
        get_config.cache_clear()

        from nephoscope.recorder.run import _ensure_db_bootstrapped

        result = _ensure_db_bootstrapped()
        assert result is None

    def test_creates_db_when_path_configured(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_ensure_db_bootstrapped()`` creates the DB file when a path is configured."""
        db_path = tmp_path / "obs.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))
        get_config.cache_clear()

        from nephoscope.recorder.run import _ensure_db_bootstrapped

        _ensure_db_bootstrapped()
        assert db_path.exists()


class TestHandleSessionStartDatabaseConfig:
    def test_writes_database_key_when_plugin_data_set(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``CLAUDE_PLUGIN_DATA`` is set, ``_handle_session_start`` writes
        the ``database`` key into the config TOML file."""
        db_path = tmp_path / "obs.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_dir))

        config_path = tmp_path / "config.toml"
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(config_path))
        get_config.cache_clear()

        from nephoscope.recorder.run import _handle_session_start

        _handle_session_start({"session_id": "sess-001", "cwd": str(tmp_path)})

        assert config_path.exists(), (
            "config.toml must be created by _handle_session_start"
        )
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
        expected_db = str(plugin_dir / "observations.db")
        assert "database" in data, f"database key missing from config; got: {data!r}"
        assert data["database"] == expected_db, (
            f"expected database={expected_db!r}, got {data['database']!r}"
        )

    def test_skips_database_write_when_plugin_data_absent(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``CLAUDE_PLUGIN_DATA`` is absent, no config file is created."""
        db_path = tmp_path / "obs.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)

        config_path = tmp_path / "config.toml"
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(config_path))
        get_config.cache_clear()

        from nephoscope.recorder.run import _handle_session_start

        _handle_session_start({"session_id": "sess-002", "cwd": str(tmp_path)})

        assert not config_path.exists(), (
            "config.toml must NOT be created when CLAUDE_PLUGIN_DATA is absent"
        )

    def test_does_not_overwrite_existing_database_key(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An existing ``database`` key in the config must not be overwritten."""
        db_path = tmp_path / "obs.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_dir))

        config_path = tmp_path / "config.toml"
        config_path.write_text('database = "/data/existing.db"\n')
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(config_path))
        get_config.cache_clear()

        from nephoscope.recorder.run import _handle_session_start

        _handle_session_start({"session_id": "sess-003", "cwd": str(tmp_path)})

        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
        assert data["database"] == "/data/existing.db", (
            f"existing database key must not be overwritten; got {data['database']!r}"
        )

    def test_session_start_idempotent_on_two_sequential_calls(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two sequential ``_handle_session_start`` calls must not duplicate config
        entries or raise on the second call."""
        db_path = tmp_path / "obs.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_dir))

        config_path = tmp_path / "config.toml"
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(config_path))
        get_config.cache_clear()

        from nephoscope.recorder.run import _handle_session_start

        payload = {"session_id": "sess-idem-01", "cwd": str(tmp_path)}
        _handle_session_start(payload)
        get_config.cache_clear()
        _handle_session_start(payload)

        assert config_path.exists()
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
        assert "database" in data
        # TOML has no duplicate-key concept — the file must be valid and contain
        # exactly one database value equal to the plugin-data path.
        assert data["database"] == str(plugin_dir / "observations.db")

    def test_write_config_file_oserror_does_not_propagate_from_session_start(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``_write_config_file`` raises ``OSError``, ``_handle_session_start``
        must swallow it rather than propagating to the caller."""
        db_path = tmp_path / "obs.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_dir))

        config_path = tmp_path / "config.toml"
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(config_path))
        get_config.cache_clear()

        from nephoscope.recorder.run import _handle_session_start

        with patch(
            "nephoscope.cli.init_cmd._write_config_file",
            side_effect=OSError("disk full"),
        ):
            # Must not raise — the bare except in _handle_session_start swallows it.
            _handle_session_start({"session_id": "sess-oserr-01", "cwd": str(tmp_path)})

    def test_disk_full_during_config_write_is_handled_safely(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_write_config_file`` raising ``OSError: No space left on device``
        must be absorbed by ``_handle_session_start`` without propagating."""
        db_path = tmp_path / "obs.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_dir))

        config_path = tmp_path / "config.toml"
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(config_path))
        get_config.cache_clear()

        from nephoscope.recorder.run import _handle_session_start

        enospc = OSError(28, "No space left on device")
        with patch(
            "nephoscope.cli.init_cmd._write_config_file",
            side_effect=enospc,
        ):
            _handle_session_start(
                {"session_id": "sess-enospc-01", "cwd": str(tmp_path)}
            )

        # The config file must not exist — write failed, no partial artifact.
        assert not config_path.exists()

    def test_write_config_file_permission_denied_does_not_propagate(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``_write_config_file`` raises ``PermissionError``,
        ``_handle_session_start`` must swallow it rather than propagating."""
        db_path = tmp_path / 'obs.db'
        monkeypatch.setenv('OBSERVABILITY_DB', str(db_path))

        plugin_dir = tmp_path / 'plugin'
        plugin_dir.mkdir()
        monkeypatch.setenv('CLAUDE_PLUGIN_DATA', str(plugin_dir))

        config_path = tmp_path / 'config.toml'
        monkeypatch.setenv('NEPHOSCOPE_CONFIG', str(config_path))
        get_config.cache_clear()

        from nephoscope.recorder.run import _handle_session_start

        with patch(
            'nephoscope.cli.init_cmd._write_config_file',
            side_effect=PermissionError('Permission denied'),
        ):
            _handle_session_start({'session_id': 'sess-perm-01', 'cwd': str(tmp_path)})


class TestConcurrentConfigWrite:
    def test_concurrent_config_write_is_safe(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two threads calling ``_ensure_database_in_config`` simultaneously
        must not corrupt the config file or raise an uncaught exception.

        Currently xfail: ``_write_config_file`` uses a fixed ``config.toml.tmp``
        sibling. When two threads race, both open and write the same temp file.
        Thread A's ``os.rename`` moves it to ``config.toml``; thread B's
        subsequent ``os.rename`` finds no file at the temp path and raises
        ``FileNotFoundError``.  The final config content is correct — only the
        escaped exception is the bug.  In production, ``_handle_session_start``
        wraps ``_ensure_database_in_config`` in ``except Exception: pass``,
        so the failure is silent there, but direct callers without that guard
        will receive the ENOENT.
        """
        config_path = tmp_path / "config.toml"
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(config_path))
        get_config.cache_clear()

        db_path = tmp_path / "observations.db"

        from nephoscope.cli.init_cmd import _ensure_database_in_config

        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def _worker() -> None:
            barrier.wait()
            try:
                _ensure_database_in_config(db_path)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Uncaught exceptions from concurrent writes: {errors}"

        # File must exist and contain valid TOML with the expected database key.
        assert config_path.exists()
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
        assert data.get("database") == str(db_path)


class TestEnsureDbBootstrappedAdditional:
    def test_mkdir_permission_denied_returns_none(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_ensure_db_bootstrapped`` must return ``None`` when ``mkdir`` raises
        ``PermissionError``, without propagating to the caller."""
        db_path = tmp_path / 'no_access' / 'obs.db'
        monkeypatch.setenv('OBSERVABILITY_DB', str(db_path))
        get_config.cache_clear()

        from nephoscope.recorder.run import _ensure_db_bootstrapped

        with patch('pathlib.Path.mkdir', side_effect=PermissionError('Permission denied')):
            result = _ensure_db_bootstrapped()

        assert result is None

    def test_sqlite_open_error_returns_none(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_ensure_db_bootstrapped`` must return ``None`` when ``_open`` raises
        ``sqlite3.OperationalError``, without propagating to the caller."""
        db_path = tmp_path / 'obs.db'
        monkeypatch.setenv('OBSERVABILITY_DB', str(db_path))
        get_config.cache_clear()

        from nephoscope.recorder.run import _ensure_db_bootstrapped

        with patch(
            'nephoscope.recorder.run._open',
            side_effect=sqlite3.OperationalError('disk I/O error'),
        ):
            result = _ensure_db_bootstrapped()

        assert result is None

    def test_bootstrapped_runtime_error_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``observations_db_path()`` raises ``RuntimeError``,
        ``_ensure_db_bootstrapped`` must return ``None`` without propagating.

        This test patches ``observations_db_path`` directly for isolation,
        complementing ``test_survives_no_path_configured`` which tests the
        same contract via full env-var removal."""
        monkeypatch.delenv("OBSERVABILITY_DB", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        get_config.cache_clear()

        from nephoscope.recorder.run import _ensure_db_bootstrapped

        with patch(
            "nephoscope.recorder.run.observations_db_path",
            side_effect=RuntimeError("no path configured"),
        ):
            result = _ensure_db_bootstrapped()

        assert result is None

    def test_bootstrapped_idempotent_on_second_call(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A second call to ``_ensure_db_bootstrapped`` after a successful first
        call must return without error and leave the DB intact."""
        db_path = tmp_path / "obs.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))
        get_config.cache_clear()

        from nephoscope.recorder.run import _ensure_db_bootstrapped

        _ensure_db_bootstrapped()
        assert db_path.exists()

        # Second call — DB already exists, must be a no-op.
        _ensure_db_bootstrapped()
        assert db_path.exists()


class TestWriteConfigFile:
    def test_empty_dict_produces_valid_toml(self, tmp_path: pathlib.Path) -> None:
        """``_write_config_file`` with an empty dict must create a valid TOML file
        that parses back to ``{}``."""
        from nephoscope.cli.init_cmd import _write_config_file

        config_path = tmp_path / 'config.toml'
        _write_config_file(config_path, {})

        assert config_path.exists()
        with config_path.open('rb') as fh:
            data = tomllib.load(fh)
        assert data == {}

    def test_long_value_round_trips(self, tmp_path: pathlib.Path) -> None:
        """A 4096-character string value must serialize without error and
        round-trip faithfully through ``tomllib.loads``."""
        from nephoscope.cli.init_cmd import _write_config_file

        long_value = 'x' * 4096
        config_path = tmp_path / 'config.toml'
        _write_config_file(config_path, {'long_key': long_value})

        with config_path.open('rb') as fh:
            data = tomllib.load(fh)
        assert data['long_key'] == long_value

    def test_escape_toml_string_rejects_control_characters(self) -> None:
        """Control characters in the ``[\x00-\x08]`` and ``[\x0a-\x1f]`` ranges
        must raise ``ValueError`` — they cannot appear in TOML basic strings."""
        from nephoscope.cli.init_cmd import _escape_toml_string

        for char in ('\x08', '\x1f'):
            with pytest.raises(ValueError, match='control character'):
                _escape_toml_string(char)

    def test_escape_toml_string_allowed_boundary_characters_round_trip(
        self,
    ) -> None:
        """Characters just inside the allowed range (tab ``\x09``, space ``\x20``,
        and backslash+quote ``\\"``) must escape without error and round-trip
        through ``tomllib.loads``."""
        from nephoscope.cli.init_cmd import _escape_toml_string

        cases = {
            'tab': '\x09',
            'space': '\x20',
            'backslash_quote': '\\"',
        }
        for label, original in cases.items():
            escaped = _escape_toml_string(original)
            toml_src = f'key = "{escaped}"\n'
            data = tomllib.loads(toml_src)
            assert data['key'] == original, (
                f'{label!r}: round-trip failed; escaped={escaped!r}, parsed={data["key"]!r}'
            )


class TestUnicodePaths:
    def test_unicode_observability_db_path_does_not_raise(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A unicode path in ``OBSERVABILITY_DB`` (diacritics, CJK, emoji) must
        not cause an encoding error during bootstrap or session-start config write."""
        unicode_dir = tmp_path / "données_観測_🔭"
        unicode_dir.mkdir()
        db_path = unicode_dir / "obs.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_dir))

        config_path = tmp_path / "config.toml"
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(config_path))
        get_config.cache_clear()

        from nephoscope.recorder.run import (
            _ensure_db_bootstrapped,
            _handle_session_start,
        )

        _ensure_db_bootstrapped()
        assert db_path.exists()

        # Config write path must also handle unicode in the db value.
        get_config.cache_clear()
        _handle_session_start({"session_id": "sess-unicode-01", "cwd": str(tmp_path)})

        assert config_path.exists()
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
        # OBSERVABILITY_DB is set so the config write uses plugin_dir path,
        # not OBSERVABILITY_DB.  Either way the file must be valid TOML.
        assert "database" in data
