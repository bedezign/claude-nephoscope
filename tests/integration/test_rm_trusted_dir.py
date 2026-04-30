"""Integration tests: rm with $TRUSTED_DIR fixture in the match pipeline.

Exercises:
  - rm -rf under a configured trusted_dir → Allow (seeded safe_shapes rule fires)
  - rm -rf outside all trusted_dirs → Ask (ask_flag_patterns still active)
  - rm -f outside trusted_dirs → Ask (deny.yaml ask_flag_patterns still active)
  - Read on a path inside trusted_dir but matching a Deny rule → Deny (deny wins over Allow)
"""

from __future__ import annotations

import sqlite3
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from nephoscope.config import get_config
from nephoscope.learners.permission.match import Verdict, dispatch
from nephoscope.learners.permission.match.file import match as file_match
from nephoscope.learners.permission.seed import apply_fixtures
from nephoscope.lib.db import insert_permission, upsert_rule_shape

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

        verdict, _ = dispatch(
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

        verdict, _ = dispatch(
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

        verdict, _ = dispatch(
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

        verdict, _ = dispatch(
            tool_name="Bash",
            tool_input={"command": "rm -rf /any/path"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
        )
        assert verdict == Verdict.Ask, (
            f"rm -rf with no trusted_dirs must return Ask; got {verdict}"
        )


class TestDenyInsideTrustedDir:
    """Deny rules for sub-paths inside a trusted dir must fire even when a
    broader Allow rule covers the same trusted-dir subtree."""

    def _seed_rules(self, conn, ts: str = "2024-01-01T00:00:00Z") -> None:
        """Seed an Allow rule for $TRUSTED_DIR/** and a Deny rule for $TRUSTED_DIR/.env."""
        allow_id = upsert_rule_shape(conn, "Read", None, "[]", "$TRUSTED_DIR/**", ts)
        insert_permission(conn, allow_id, None, None, "approved", "seed", ts)

        deny_id = upsert_rule_shape(conn, "Read", None, "[]", "$TRUSTED_DIR/.env", ts)
        insert_permission(conn, deny_id, None, None, "rejected", "seed", ts)
        conn.commit()

    def test_regular_file_inside_trusted_dir_returns_allow(
        self, monkeypatch, tmp_path, tmp_db
    ) -> None:
        """Read on a regular file inside trusted_dir → Allow (broad Allow rule fires)."""
        _configure(monkeypatch, tmp_path, ["/tmp/test-trusted"])
        self._seed_rules(tmp_db)

        verdict, _ = file_match(
            tool_name="Read",
            tool_input={"file_path": "/tmp/test-trusted/regular_file.py"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
            ctx={},
            trusted_dirs=["/tmp/test-trusted"],
        )
        assert verdict == Verdict.Allow, (
            f"Read on regular file inside trusted_dir must return Allow; got {verdict}"
        )

    def test_env_file_inside_trusted_dir_returns_deny(
        self, monkeypatch, tmp_path, tmp_db
    ) -> None:
        """Read on .env inside trusted_dir → Deny (specific Deny rule overrides Allow)."""
        _configure(monkeypatch, tmp_path, ["/tmp/test-trusted"])
        self._seed_rules(tmp_db)

        verdict, _ = file_match(
            tool_name="Read",
            tool_input={"file_path": "/tmp/test-trusted/.env"},
            conn=tmp_db,
            session_id=None,
            project_id=None,
            ctx={},
            trusted_dirs=["/tmp/test-trusted"],
        )
        assert verdict == Verdict.Deny, (
            f"Read on .env inside trusted_dir must return Deny; got {verdict}"
        )


_EXPECTED_FILE_VERBS = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit"}


class TestAutoSeedOnTrustedDirAdd:
    """_append_trusted_dirs seeds a per-verb $TRUSTED_DIR/** Allow rule for each file tool."""

    @pytest.fixture()
    def db_with_mirror(self, tmp_path, monkeypatch) -> Iterator[sqlite3.Connection]:
        """Isolated DB with global_mirror singleton seeded (required by sync_affected)."""
        import sqlite3 as _sqlite3

        PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
        schema_sql = (
            PROJECT_ROOT / "src" / "nephoscope" / "lib" / "schema.sql"
        ).read_text()
        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        fake_settings = tmp_path / "settings.json"
        conn = _sqlite3.connect(str(db_path), isolation_level=None)
        conn.executescript(schema_sql)
        conn.execute(
            "INSERT OR IGNORE INTO global_mirror"
            " (id, settings_json_path, settings_json_sha256, settings_json_last_synced)"
            " VALUES (1, ?, NULL, NULL);",
            (str(fake_settings),),
        )
        conn.execute(
            "INSERT OR IGNORE INTO permission_modes (name)"
            " VALUES ('default'),('acceptEdits'),('bypassPermissions'),('plan'),('auto');"
        )
        conn.execute(
            "INSERT OR IGNORE INTO call_statuses (name)"
            " VALUES ('pending'),('ok'),('err'),('denied'),('orphan');"
        )
        yield conn
        conn.close()

    def test_rule_shape_and_permission_seeded_after_append(
        self, monkeypatch, tmp_path, db_with_mirror
    ) -> None:
        """_append_trusted_dirs seeds one rule_shapes row per file-tool verb.

        Asserts:
        - One rule_shapes row per verb in FILE_VERBS with path_spec='$TRUSTED_DIR/**'.
        - Each has an approved global-tier permission with source='seed'.
        - settings.json contains '$TRUSTED_DIR/**' entries for the seeded verbs.
        """
        import json

        from nephoscope.cli.init_cmd import _append_trusted_dirs

        cfg_path = tmp_path / "nephoscope-config.toml"
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg_path))
        get_config.cache_clear()

        trusted_dir = str(tmp_path / "myproject")
        _append_trusted_dirs([trusted_dir])

        conn = db_with_mirror
        for verb in _EXPECTED_FILE_VERBS:
            shape = conn.execute(
                "SELECT id FROM rule_shapes WHERE verb = ? AND path_spec = '$TRUSTED_DIR/**';",
                (verb,),
            ).fetchone()
            assert shape is not None, (
                f"rule_shapes row for verb={verb!r} path_spec='$TRUSTED_DIR/**' must exist"
            )
            shape_id = shape[0]

            perm = conn.execute(
                "SELECT decision, source FROM permissions"
                " WHERE rule_shape_id = ? AND session_id IS NULL AND project_id IS NULL;",
                (shape_id,),
            ).fetchone()
            assert perm is not None, f"permissions row must exist for verb={verb!r}"
            assert perm[0] == "approved", (
                f"decision must be 'approved' for verb={verb!r}"
            )
            assert perm[1] == "seed", f"source must be 'seed' for verb={verb!r}"

        # settings.json must contain resolved path entries for the trusted dir.
        # _generate_workspace_entries writes Write/Edit/Read entries using the
        # resolved absolute path (not the $TRUSTED_DIR token).
        fake_settings = tmp_path / "settings.json"
        settings = json.loads(fake_settings.read_text())
        allow_entries = settings.get("permissions", {}).get("allow", [])
        allow_text = json.dumps(allow_entries)
        assert trusted_dir in allow_text, (
            f"settings.json allow entries must reference {trusted_dir!r}; got: {allow_entries!r}"
        )

    def test_append_twice_produces_single_permission_row_per_verb(
        self, monkeypatch, tmp_path, db_with_mirror
    ) -> None:
        """Calling _append_trusted_dirs twice does not duplicate permission rows."""
        from nephoscope.cli.init_cmd import _append_trusted_dirs

        cfg_path = tmp_path / "nephoscope-config.toml"
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(cfg_path))
        get_config.cache_clear()

        _append_trusted_dirs([str(tmp_path)])
        _append_trusted_dirs([str(tmp_path / "second")])

        conn = db_with_mirror
        for verb in _EXPECTED_FILE_VERBS:
            shape = conn.execute(
                "SELECT id FROM rule_shapes WHERE verb = ? AND path_spec = '$TRUSTED_DIR/**';",
                (verb,),
            ).fetchone()
            assert shape is not None, (
                f"rule_shapes row for verb={verb!r} must exist after second append"
            )
            shape_id = shape[0]

            count = conn.execute(
                "SELECT COUNT(*) FROM permissions"
                " WHERE rule_shape_id = ? AND session_id IS NULL AND project_id IS NULL;",
                (shape_id,),
            ).fetchone()[0]
            assert count == 1, (
                f"expected exactly 1 permission row for verb={verb!r}, got {count}"
            )
