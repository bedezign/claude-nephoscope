"""Tests for optional permission fixture profiles (meta-profile format).

Loads each YAML via apply_profile against a fresh tmp_db and asserts
expected verb/subcommand/flags/decision triples are present in the DB.
Also covers doom-paths: empty file, missing decision, malformed flags.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

FIXTURES_META_DIR = Path(__file__).resolve().parents[2] / (
    "src/nephoscope/learners/permission/config/fixtures/meta-profiles"
)
DEV_TOOLS_PATH = FIXTURES_META_DIR / "dev-tools.yaml"
PYTHON_DEV_PATH = FIXTURES_META_DIR / "python-dev.yaml"
JAVASCRIPT_PATH = FIXTURES_META_DIR / "javascript.yaml"
DEVOPS_PATH = FIXTURES_META_DIR / "devops.yaml"
PROJECT_DEV_PATH = FIXTURES_META_DIR / "project-dev.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _approved_rows(conn: sqlite3.Connection) -> list[tuple]:
    """Return (verb, subcommand, flags, path_spec, decision) for all rows."""
    return conn.execute(
        """
        SELECT rs.verb, rs.subcommand, rs.flags, rs.path_spec, p.decision
          FROM rule_shapes rs
          JOIN permissions p ON p.rule_shape_id = rs.id
        """
    ).fetchall()


def _has_entry(
    rows: list[tuple],
    verb: str,
    *,
    subcommand: str | None = None,
    flags: str = "[]",
    path_spec: str | None = None,
    decision: str = "approved",
) -> bool:
    """Return True iff a matching row exists."""
    for r_verb, r_sub, r_flags, r_path, r_dec in rows:
        if (
            r_verb == verb
            and r_sub == subcommand
            and r_flags == flags
            and r_path == path_spec
            and r_dec == decision
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# dev-tools.yaml
# ---------------------------------------------------------------------------


class TestDevToolsFixture:
    """apply_profile on meta-profiles/dev-tools.yaml lands expected entries."""

    @pytest.fixture(autouse=True)
    def _load(self, tmp_db):
        from nephoscope.learners.permission.profiles import apply_profile

        apply_profile(tmp_db, DEV_TOOLS_PATH)
        tmp_db.commit()
        self._rows = _approved_rows(tmp_db)

    def test_make_dry_run_approved(self):
        assert _has_entry(
            self._rows,
            "make",
            flags='["--dry-run"]',
        ), "make --dry-run approved entry missing"

    def test_make_wildcard_trusted_dir_approved(self):
        assert _has_entry(
            self._rows,
            "make",
            flags="*",
            path_spec="$TRUSTED_DIR/**",
        ), "make * $TRUSTED_DIR/** approved entry missing"

    def test_curl_no_flags_approved(self):
        assert _has_entry(self._rows, "curl"), "curl [] approved entry missing"

    def test_wget_spider_approved(self):
        assert _has_entry(
            self._rows,
            "wget",
            flags='["--spider"]',
        ), "wget --spider approved entry missing"

    def test_touch_approved(self):
        assert _has_entry(
            self._rows,
            "touch",
            path_spec="$TRUSTED_DIR/**",
        ), "touch approved entry missing"

    def test_man_approved(self):
        assert _has_entry(self._rows, "man"), "man approved entry missing"

    def test_openssl_x509_approved(self):
        assert _has_entry(
            self._rows,
            "openssl",
            subcommand="x509",
        ), "openssl x509 approved entry missing"

    def test_openssl_s_client_approved(self):
        assert _has_entry(
            self._rows,
            "openssl",
            subcommand="s_client",
        ), "openssl s_client approved entry missing"


class TestDevToolsFixtureDoomPaths:
    """Doom-path coverage: malformed inputs must raise ValueError."""

    def test_empty_file_raises(self, tmp_db, tmp_path):
        """A meta-profile with no permissions section is valid but empty."""
        from nephoscope.learners.permission.profiles import apply_profile

        empty = tmp_path / "empty.yaml"
        empty.write_text("_meta:\n  id: test-empty\n  description: empty\n")
        perms_count, verb_types_count = apply_profile(tmp_db, empty)
        assert perms_count == 0
        assert verb_types_count == 0

    def test_missing_decision_raises(self, tmp_db, tmp_path):
        from nephoscope.learners.permission.profiles import apply_profile

        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "_meta:\n  id: bad\n  description: bad\npermissions:\n"
            "  - verb: curl\n    flags: []\n"
        )  # decision missing
        with pytest.raises(ValueError, match="missing"):
            apply_profile(tmp_db, bad)

    def test_malformed_flags_raises(self, tmp_db, tmp_path):
        from nephoscope.learners.permission.profiles import apply_profile

        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "_meta:\n  id: bad\n  description: bad\npermissions:\n"
            "  - verb: curl\n    flags: 123\n    decision: approved\n"
        )  # flags is an int, not list or "*"
        with pytest.raises((ValueError, TypeError)):
            apply_profile(tmp_db, bad)


# ---------------------------------------------------------------------------
# python-dev.yaml, javascript.yaml, devops.yaml
# ---------------------------------------------------------------------------


class TestPythonDevFixture:
    """apply_profile on meta-profiles/python-dev.yaml lands expected entries."""

    @pytest.fixture(autouse=True)
    def _load(self, tmp_db):
        from nephoscope.learners.permission.profiles import apply_profile

        apply_profile(tmp_db, PYTHON_DEV_PATH)
        tmp_db.commit()
        self._rows = _approved_rows(tmp_db)

    @pytest.mark.parametrize(
        "subcommand",
        ["run", "sync", "lock", "pip", "tool", "add", "remove"],
    )
    def test_uv_subcommands_approved(self, subcommand):
        assert _has_entry(
            self._rows,
            "uv",
            subcommand=subcommand,
        ), f"uv {subcommand} approved entry missing"

    @pytest.mark.parametrize(
        "verb",
        ["ruff", "pyright", "pytest", "mypy", "black", "isort", "coverage", "bandit"],
    )
    def test_top_level_tools_approved(self, verb):
        assert _has_entry(self._rows, verb), f"{verb} approved entry missing"

    def test_mutmut_run_approved(self):
        assert _has_entry(self._rows, "mutmut", subcommand="run"), "mutmut run missing"

    def test_mutmut_results_approved(self):
        assert _has_entry(self._rows, "mutmut", subcommand="results"), (
            "mutmut results missing"
        )

    @pytest.mark.parametrize("subcommand", ["show", "list", "freeze"])
    def test_pip_subcommands_approved(self, subcommand):
        assert _has_entry(self._rows, "pip", subcommand=subcommand), (
            f"pip {subcommand} missing"
        )


class TestJavaScriptFixture:
    """apply_profile on meta-profiles/javascript.yaml lands expected entries."""

    @pytest.fixture(autouse=True)
    def _load(self, tmp_db):
        from nephoscope.learners.permission.profiles import apply_profile

        apply_profile(tmp_db, JAVASCRIPT_PATH)
        tmp_db.commit()
        self._rows = _approved_rows(tmp_db)

    @pytest.mark.parametrize("verb", ["node", "npx", "deno"])
    def test_wildcard_flags_approved(self, verb):
        assert _has_entry(
            self._rows,
            verb,
            flags="*",
            path_spec="$TRUSTED_DIR/**",
        ), f"{verb} wildcard-flags entry missing"

    @pytest.mark.parametrize(
        ("verb", "subcommand"),
        [
            ("npm", "list"),
            ("npm", "info"),
            ("npm", "run"),
            ("npm", "test"),
            ("npm", "audit"),
            ("yarn", "list"),
            ("yarn", "info"),
            ("yarn", "run"),
            ("yarn", "test"),
            ("yarn", "audit"),
            ("pnpm", "list"),
            ("pnpm", "info"),
            ("pnpm", "run"),
            ("pnpm", "test"),
            ("pnpm", "audit"),
        ],
    )
    def test_package_manager_subcommands_approved(self, verb, subcommand):
        assert _has_entry(self._rows, verb, subcommand=subcommand), (
            f"{verb} {subcommand} missing"
        )

    @pytest.mark.parametrize("verb", ["tsc", "eslint", "vite", "vitest"])
    def test_top_level_tools_approved(self, verb):
        assert _has_entry(self._rows, verb), f"{verb} approved entry missing"


class TestDevOpsFixture:
    """apply_profile on meta-profiles/devops.yaml lands expected entries.

    Explicitly asserts ansible entries use flags, not subcommand, because
    --list and --list-hosts are flags on a top-level invocation — not
    positional subcommands.
    """

    @pytest.fixture(autouse=True)
    def _load(self, tmp_db):
        from nephoscope.learners.permission.profiles import apply_profile

        apply_profile(tmp_db, DEVOPS_PATH)
        tmp_db.commit()
        self._rows = _approved_rows(tmp_db)

    @pytest.mark.parametrize(
        "subcommand",
        ["get", "describe", "logs", "version", "config", "explain", "api-resources"],
    )
    def test_kubectl_subcommands_approved(self, subcommand):
        assert _has_entry(self._rows, "kubectl", subcommand=subcommand), (
            f"kubectl {subcommand} missing"
        )

    @pytest.mark.parametrize(
        "subcommand",
        ["list", "status", "history", "get", "template", "version", "show", "repo"],
    )
    def test_helm_subcommands_approved(self, subcommand):
        assert _has_entry(self._rows, "helm", subcommand=subcommand), (
            f"helm {subcommand} missing"
        )

    @pytest.mark.parametrize(
        "subcommand",
        ["ps", "images", "inspect", "logs", "version", "info", "top"],
    )
    def test_docker_subcommands_approved(self, subcommand):
        assert _has_entry(self._rows, "docker", subcommand=subcommand), (
            f"docker {subcommand} missing"
        )

    @pytest.mark.parametrize(
        "subcommand",
        ["validate", "plan", "fmt", "version", "output", "show"],
    )
    def test_terraform_subcommands_approved(self, subcommand):
        assert _has_entry(self._rows, "terraform", subcommand=subcommand), (
            f"terraform {subcommand} missing"
        )

    def test_ansible_inventory_uses_flags_not_subcommand(self):
        """ansible-inventory --list is a FLAG, not a subcommand."""
        assert _has_entry(
            self._rows,
            "ansible-inventory",
            subcommand=None,
            flags='["--list"]',
        ), "ansible-inventory --list (as flag) missing"
        # Must NOT be present as a subcommand
        bad_row = _has_entry(
            self._rows,
            "ansible-inventory",
            subcommand="--list",
        )
        assert not bad_row, (
            "ansible-inventory --list was incorrectly stored as subcommand"
        )

    def test_ansible_uses_flags_not_subcommand(self):
        """ansible --list-hosts is a FLAG, not a subcommand."""
        assert _has_entry(
            self._rows,
            "ansible",
            subcommand=None,
            flags='["--list-hosts"]',
        ), "ansible --list-hosts (as flag) missing"
        # Must NOT be present as a subcommand
        bad_row = _has_entry(
            self._rows,
            "ansible",
            subcommand="--list-hosts",
        )
        assert not bad_row, "ansible --list-hosts was incorrectly stored as subcommand"


# ---------------------------------------------------------------------------
# project-dev.yaml
# ---------------------------------------------------------------------------


class TestProjectDevFixture:
    """apply_profile on meta-profiles/project-dev.yaml lands expected entries."""

    @pytest.fixture(autouse=True)
    def _load(self, tmp_db):
        from nephoscope.learners.permission.profiles import apply_profile

        apply_profile(tmp_db, PROJECT_DEV_PATH)
        tmp_db.commit()
        self._rows = _approved_rows(tmp_db)

    @pytest.mark.parametrize(
        "verb", ["Edit", "Write", "Read", "MultiEdit", "NotebookEdit"]
    )
    def test_file_verbs_trusted_dir_approved(self, verb):
        assert _has_entry(
            self._rows,
            verb,
            flags="[]",
            path_spec="$TRUSTED_DIR/**",
        ), f"{verb} with $TRUSTED_DIR/** approved entry missing"

    @pytest.mark.parametrize("verb", ["python3", "python"])
    def test_python_wildcard_flags_approved(self, verb):
        assert _has_entry(
            self._rows,
            verb,
            flags="*",
            path_spec="$TRUSTED_DIR/**",
        ), f"{verb} with wildcard flags and $TRUSTED_DIR/** approved entry missing"

    def test_bash_trusted_dir_approved(self):
        assert _has_entry(
            self._rows,
            "bash",
            flags="[]",
            path_spec="$TRUSTED_DIR/**",
        ), "bash with $TRUSTED_DIR/** approved entry missing"
