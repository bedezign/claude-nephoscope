from __future__ import annotations

import pytest

from nephoscope.learners.permission.canonicalize import pre_resolve_env_vars


class TestPreResolveEnvVars:
    def test_default_form_with_var_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MYDB", "/real/path.db")
        assert pre_resolve_env_vars("${MYDB:-/tmp/x.db}") == "/real/path.db"

    def test_default_form_with_var_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MYDB", raising=False)
        assert pre_resolve_env_vars("${MYDB:-/tmp/x.db}") == "/tmp/x.db"

    def test_default_form_with_var_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MYDB", "")
        assert pre_resolve_env_vars("${MYDB:-/tmp/x.db}") == "/tmp/x.db"

    def test_braced_var_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PROJ", "/workspace")
        assert pre_resolve_env_vars("${PROJ}") == "/workspace"

    def test_braced_var_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PROJ", raising=False)
        assert pre_resolve_env_vars("${PROJ}") == ""

    def test_bare_var_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", "/home/user")
        assert pre_resolve_env_vars("$HOME") == "/home/user"

    def test_bare_var_followed_by_non_word_char(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", "/home/user")
        result = pre_resolve_env_vars("$HOME/.config")
        assert result == "/home/user/.config"

    def test_escaped_dollar_not_expanded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VAR", "should-not-appear")
        result = pre_resolve_env_vars(r"\$VAR")
        assert "should-not-appear" not in result

    def test_command_substitution_not_expanded(self) -> None:
        result = pre_resolve_env_vars("$(date)")
        assert result == "$(date)"

    def test_backtick_not_expanded(self) -> None:
        result = pre_resolve_env_vars("`date`")
        assert result == "`date`"

    def test_mixed_default_db_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DB", raising=False)
        result = pre_resolve_env_vars('sqlite3 "${DB:-/tmp/x.db}" .tables')
        assert result == 'sqlite3 "/tmp/x.db" .tables'

    def test_mixed_braced_home_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", "/home/user")
        result = pre_resolve_env_vars('sqlite3 "${HOME}/.claude/obs.db" .tables')
        assert result == 'sqlite3 "/home/user/.claude/obs.db" .tables'

    def test_empty_string_input(self) -> None:
        assert pre_resolve_env_vars("") == ""

    def test_whitespace_only_input(self) -> None:
        assert pre_resolve_env_vars("   ") == "   "

    def test_pathological_nested_default_forms_complete_within_time_budget(
        self,
    ) -> None:
        """Deeply nested ${VAR:-fallback} forms must not cause regex backtracking explosion."""
        import time

        depth = 1000
        # Build: ${V:-${V:-${V:-...fallback...}}}
        inner = "fallback"
        for _ in range(depth):
            inner = "${V:-" + inner + "}"
        start = time.monotonic()
        result = pre_resolve_env_vars(inner)
        elapsed = time.monotonic() - start
        assert result is not None
        assert elapsed < 0.5, (
            f"pre_resolve_env_vars took {elapsed:.3f}s on pathological input"
        )

    def test_pathological_large_plain_string_completes_within_time_budget(self) -> None:
        """A 64KB string with no env vars must pass through without regex backtracking explosion."""
        import time

        large_input = "a" * 65536
        start = time.monotonic()
        result = pre_resolve_env_vars(large_input)
        elapsed = time.monotonic() - start
        assert result == large_input
        assert elapsed < 0.5, (
            f"pre_resolve_env_vars took {elapsed:.3f}s on 64KB plain input"
        )
