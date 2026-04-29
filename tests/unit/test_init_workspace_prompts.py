"""Tests for workspace_roots interactive prompting in nephoscope-init."""

from __future__ import annotations

import argparse
import os
import tomllib
from collections.abc import Generator
from pathlib import Path

import pytest

from nephoscope.config import get_config


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_config_cache() -> Generator[None, None, None]:
    """Wipe lru_cache before and after every test to prevent cross-test pollution."""
    get_config.cache_clear()
    yield
    get_config.cache_clear()


@pytest.fixture
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point NEPHOSCOPE_CONFIG at a non-existent path in tmp_path."""
    config_path = tmp_path / "config.toml"
    monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(config_path))
    return config_path


def _make_args(
    *,
    no_workspace_prompts: bool = False,
    db_path: str | None = None,
) -> argparse.Namespace:
    """Build a minimal argparse.Namespace matching what the CLI parser produces."""
    return argparse.Namespace(
        no_workspace_prompts=no_workspace_prompts,
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# Helper to call the function under test
# ---------------------------------------------------------------------------


def _run(args: argparse.Namespace) -> None:
    from nephoscope.cli.init_cmd import _configure_workspace_roots

    _configure_workspace_roots(args)


# ---------------------------------------------------------------------------
# 1. Prompts skipped when workspace_roots already configured
# ---------------------------------------------------------------------------


class TestSkipsWhenAlreadyConfigured:
    def test_no_input_called_when_roots_present(
        self,
        isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """input() must never be called when workspace_roots is non-empty."""
        isolated_config.write_text('trusted_dirs = ["/existing/path"]\n')

        monkeypatch.setattr("builtins.input", lambda _: pytest.fail("input() called"))

        _run(_make_args())

    def test_config_unchanged_when_roots_present(
        self,
        isolated_config: Path,
    ) -> None:
        """Config must not be written when workspace_roots already set."""
        original = 'trusted_dirs = ["/existing/path"]\n'
        isolated_config.write_text(original)
        original_mtime = isolated_config.stat().st_mtime

        _run(_make_args())

        assert isolated_config.read_text() == original
        assert isolated_config.stat().st_mtime == original_mtime


# ---------------------------------------------------------------------------
# 2. Auto-register adds CWD when auto_register_project_paths=True
# ---------------------------------------------------------------------------


class TestAutoRegister:
    def test_cwd_added_when_auto_register_on(
        self,
        isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """CWD is written to workspace_roots when auto_register_project_paths=true."""
        isolated_config.write_text("auto_register_project_paths = true\n")
        monkeypatch.chdir(tmp_path)

        _run(_make_args())

        get_config.cache_clear()
        config = get_config()
        expected = os.path.realpath(str(tmp_path))
        assert expected in config.trusted_dirs

    def test_auto_register_does_not_prompt(
        self,
        isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Auto-register must not call input()."""
        isolated_config.write_text("auto_register_project_paths = true\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("builtins.input", lambda _: pytest.fail("input() called"))

        _run(_make_args())


# ---------------------------------------------------------------------------
# 3. Interactive prompt accepts a valid path
# ---------------------------------------------------------------------------


class TestInteractivePrompt:
    def test_valid_path_written_to_config(
        self,
        isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A valid directory entered interactively is saved to workspace_roots."""
        valid_dir = tmp_path / "myproject"
        valid_dir.mkdir()

        inputs = iter([str(valid_dir), ""])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        _run(_make_args())

        get_config.cache_clear()
        config = get_config()
        expected = os.path.realpath(str(valid_dir))
        assert expected in config.trusted_dirs


# ---------------------------------------------------------------------------
# 4. Invalid path (non-existent dir) is skipped with warning
# ---------------------------------------------------------------------------


class TestInvalidPathSkipped:
    def test_nonexistent_path_not_written(
        self,
        isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A non-existent directory is skipped and a warning is printed."""
        inputs = iter(["/nonexistent/path/that/cannot/exist", ""])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        _run(_make_args())

        # Config must not have been created (no valid paths collected)
        assert not isolated_config.exists()

        # A warning must be emitted to stderr
        captured = capsys.readouterr()
        assert "not a directory" in captured.err


# ---------------------------------------------------------------------------
# 5. --no-workspace-prompts skips everything
# ---------------------------------------------------------------------------


class TestNoWorkspacePromptsFlag:
    def test_config_not_written_with_flag(
        self,
        isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--no-workspace-prompts must skip config writes entirely."""
        monkeypatch.setattr("builtins.input", lambda _: pytest.fail("input() called"))

        _run(_make_args(no_workspace_prompts=True))

        assert not isolated_config.exists()

    def test_no_prompts_flag_overrides_auto_register(
        self,
        isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """--no-workspace-prompts wins even when auto_register_project_paths=true."""
        isolated_config.write_text("auto_register_project_paths = true\n")
        monkeypatch.chdir(tmp_path)

        _run(_make_args(no_workspace_prompts=True))

        get_config.cache_clear()
        config = get_config()
        assert config.trusted_dirs == []


# ---------------------------------------------------------------------------
# 6. Empty input (just Enter) writes nothing
# ---------------------------------------------------------------------------


class TestEmptyInputWritesNothing:
    def test_blank_enter_does_not_create_config(
        self,
        isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pressing Enter immediately without any path must not create the config file."""
        monkeypatch.setattr("builtins.input", lambda _: "")

        _run(_make_args())

        assert not isolated_config.exists()


# ---------------------------------------------------------------------------
# 7. Config file created if absent
# ---------------------------------------------------------------------------


class TestConfigFileCreatedIfAbsent:
    def test_config_file_created_with_valid_path(
        self,
        isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Config file must be created when it does not exist and a valid path is given."""
        assert not isolated_config.exists()

        valid_dir = tmp_path / "newproject"
        valid_dir.mkdir()

        inputs = iter([str(valid_dir), ""])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        _run(_make_args())

        assert isolated_config.exists()
        get_config.cache_clear()
        config = get_config()
        assert len(config.trusted_dirs) == 1
        assert config.trusted_dirs[0] == os.path.realpath(str(valid_dir))


# ---------------------------------------------------------------------------
# 8. Existing non-workspace-roots config keys preserved
# ---------------------------------------------------------------------------


class TestExistingKeysPreserved:
    def test_non_bash_tool_matching_survives_workspace_write(
        self,
        isolated_config: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Writing workspace_roots must not clobber other config keys."""
        isolated_config.write_text("non_bash_tool_matching = true\n")

        valid_dir = tmp_path / "project"
        valid_dir.mkdir()

        inputs = iter([str(valid_dir), ""])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        _run(_make_args())

        get_config.cache_clear()
        config = get_config()
        assert config.non_bash_tool_matching is True
        expected = os.path.realpath(str(valid_dir))
        assert expected in config.trusted_dirs


# ---------------------------------------------------------------------------
# 9. EOFError from input() terminates the loop and returns empty list
# ---------------------------------------------------------------------------


class TestPromptForPathsEOF:
    def test_prompt_for_paths_eof_returns_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When input() raises EOFError on the first call, _prompt_for_paths()
        must break out of the loop and return an empty list."""
        from nephoscope.cli.init_cmd import _prompt_for_paths

        monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(EOFError))

        result = _prompt_for_paths()

        assert result == []


# ---------------------------------------------------------------------------
# 10. _write_config_file TOML string escaping
# ---------------------------------------------------------------------------


class TestWriteConfigFileEscaping:
    def test_str_value_with_double_quote_round_trips(self, tmp_path: Path) -> None:
        """A str value containing a double-quote must survive a TOML round-trip."""
        from nephoscope.cli.init_cmd import _write_config_file

        config_file = tmp_path / "config.toml"
        _write_config_file(config_file, {"some_key": 'value with "quotes"'})

        with config_file.open("rb") as f:
            loaded = tomllib.load(f)

        assert loaded["some_key"] == 'value with "quotes"'

    def test_str_value_with_backslash_round_trips(self, tmp_path: Path) -> None:
        """A str value containing a backslash must survive a TOML round-trip."""
        from nephoscope.cli.init_cmd import _write_config_file

        config_file = tmp_path / "config.toml"
        _write_config_file(config_file, {"some_key": "C:\\Users\\me"})

        with config_file.open("rb") as f:
            loaded = tomllib.load(f)

        assert loaded["some_key"] == "C:\\Users\\me"

    def test_list_value_with_double_quote_round_trips(self, tmp_path: Path) -> None:
        """List elements containing double-quotes must survive a TOML round-trip."""
        from nephoscope.cli.init_cmd import _write_config_file

        config_file = tmp_path / "config.toml"
        _write_config_file(config_file, {"trusted_dirs": ['/home/user/my "project"']})

        with config_file.open("rb") as f:
            loaded = tomllib.load(f)

        assert loaded["trusted_dirs"] == ['/home/user/my "project"']

    def test_list_value_with_backslash_round_trips(self, tmp_path: Path) -> None:
        """List elements containing backslashes must survive a TOML round-trip."""
        from nephoscope.cli.init_cmd import _write_config_file

        config_file = tmp_path / "config.toml"
        _write_config_file(config_file, {"trusted_dirs": ["C:\\Projects\\app"]})

        with config_file.open("rb") as f:
            loaded = tomllib.load(f)

        assert loaded["trusted_dirs"] == ["C:\\Projects\\app"]
