"""Standalone installer for nephoscope.

Uses print() instead of logging — installer context, no logger configured.
Stdlib only: no third-party dependencies required.

Usage:
    python3 install.py                  # install from PyPI
    python3 install.py --source PATH    # install from local source directory

After installing the package the script runs ``nephoscope-init`` to bootstrap
the observations database and apply automatic permission fixtures.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path


def _check_python_version() -> None:
    """Exit 1 if the Python interpreter is older than 3.11."""
    if sys.version_info < (3, 11):
        print(
            f"Error: Python 3.11+ is required; running {sys.version}",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _resolve_plugin_data() -> Path:
    """Return the plugin data directory.

    Resolution order:
    - ``$CLAUDE_PLUGIN_DATA`` env var (set by Claude Code at hook runtime)
    - ``~/.claude/plugins/data/nephoscope-bedezign-nephoscope`` (default)
    """
    env_val = os.environ.get("CLAUDE_PLUGIN_DATA")
    if env_val:
        return Path(env_val)
    return (
        Path.home() / ".claude" / "plugins" / "data" / "nephoscope-bedezign-nephoscope"
    )


def _create_venv(plugin_data: Path) -> Path:
    """Create a venv under ``plugin_data/.venv``.

    Prints a remediation hint if venv creation fails (e.g. missing
    ``python3-venv`` on Debian-based systems) and re-raises.
    """
    venv_dir = plugin_data / ".venv"
    plugin_data.mkdir(parents=True, exist_ok=True)
    try:
        venv.create(str(venv_dir), with_pip=True, clear=False)
    except Exception:
        print(
            "Error: venv creation failed. On Debian/Ubuntu you may need:\n"
            "  sudo apt-get install python3-venv",
            file=sys.stderr,
        )
        raise
    return venv_dir


def _install_package(venv_dir: Path, source: str) -> None:
    """Run pip install inside the venv for the given source specifier."""
    pip = venv_dir / "bin" / "pip"
    subprocess.run([str(pip), "install", source], check=True)


def _cache_manifest(source: str, plugin_data: Path) -> None:
    """Copy pyproject.toml to the cached location only for local installs.

    This mirrors what bootstrap.sh does so that subsequent invocations of
    bootstrap.sh can use the diff gate to skip reinstalls.
    """
    if Path(source).is_dir():
        manifest = Path(source) / "pyproject.toml"
        if manifest.exists():
            plugin_data.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(manifest), str(plugin_data / "pyproject.toml.cached"))


def _run_init(venv_dir: Path) -> None:
    """Run ``nephoscope-init`` from the venv to bootstrap the DB."""
    init_bin = venv_dir / "bin" / "nephoscope-init"
    subprocess.run([str(init_bin)], check=True)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install nephoscope and bootstrap the observations database.",
    )
    parser.add_argument(
        "--source",
        default="nephoscope",
        help=(
            "Package source: PyPI package name (default) or path to local "
            "source directory for development installs."
        ),
    )
    args = parser.parse_args(argv)

    _check_python_version()

    plugin_data = _resolve_plugin_data()
    print(f"Installing nephoscope into {plugin_data}")

    venv_dir = _create_venv(plugin_data)
    print(f"Virtual environment: {venv_dir}")

    print(f"Installing package from: {args.source}")
    _install_package(venv_dir, args.source)

    _cache_manifest(args.source, plugin_data)

    print("Running nephoscope-init ...")
    _run_init(venv_dir)

    print("Installation complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
