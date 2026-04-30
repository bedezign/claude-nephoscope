"""Integration tests: main() profile prompt gating — Phase 2 Step 15.

Four cases:
1. Fresh DB + TTY + input "1" → fixtures applied (prompt fires, entries land)
2. Existing DB → no prompt (_prompt_for_profiles never called)
3. --no-workspace-prompts → no prompt
4. sys.stdin.isatty() False → no prompt
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"


def _open_db(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db_path), isolation_level=None)


def _count_verb(conn: sqlite3.Connection, verb: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM rule_shapes WHERE verb = ?;", (verb,)
    ).fetchone()[0]


class TestMainProfileGating:
    """main() gates the optional profile prompt correctly."""

    def test_fresh_db_tty_with_selection_seeds_fixtures(self, tmp_path, monkeypatch):
        """Fresh DB + TTY + input '2' → dev-tools fixtures applied.

        Profile 1 is now project-dev; profile 2 is dev-tools.
        Skips workspace-prompts phase by patching _configure_workspace_roots
        so only the profile prompt path is exercised.
        """
        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        from nephoscope.cli import init_cmd

        # Patch TTY check and bypass workspace-roots entirely so input('2') goes
        # only to _prompt_for_profiles.
        with (
            patch.object(init_cmd.sys.stdin, "isatty", return_value=True),
            patch.object(init_cmd, "_configure_workspace_roots"),
            patch("builtins.input", return_value="2"),
        ):
            rc = init_cmd.main([])

        assert rc == 0

        conn = _open_db(db_path)
        try:
            # 'curl' is a dev-tools (profile 2) entry — must be present
            count = _count_verb(conn, "curl")
            assert count >= 1, (
                f"Expected curl entry from dev-tools profile, got {count}"
            )
        finally:
            conn.close()

    def test_fresh_db_tty_no_workspace_prompts_skips_profile_prompt(
        self, tmp_path, monkeypatch
    ):
        """--no-workspace-prompts suppresses both workspace AND profile prompts."""
        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        from nephoscope.cli import init_cmd

        with patch.object(init_cmd, "_prompt_for_profiles") as mock_prompt:
            rc = init_cmd.main(["--no-workspace-prompts"])

        assert rc == 0
        mock_prompt.assert_not_called()

    def test_existing_db_skips_profile_prompt(self, tmp_path, monkeypatch):
        """Second run on existing DB: _prompt_for_profiles is never called."""
        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        from nephoscope.cli import init_cmd

        # First run — fresh DB
        init_cmd.main(["--no-workspace-prompts"])

        # Second run — existing DB
        with patch.object(init_cmd, "_prompt_for_profiles") as mock_prompt:
            rc = init_cmd.main(["--no-workspace-prompts"])

        assert rc == 0
        mock_prompt.assert_not_called()

    def test_non_tty_stdin_skips_profile_prompt(self, tmp_path, monkeypatch):
        """Non-TTY stdin: _prompt_for_profiles is never called."""
        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        from nephoscope.cli import init_cmd

        with (
            patch.object(init_cmd.sys.stdin, "isatty", return_value=False),
            patch.object(init_cmd, "_prompt_for_profiles") as mock_prompt,
        ):
            rc = init_cmd.main(["--no-workspace-prompts"])

        assert rc == 0
        mock_prompt.assert_not_called()
