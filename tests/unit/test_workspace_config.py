"""Tests for nephoscope.config — typed lazy config loader."""

from __future__ import annotations

import tomllib
from collections.abc import Generator
from pathlib import Path

import pytest

from nephoscope.config import NephoscopeConfig, get_config


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


class TestAbsentConfigReturnsDefaults:
    def test_trusted_dirs_default(self, isolated_config: Path) -> None:
        assert not isolated_config.exists()
        config = get_config()
        assert config.trusted_dirs == []

    def test_auto_register_project_paths_default(self, isolated_config: Path) -> None:
        assert not isolated_config.exists()
        config = get_config()
        assert config.auto_register_project_paths is False

    def test_non_bash_tool_matching_default(self, isolated_config: Path) -> None:
        assert not isolated_config.exists()
        config = get_config()
        assert config.non_bash_tool_matching is False


class TestTomlFieldLoading:
    def test_trusted_dirs_loaded(self, isolated_config: Path) -> None:
        isolated_config.write_text('trusted_dirs = ["/tmp/project"]\n')
        config = get_config()
        assert config.trusted_dirs == ["/tmp/project"]

    def test_non_bash_tool_matching_loaded(self, isolated_config: Path) -> None:
        isolated_config.write_text("non_bash_tool_matching = true\n")
        config = get_config()
        assert config.non_bash_tool_matching is True

    def test_auto_register_project_paths_loaded(self, isolated_config: Path) -> None:
        isolated_config.write_text("auto_register_project_paths = true\n")
        config = get_config()
        assert config.auto_register_project_paths is True

    def test_multiple_trusted_dirs(self, isolated_config: Path) -> None:
        isolated_config.write_text(
            'trusted_dirs = ["/home/user/work", "/tmp/projects"]\n'
        )
        config = get_config()
        assert config.trusted_dirs == ["/home/user/work", "/tmp/projects"]

    def test_all_fields_loaded_together(self, isolated_config: Path) -> None:
        isolated_config.write_text(
            'trusted_dirs = ["/tmp/ws"]\n'
            "auto_register_project_paths = true\n"
            "non_bash_tool_matching = true\n"
        )
        config = get_config()
        assert config.trusted_dirs == ["/tmp/ws"]
        assert config.auto_register_project_paths is True
        assert config.non_bash_tool_matching is True


class TestCacheBehavior:
    def test_cache_is_per_config_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Changing NEPHOSCOPE_CONFIG after cache_clear loads from the new path."""
        config_a = tmp_path / "a.toml"
        config_a.write_text('trusted_dirs = ["/tmp/alpha"]\n')
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(config_a))
        result_a = get_config()
        assert result_a.trusted_dirs == ["/tmp/alpha"]

        get_config.cache_clear()

        config_b = tmp_path / "b.toml"
        config_b.write_text('trusted_dirs = ["/tmp/beta"]\n')
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(config_b))
        result_b = get_config()
        assert result_b.trusted_dirs == ["/tmp/beta"]

    def test_cache_returns_same_object_on_second_call(
        self, isolated_config: Path
    ) -> None:
        """lru_cache returns identical object without re-reading disk."""
        isolated_config.write_text('trusted_dirs = ["/tmp/cached"]\n')
        first = get_config()
        second = get_config()
        assert first is second


class TestEnvVarRouting:
    def test_nephoscope_config_env_var_respected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        custom_path = tmp_path / "custom.toml"
        custom_path.write_text('trusted_dirs = ["/tmp/custom"]\n')
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(custom_path))
        config = get_config()
        assert config.trusted_dirs == ["/tmp/custom"]

    def test_env_var_takes_precedence_over_default_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """NEPHOSCOPE_CONFIG overrides the default ~/.config path."""
        custom_path = tmp_path / "override.toml"
        custom_path.write_text('trusted_dirs = ["/tmp/override"]\n')
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(custom_path))
        config = get_config()
        assert config.trusted_dirs == ["/tmp/override"]


class TestEmptyTomlFile:
    def test_empty_toml_returns_defaults(self, isolated_config: Path) -> None:
        """A zero-byte (or blank) TOML file must not crash; all fields
        must fall back to their defaults."""
        isolated_config.write_text("")
        config = get_config()
        assert config.trusted_dirs == []
        assert config.auto_register_project_paths is False
        assert config.non_bash_tool_matching is False

    def test_whitespace_only_toml_returns_defaults(self, isolated_config: Path) -> None:
        """A file containing only whitespace is valid TOML (empty document);
        get_config() must return defaults without raising."""
        isolated_config.write_text("   \n\n  \n")
        config = get_config()
        assert isinstance(config, NephoscopeConfig)
        assert config.trusted_dirs == []


class TestMalformedToml:
    def test_malformed_toml_raises_decode_error(self, isolated_config: Path) -> None:
        isolated_config.write_text("trusted_dirs = [not valid toml\n")
        with pytest.raises(tomllib.TOMLDecodeError):
            get_config()


class TestLazyLoadInvariant:
    def test_get_config_is_lru_cached(self) -> None:
        """get_config must be wrapped in lru_cache, proving lazy-load semantics."""
        assert hasattr(get_config, "cache_clear"), (
            "get_config must be decorated with @functools.lru_cache"
        )

    def test_module_returns_nephoscope_config_type(self, isolated_config: Path) -> None:
        """get_config() always returns a NephoscopeConfig instance."""
        config = get_config()
        assert isinstance(config, NephoscopeConfig)


class TestWorkspaceRootsStoredAsStrings:
    def test_paths_returned_as_strings_not_path_objects(
        self, isolated_config: Path
    ) -> None:
        isolated_config.write_text('trusted_dirs = ["/tmp/ws"]\n')
        config = get_config()
        assert all(isinstance(p, str) for p in config.trusted_dirs)

    def test_trusted_dirs_not_resolved_by_loader(self, isolated_config: Path) -> None:
        """The loader must store paths verbatim — callers resolve lazily."""
        isolated_config.write_text('trusted_dirs = ["~/projects"]\n')
        config = get_config()
        assert config.trusted_dirs == ["~/projects"]
