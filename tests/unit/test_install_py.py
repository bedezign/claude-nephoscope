"""Tests for install.py — stdlib-only first-run installer.

All subprocess calls are mocked. Covers:
- _check_python_version exits 1 on (3, 10)
- _resolve_plugin_data honours env var; defaults to Path.home()/... when unset
- --source /local/path → pip install with local path + pyproject.toml.cached written
- No --source → pip install "nephoscope" + pyproject.toml.cached NOT written
- Both paths invoke nephoscope-init
- _create_venv failure prints hint about python3-venv and re-raises
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Load install.py as a module without importing it as a package member
_INSTALL_PY = Path(__file__).resolve().parents[2] / "install.py"


def _load_install():
    """Import install.py from repo root as a fresh module each time."""
    spec = importlib.util.spec_from_file_location("install_py_under_test", _INSTALL_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# _check_python_version
# ---------------------------------------------------------------------------


class TestCheckPythonVersion:
    def test_exits_on_310(self):
        install = _load_install()
        with patch.object(sys, "version_info", (3, 10, 0, "final", 0)):
            with pytest.raises(SystemExit) as exc_info:
                install._check_python_version()
        assert exc_info.value.code == 1

    def test_exits_on_310_with_high_patch(self):
        # (3, 10, 99) < (3, 11) is True — confirms the guard is < (3, 11), not <= (3, 10)
        install = _load_install()
        with patch.object(sys, "version_info", (3, 10, 99, "final", 0)):
            with pytest.raises(SystemExit) as exc_info:
                install._check_python_version()
        assert exc_info.value.code == 1

    def test_passes_on_311(self):
        install = _load_install()
        with patch.object(sys, "version_info", (3, 11, 0, "final", 0)):
            install._check_python_version()  # must not raise

    def test_passes_on_312(self):
        install = _load_install()
        with patch.object(sys, "version_info", (3, 12, 0, "final", 0)):
            install._check_python_version()  # must not raise


# ---------------------------------------------------------------------------
# _resolve_plugin_data
# ---------------------------------------------------------------------------


class TestResolvePluginData:
    def test_honours_env_var(self, monkeypatch, tmp_path):
        install = _load_install()
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        result = install._resolve_plugin_data()
        assert result == tmp_path

    def test_defaults_to_home_path(self, monkeypatch):
        install = _load_install()
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        result = install._resolve_plugin_data()
        expected = (
            Path.home()
            / ".claude"
            / "plugins"
            / "data"
            / "nephoscope-bedezign-nephoscope"
        )
        assert result == expected


# ---------------------------------------------------------------------------
# _install_package + _cache_manifest
# ---------------------------------------------------------------------------


class TestInstallPackage:
    def test_local_source_pip_installs_path(self, tmp_path):
        install = _load_install()
        venv_dir = tmp_path / ".venv"
        venv_dir.mkdir()
        pip = venv_dir / "bin" / "pip"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            install._install_package(venv_dir, "/some/local/path")

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert str(pip) in cmd or cmd[0] == str(pip)
        assert "/some/local/path" in cmd

    def test_pypi_source_pip_installs_nephoscope(self, tmp_path):
        install = _load_install()
        venv_dir = tmp_path / ".venv"
        venv_dir.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            install._install_package(venv_dir, "nephoscope")

        cmd = mock_run.call_args[0][0]
        assert "nephoscope" in cmd

    def test_pip_failure_propagates(self, tmp_path):
        install = _load_install()
        venv_dir = tmp_path / ".venv"
        venv_dir.mkdir()
        error = subprocess.CalledProcessError(1, "pip")

        with patch("subprocess.run", side_effect=error):
            with pytest.raises(subprocess.CalledProcessError):
                install._install_package(venv_dir, "nephoscope")


class TestCacheManifest:
    def test_local_source_writes_cached(self, tmp_path):
        install = _load_install()
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        pyproject = source_dir / "pyproject.toml"
        pyproject.write_text('[project]\nname = "test"\n')
        plugin_data = tmp_path / "plugin_data"
        plugin_data.mkdir()

        install._cache_manifest(str(source_dir), plugin_data)

        cached = plugin_data / "pyproject.toml.cached"
        assert cached.exists(), (
            "pyproject.toml.cached should be written for local source"
        )

    def test_pypi_source_does_not_write_cached(self, tmp_path):
        install = _load_install()
        plugin_data = tmp_path / "plugin_data"
        plugin_data.mkdir()

        install._cache_manifest("nephoscope", plugin_data)

        cached = plugin_data / "pyproject.toml.cached"
        assert not cached.exists(), (
            "pyproject.toml.cached must NOT be written for PyPI source"
        )


# ---------------------------------------------------------------------------
# _run_init
# ---------------------------------------------------------------------------


class TestRunInit:
    def test_invokes_nephoscope_init(self, tmp_path):
        install = _load_install()
        venv_dir = tmp_path / ".venv"
        venv_dir.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            install._run_init(venv_dir)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert any("nephoscope-init" in str(c) for c in cmd), (
            f"Expected nephoscope-init in command, got {cmd!r}"
        )

    def test_init_failure_propagates(self, tmp_path):
        install = _load_install()
        venv_dir = tmp_path / ".venv"
        venv_dir.mkdir()
        error = subprocess.CalledProcessError(1, "nephoscope-init")

        with patch("subprocess.run", side_effect=error):
            with pytest.raises(subprocess.CalledProcessError):
                install._run_init(venv_dir)


# ---------------------------------------------------------------------------
# _main orchestration — both code paths call nephoscope-init
# ---------------------------------------------------------------------------


class TestMainOrchestration:
    def _run_main(self, argv, monkeypatch, tmp_path):
        install = _load_install()
        plugin_data = tmp_path / "plugin_data"
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))

        with (
            patch.object(sys, "version_info", (3, 11, 0, "final", 0)),
            patch.object(install, "_create_venv", return_value=tmp_path / ".venv"),
            patch.object(install, "_install_package"),
            patch.object(install, "_cache_manifest"),
            patch.object(install, "_run_init") as mock_init,
        ):
            rc = install._main(argv)

        return rc, mock_init

    def test_local_source_calls_init(self, tmp_path, monkeypatch):
        rc, mock_init = self._run_main(
            ["--source", "/some/path"], monkeypatch, tmp_path
        )
        assert rc == 0
        mock_init.assert_called_once()

    def test_no_source_calls_init(self, tmp_path, monkeypatch):
        rc, mock_init = self._run_main([], monkeypatch, tmp_path)
        assert rc == 0
        mock_init.assert_called_once()


# ---------------------------------------------------------------------------
# Step 22: _create_venv failure prints hint about python3-venv and re-raises
# ---------------------------------------------------------------------------


class TestCreateVenvErrorHint:
    def test_failure_prints_python3_venv_hint_and_reraises(self, tmp_path, capsys):
        install = _load_install()
        plugin_data = tmp_path / "plugin_data"
        plugin_data.mkdir()

        with patch("venv.create", side_effect=Exception("venv error")):
            with pytest.raises(Exception, match="venv error"):
                install._create_venv(plugin_data)

        captured = capsys.readouterr()
        assert "python3-venv" in captured.out or "python3-venv" in captured.err, (
            'Expected hint mentioning "python3-venv" on failure'
        )
