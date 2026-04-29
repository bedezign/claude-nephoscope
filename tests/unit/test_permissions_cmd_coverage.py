"""Unit tests for _print_workspace_coverage in permissions_cmd.

Tests the Workspace Coverage section added to the mirror-status output.
The coverage helper reads workspace_roots from get_config() and checks
_nephoscopeAllowedTools in the global settings.json to determine coverage.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path

import pytest

from nephoscope.config import get_config


@pytest.fixture(autouse=True)
def _clear_config_cache() -> Generator[None, None, None]:
    """Clear lru_cache before and after each test to prevent cross-test pollution."""
    get_config.cache_clear()
    yield
    get_config.cache_clear()


@pytest.fixture
def config_with_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Factory fixture: returns a function that writes config and returns the config path."""

    def _make(roots: list[str]) -> Path:
        config_path = tmp_path / "config.toml"
        if roots:
            roots_toml = ", ".join(f'"{r}"' for r in roots)
            config_path.write_text(f"trusted_dirs = [{roots_toml}]\n")
        else:
            config_path.write_text("")
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(config_path))
        get_config.cache_clear()
        return config_path

    return _make


def _make_settings_json(tmp_path: Path, nephoscope_tools: list[str]) -> Path:
    """Write a minimal settings.json with _nephoscopeAllowedTools and return the path."""
    settings_path = tmp_path / "settings.json"
    data: dict = {}
    if nephoscope_tools is not None:
        data["_nephoscopeAllowedTools"] = nephoscope_tools
    settings_path.write_text(json.dumps(data))
    return settings_path


class TestEmptyWorkspaceRootsOmitsSection:
    def test_empty_roots_omits_section(
        self,
        capsys: pytest.CaptureFixture,
        config_with_roots,
        tmp_path: Path,
    ) -> None:
        """When workspace_roots is empty, no coverage section is printed."""
        config_with_roots([])
        settings_path = _make_settings_json(tmp_path, [])

        from nephoscope.cli.permissions_cmd import _print_workspace_coverage

        _print_workspace_coverage(settings_path)
        captured = capsys.readouterr()
        assert "Workspace coverage" not in captured.out

    def test_empty_roots_produces_no_output(
        self,
        capsys: pytest.CaptureFixture,
        config_with_roots,
        tmp_path: Path,
    ) -> None:
        """Empty workspace_roots produces no stdout at all."""
        config_with_roots([])
        settings_path = _make_settings_json(tmp_path, [])

        from nephoscope.cli.permissions_cmd import _print_workspace_coverage

        _print_workspace_coverage(settings_path)
        captured = capsys.readouterr()
        assert captured.out == ""


class TestCoveredRootShowsCheckmark:
    def test_covered_root_shows_checkmark(
        self,
        capsys: pytest.CaptureFixture,
        config_with_roots,
        tmp_path: Path,
    ) -> None:
        """A root whose Write entry is in _nephoscopeAllowedTools shows checkmark."""
        ws = tmp_path / "ws"
        ws.mkdir()
        config_with_roots([str(ws)])
        tools = [
            f"Write({ws}/**)",
            f"Edit({ws}/**)",
            f"Read({ws}/**)",
        ]
        settings_path = _make_settings_json(tmp_path, tools)

        from nephoscope.cli.permissions_cmd import _print_workspace_coverage

        _print_workspace_coverage(settings_path)
        captured = capsys.readouterr()
        assert "✓" in captured.out
        assert "✗" not in captured.out

    def test_covered_root_shows_resolved_path(
        self,
        capsys: pytest.CaptureFixture,
        config_with_roots,
        tmp_path: Path,
    ) -> None:
        """The resolved absolute path of the root is shown in output."""
        import os

        ws = tmp_path / "ws"
        ws.mkdir()
        resolved = os.path.realpath(str(ws))
        config_with_roots([str(ws)])
        tools = [
            f"Write({resolved}/**)",
            f"Edit({resolved}/**)",
            f"Read({resolved}/**)",
        ]
        settings_path = _make_settings_json(tmp_path, tools)

        from nephoscope.cli.permissions_cmd import _print_workspace_coverage

        _print_workspace_coverage(settings_path)
        captured = capsys.readouterr()
        assert resolved in captured.out


class TestUncoveredRootShowsCross:
    def test_uncovered_root_shows_cross(
        self,
        capsys: pytest.CaptureFixture,
        config_with_roots,
        tmp_path: Path,
    ) -> None:
        """A root without a Write entry in _nephoscopeAllowedTools shows cross."""
        ws = tmp_path / "uncovered"
        ws.mkdir()
        config_with_roots([str(ws)])
        # Settings exists but has entries for a different path
        tools = ["Write(/some/other/path/**)", "Edit(/some/other/path/**)"]
        settings_path = _make_settings_json(tmp_path, tools)

        from nephoscope.cli.permissions_cmd import _print_workspace_coverage

        _print_workspace_coverage(settings_path)
        captured = capsys.readouterr()
        assert "✗" in captured.out
        assert "✓" not in captured.out

    def test_empty_nephoscope_tools_key_shows_cross(
        self,
        capsys: pytest.CaptureFixture,
        config_with_roots,
        tmp_path: Path,
    ) -> None:
        """_nephoscopeAllowedTools present but empty counts as uncovered."""
        ws = tmp_path / "ws"
        ws.mkdir()
        config_with_roots([str(ws)])
        settings_path = _make_settings_json(tmp_path, [])

        from nephoscope.cli.permissions_cmd import _print_workspace_coverage

        _print_workspace_coverage(settings_path)
        captured = capsys.readouterr()
        assert "✗" in captured.out


class TestMissingSettingsJsonShowsUncovered:
    def test_missing_settings_json_shows_cross(
        self,
        capsys: pytest.CaptureFixture,
        config_with_roots,
        tmp_path: Path,
    ) -> None:
        """When settings.json does not exist, root is shown as uncovered."""
        ws = tmp_path / "ws"
        ws.mkdir()
        config_with_roots([str(ws)])
        settings_path = tmp_path / "nonexistent_settings.json"
        assert not settings_path.exists()

        from nephoscope.cli.permissions_cmd import _print_workspace_coverage

        _print_workspace_coverage(settings_path)
        captured = capsys.readouterr()
        assert "✗" in captured.out
        assert "✓" not in captured.out

    def test_missing_settings_json_shows_hint(
        self,
        capsys: pytest.CaptureFixture,
        config_with_roots,
        tmp_path: Path,
    ) -> None:
        """Missing settings.json triggers the reconcile hint line."""
        ws = tmp_path / "ws"
        ws.mkdir()
        config_with_roots([str(ws)])
        settings_path = tmp_path / "nonexistent_settings.json"

        from nephoscope.cli.permissions_cmd import _print_workspace_coverage

        _print_workspace_coverage(settings_path)
        captured = capsys.readouterr()
        assert "nephoscope-permissions reconcile" in captured.out


class TestHintLinePresenceAbsence:
    def test_hint_shown_when_any_uncovered(
        self,
        capsys: pytest.CaptureFixture,
        config_with_roots,
        tmp_path: Path,
    ) -> None:
        """Hint line appears when at least one root is uncovered."""
        ws = tmp_path / "ws"
        ws.mkdir()
        config_with_roots([str(ws)])
        settings_path = _make_settings_json(tmp_path, [])

        from nephoscope.cli.permissions_cmd import _print_workspace_coverage

        _print_workspace_coverage(settings_path)
        captured = capsys.readouterr()
        assert "nephoscope-permissions reconcile" in captured.out

    def test_hint_absent_when_all_covered(
        self,
        capsys: pytest.CaptureFixture,
        config_with_roots,
        tmp_path: Path,
    ) -> None:
        """Hint line absent when all roots are covered."""
        import os

        ws = tmp_path / "ws"
        ws.mkdir()
        resolved = os.path.realpath(str(ws))
        config_with_roots([str(ws)])
        tools = [
            f"Write({resolved}/**)",
            f"Edit({resolved}/**)",
            f"Read({resolved}/**)",
        ]
        settings_path = _make_settings_json(tmp_path, tools)

        from nephoscope.cli.permissions_cmd import _print_workspace_coverage

        _print_workspace_coverage(settings_path)
        captured = capsys.readouterr()
        assert "nephoscope-permissions reconcile" not in captured.out


class TestMultipleRootsMixed:
    def test_two_roots_one_covered_one_not(
        self,
        capsys: pytest.CaptureFixture,
        config_with_roots,
        tmp_path: Path,
    ) -> None:
        """With two roots (one covered, one not), both symbols appear in output."""
        import os

        ws_covered = tmp_path / "covered"
        ws_covered.mkdir()
        ws_uncovered = tmp_path / "uncovered"
        ws_uncovered.mkdir()
        resolved_covered = os.path.realpath(str(ws_covered))

        config_with_roots([str(ws_covered), str(ws_uncovered)])
        tools = [
            f"Write({resolved_covered}/**)",
            f"Edit({resolved_covered}/**)",
            f"Read({resolved_covered}/**)",
        ]
        settings_path = _make_settings_json(tmp_path, tools)

        from nephoscope.cli.permissions_cmd import _print_workspace_coverage

        _print_workspace_coverage(settings_path)
        captured = capsys.readouterr()
        assert "✓" in captured.out
        assert "✗" in captured.out
        assert "nephoscope-permissions reconcile" in captured.out

    def test_two_roots_both_covered_no_hint(
        self,
        capsys: pytest.CaptureFixture,
        config_with_roots,
        tmp_path: Path,
    ) -> None:
        """With two covered roots, hint line is absent."""
        import os

        ws_a = tmp_path / "alpha"
        ws_a.mkdir()
        ws_b = tmp_path / "beta"
        ws_b.mkdir()
        resolved_a = os.path.realpath(str(ws_a))
        resolved_b = os.path.realpath(str(ws_b))

        config_with_roots([str(ws_a), str(ws_b)])
        tools = [
            f"Write({resolved_a}/**)",
            f"Edit({resolved_a}/**)",
            f"Read({resolved_a}/**)",
            f"Write({resolved_b}/**)",
            f"Edit({resolved_b}/**)",
            f"Read({resolved_b}/**)",
        ]
        settings_path = _make_settings_json(tmp_path, tools)

        from nephoscope.cli.permissions_cmd import _print_workspace_coverage

        _print_workspace_coverage(settings_path)
        captured = capsys.readouterr()
        assert "nephoscope-permissions reconcile" not in captured.out
        assert captured.out.count("✓") == 2


class TestTildeExpansion:
    def test_tilde_in_workspace_root_is_resolved(
        self,
        capsys: pytest.CaptureFixture,
        config_with_roots,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Tilde in workspace_roots is expanded and resolved before matching."""
        import os

        # Point HOME at tmp_path so '~' expands predictably
        monkeypatch.setenv("HOME", str(tmp_path))
        ws = tmp_path / "projects"
        ws.mkdir()
        resolved = os.path.realpath(str(ws))

        config_with_roots(["~/projects"])
        tools = [
            f"Write({resolved}/**)",
            f"Edit({resolved}/**)",
            f"Read({resolved}/**)",
        ]
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"_nephoscopeAllowedTools": tools}))

        from nephoscope.cli.permissions_cmd import _print_workspace_coverage

        _print_workspace_coverage(settings_path)
        captured = capsys.readouterr()
        assert "✓" in captured.out


class TestMalformedSettingsJson:
    def test_malformed_json_treats_root_as_uncovered(
        self,
        capsys: pytest.CaptureFixture,
        config_with_roots,
        tmp_path: Path,
    ) -> None:
        """Malformed settings.json does not crash; roots shown as uncovered."""
        ws = tmp_path / "ws"
        ws.mkdir()
        config_with_roots([str(ws)])
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{invalid json here")

        from nephoscope.cli.permissions_cmd import _print_workspace_coverage

        _print_workspace_coverage(settings_path)
        captured = capsys.readouterr()
        assert "✗" in captured.out
        assert "nephoscope-permissions reconcile" in captured.out

    def test_malformed_json_does_not_raise(
        self,
        config_with_roots,
        tmp_path: Path,
    ) -> None:
        """Malformed settings.json must not propagate an exception."""
        ws = tmp_path / "ws"
        ws.mkdir()
        config_with_roots([str(ws)])
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{invalid json")

        from nephoscope.cli.permissions_cmd import _print_workspace_coverage

        # Must not raise
        _print_workspace_coverage(settings_path)


class TestMissingNephoscopeToolsKey:
    def test_no_key_in_settings_json_shows_uncovered(
        self,
        capsys: pytest.CaptureFixture,
        config_with_roots,
        tmp_path: Path,
    ) -> None:
        """settings.json without _nephoscopeAllowedTools key treats all roots as uncovered."""
        ws = tmp_path / "ws"
        ws.mkdir()
        config_with_roots([str(ws)])
        settings_path = tmp_path / "settings.json"
        # File exists but has no _nephoscopeAllowedTools key
        settings_path.write_text(json.dumps({"permissions": {"allow": [], "deny": []}}))

        from nephoscope.cli.permissions_cmd import _print_workspace_coverage

        _print_workspace_coverage(settings_path)
        captured = capsys.readouterr()
        assert "✗" in captured.out
        assert "✓" not in captured.out


class TestSectionHeader:
    def test_section_header_present_when_roots_exist(
        self,
        capsys: pytest.CaptureFixture,
        config_with_roots,
        tmp_path: Path,
    ) -> None:
        """'Workspace coverage:' header appears when workspace_roots is non-empty."""
        ws = tmp_path / "ws"
        ws.mkdir()
        config_with_roots([str(ws)])
        settings_path = _make_settings_json(tmp_path, [])

        from nephoscope.cli.permissions_cmd import _print_workspace_coverage

        _print_workspace_coverage(settings_path)
        captured = capsys.readouterr()
        assert "Workspace coverage:" in captured.out
