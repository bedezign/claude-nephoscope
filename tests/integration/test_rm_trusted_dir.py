"""Integration tests: rm with $TRUSTED_DIR fixture in the match pipeline.

Exercises:
  - rm -rf under a configured trusted_dir → Allow (seeded safe_shapes rule fires)
  - rm -rf outside all trusted_dirs → Ask (ask_flag_patterns still active)
  - rm -f outside trusted_dirs → Ask (deny.yaml ask_flag_patterns still active)
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from nephoscope.config import get_config
from nephoscope.learners.permission.match import Verdict, dispatch
from nephoscope.learners.permission.seed import apply_fixtures

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SAFE_SHAPES = (
    PROJECT_ROOT
    / "src"
    / "nephoscope"
    / "learners"
    / "permission"
    / "config"
    / "fixtures"
    / "safe_shapes.yaml"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, trusted_dirs: list[str]) -> Path:
    dirs_toml = "[" + ", ".join(f'"{d}"' for d in trusted_dirs) + "]"
    content = textwrap.dedent(f"""\
        trusted_dirs = {dirs_toml}
    """)
    cfg_path = tmp_path / "nephoscope-config.toml"
    cfg_path.write_text(content)
    return cfg_path


def _configure(monkeypatch, tmp_path: Path, trusted_dirs: list[str]) -> None:
    cfg_path = _write_config(tmp_path, trusted_dirs)
    monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg_path))
    get_config.cache_clear()


@pytest.fixture(autouse=True)
def _config_isolation(monkeypatch, tmp_path):
    get_config.cache_clear()
    yield
    get_config.cache_clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRmInsideTrustedDir:
    def test_rm_rf_inside_trusted_dir_returns_allow(
        self, monkeypatch, tmp_path, tmp_db
    ) -> None:
        """rm -rf /trusted/dir/foo → Allow when /trusted/dir is a trusted_dir."""
        _configure(monkeypatch, tmp_path, ["/trusted/dir"])
        apply_fixtures(tmp_db, SAFE_SHAPES)

        verdict = dispatch(
            tool_name="Bash",
            tool_input={"command": "rm -rf /trusted/dir/foo"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
        )
        assert verdict == Verdict.Allow, (
            f"rm -rf inside trusted_dir must return Allow; got {verdict}"
        )

    def test_rm_r_inside_trusted_dir_returns_allow(
        self, monkeypatch, tmp_path, tmp_db
    ) -> None:
        """rm -r /trusted/dir/subdir → Allow when /trusted/dir is a trusted_dir."""
        _configure(monkeypatch, tmp_path, ["/trusted/dir"])
        apply_fixtures(tmp_db, SAFE_SHAPES)

        verdict = dispatch(
            tool_name="Bash",
            tool_input={"command": "rm -r /trusted/dir/subdir"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
        )
        assert verdict == Verdict.Allow, (
            f"rm -r inside trusted_dir must return Allow; got {verdict}"
        )


class TestRmOutsideTrustedDir:
    def test_rm_rf_outside_trusted_dir_returns_ask(
        self, monkeypatch, tmp_path, tmp_db
    ) -> None:
        """rm -rf /random/path with no matching trusted_dir → Ask (ask_flag_patterns)."""
        _configure(monkeypatch, tmp_path, ["/trusted/dir"])
        apply_fixtures(tmp_db, SAFE_SHAPES)

        verdict = dispatch(
            tool_name="Bash",
            tool_input={"command": "rm -rf /random/other/path"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
        )
        assert verdict == Verdict.Ask, (
            f"rm -rf outside trusted_dir must still return Ask; got {verdict}"
        )

    def test_rm_rf_no_trusted_dirs_configured_returns_ask(
        self, monkeypatch, tmp_path, tmp_db
    ) -> None:
        """rm -rf with no trusted_dirs configured → Ask (ask_flag_patterns active)."""
        _configure(monkeypatch, tmp_path, [])
        apply_fixtures(tmp_db, SAFE_SHAPES)

        verdict = dispatch(
            tool_name="Bash",
            tool_input={"command": "rm -rf /any/path"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
        )
        assert verdict == Verdict.Ask, (
            f"rm -rf with no trusted_dirs must return Ask; got {verdict}"
        )
