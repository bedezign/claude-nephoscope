"""Integration tests: env-var pre-resolution → bashlex parse → canonicalization → DB match.

Exercises the full path:
  pre_resolve_env_vars() → parse_command() → dispatch() / to_pattern_form()

All tests use isolated temp databases via ``tmp_db`` from conftest.py.
No writes reach ~/.claude/settings.json or the live observations DB.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

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
# Pure-function tests for pre_resolve_env_vars
# ---------------------------------------------------------------------------


class TestPreResolveEnvVars:
    """Unit-level tests embedded in the integration suite because they exercise
    the same module under test; they are fast and need no DB fixture."""

    def test_default_form_uses_default_when_var_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """${VAR:-default} resolves to the default string when VAR is unset."""
        from nephoscope.learners.permission.canonicalize import pre_resolve_env_vars

        monkeypatch.delenv("DB", raising=False)
        result = pre_resolve_env_vars('sqlite3 "${DB:-/tmp/x.db}" .tables')
        assert "/tmp/x.db" in result
        assert "${DB" not in result

    def test_default_form_uses_var_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """${VAR:-default} resolves to the env value when VAR is set."""
        from nephoscope.learners.permission.canonicalize import pre_resolve_env_vars

        monkeypatch.setenv("DB", "/tmp/real.db")
        result = pre_resolve_env_vars('sqlite3 "${DB:-/tmp/fallback.db}" .tables')
        assert "/tmp/real.db" in result
        assert "/tmp/fallback.db" not in result
        assert "${DB" not in result

    def test_brace_form_resolves_set_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """${VAR} resolves to env value when set."""
        from nephoscope.learners.permission.canonicalize import pre_resolve_env_vars

        monkeypatch.setenv("DB", "/tmp/real.db")
        result = pre_resolve_env_vars('sqlite3 "${DB}" .tables')
        assert "/tmp/real.db" in result
        assert "${DB}" not in result

    def test_home_in_path_resolved(self) -> None:
        """${HOME} in a path is replaced with the actual home directory value."""
        from nephoscope.learners.permission.canonicalize import pre_resolve_env_vars

        home = os.environ.get("HOME", "")
        assert home, "HOME must be set in the test environment"

        result = pre_resolve_env_vars('cat "${HOME}/.claude/observations.db"')
        assert "${HOME}" not in result
        assert home in result

    def test_command_substitution_not_expanded(self) -> None:
        """$(cmd) forms are left untouched — no shell execution occurs."""
        from nephoscope.learners.permission.canonicalize import pre_resolve_env_vars

        raw = "echo $(date)"
        assert pre_resolve_env_vars(raw) == raw

    def test_backtick_substitution_not_expanded(self) -> None:
        """Backtick command substitutions are left untouched."""
        from nephoscope.learners.permission.canonicalize import pre_resolve_env_vars

        raw = "echo `date`"
        assert pre_resolve_env_vars(raw) == raw

    def test_escaped_dollar_not_expanded(self) -> None:
        r"""\\$VAR is not expanded — the backslash is an escape marker."""
        from nephoscope.learners.permission.canonicalize import pre_resolve_env_vars

        # In Python source '\\$HOME' is the two-char sequence backslash + $HOME.
        # pre_resolve_env_vars uses a negative lookbehind (?<!\\) on the $ patterns
        # so a preceding backslash suppresses the substitution.
        raw = "echo \\$HOME"
        result = pre_resolve_env_vars(raw)
        assert result == raw, f"expected unchanged {raw!r}, got {result!r}"
        home = os.environ.get("HOME", "")
        assert result != home, "escaped dollar must not expand to the HOME value"

    def test_positional_parameter_not_expanded(self) -> None:
        """$1, $?, $$ (special/positional params) are not touched by pass 3."""
        from nephoscope.learners.permission.canonicalize import pre_resolve_env_vars

        for raw in ("echo $1", "echo $?", "echo $$"):
            assert pre_resolve_env_vars(raw) == raw, (
                f"special parameter in {raw!r} must not be expanded"
            )


# ---------------------------------------------------------------------------
# parse_command integration: env-var → bashlex → CanonicalLeaf
# ---------------------------------------------------------------------------


class TestParseCommandEnvVarResolution:
    """Verify that env-var resolution produces canonically-correct leaves."""

    def test_sqlite3_unset_var_default_becomes_positional_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sqlite3 "${DB:-/tmp/x.db}" .tables — unset var: resolved path lands in positional_paths.

        sqlite3 is in CONTENT_VERBS so its first positional is treated as
        content (a DB file path), not a subcommand.  subcommand is None and the
        resolved path ends up in positional_paths for scope matching.
        """
        from nephoscope.learners.permission.canonicalize import parse_command

        monkeypatch.delenv("DB", raising=False)
        leaves = parse_command('sqlite3 "${DB:-/tmp/x.db}" .tables')

        assert len(leaves) == 1, f"expected 1 leaf, got {leaves}"
        leaf = leaves[0]
        assert leaf.verb == "sqlite3"
        # sqlite3 is a CONTENT_VERB — first positional is content, not a subcommand.
        assert leaf.subcommand is None, (
            f"expected subcommand=None (sqlite3 is CONTENT_VERB), got {leaf.subcommand!r}"
        )
        assert "/tmp/x.db" in leaf.positional_paths, (
            f"resolved path not found in positional_paths: {leaf.positional_paths}"
        )
        # The raw_leaf must not contain any unresolved ${...} references.
        assert "${" not in leaf.raw_leaf, (
            f"raw_leaf still contains unresolved brace: {leaf.raw_leaf!r}"
        )

    def test_sqlite3_set_var_resolves_to_positional_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sqlite3 "${DB}" .tables — set var: env value lands in positional_paths.

        sqlite3 is in CONTENT_VERBS so its first positional is content (the DB
        file path), not a subcommand.  subcommand is None and the resolved env
        value ends up in positional_paths for scope matching.
        """
        from nephoscope.learners.permission.canonicalize import parse_command

        monkeypatch.setenv("DB", "/tmp/real.db")
        leaves = parse_command('sqlite3 "${DB}" .tables')

        assert len(leaves) == 1, f"expected 1 leaf, got {leaves}"
        leaf = leaves[0]
        assert leaf.verb == "sqlite3"
        assert leaf.subcommand is None, (
            f"expected subcommand=None (sqlite3 is CONTENT_VERB), got {leaf.subcommand!r}"
        )
        assert "/tmp/real.db" in leaf.positional_paths, (
            f"resolved path not found in positional_paths: {leaf.positional_paths}"
        )
        assert "${DB}" not in leaf.raw_leaf

    def test_cat_home_path_positional_contains_resolved_home(self) -> None:
        """cat "${HOME}/.claude/observations.db" — HOME resolved, path in positionals.

        cat is in CONTENT_VERBS so subcommand=None and the resolved path lands
        in positional_paths.
        """
        from nephoscope.learners.permission.canonicalize import parse_command

        home = os.environ.get("HOME", "")
        assert home, "HOME must be set in the test environment"

        leaves = parse_command('cat "${HOME}/.claude/observations.db"')

        assert len(leaves) == 1, f"expected 1 leaf, got {leaves}"
        leaf = leaves[0]
        assert leaf.verb == "cat"
        assert leaf.subcommand is None
        assert any(home in p for p in leaf.positional_paths), (
            f"resolved HOME path not found in positional_paths: {leaf.positional_paths}"
        )
        assert not any("${HOME}" in p for p in leaf.positional_paths), (
            f"unresolved ${{HOME}} still present in positionals: {leaf.positional_paths}"
        )

    def test_command_substitution_does_not_expand_in_parse(self) -> None:
        """echo $(date) — command substitution not executed; parse returns without expansion.

        The key invariant: pre_resolve_env_vars must not have touched the $(date)
        part, verified by checking the unchanged-string property.
        """
        from nephoscope.learners.permission.canonicalize import (
            parse_command,
            pre_resolve_env_vars,
        )

        raw = "echo $(date)"
        # pre_resolve_env_vars must leave the string identical.
        assert pre_resolve_env_vars(raw) == raw, (
            "pre_resolve_env_vars must not alter $(cmd)"
        )

        # parse_command must either succeed or return [] — but must not raise.
        result = parse_command(raw)
        assert isinstance(result, list), "parse_command must return a list"

    def test_backtick_substitution_does_not_expand_in_pre_resolve(self) -> None:
        """echo `date` — backtick form is left untouched by pre_resolve_env_vars."""
        from nephoscope.learners.permission.canonicalize import pre_resolve_env_vars

        raw = "echo `date`"
        assert pre_resolve_env_vars(raw) == raw, "backtick form must be unchanged"


# ---------------------------------------------------------------------------
# Full dispatch pipeline: seeded DB → dispatch → Verdict
# ---------------------------------------------------------------------------


class TestDispatchWithEnvVarResolution:
    """Verify the full pipeline: seed DB → dispatch Bash tool call → Verdict."""

    def test_sqlite3_plain_command_returns_allow(self, tmp_db) -> None:  # type: ignore[no-untyped-def]
        """sqlite3 /tmp/x.db .tables dispatches to Allow.

        sqlite3 is in CONTENT_VERBS so its first positional is treated as a DB
        file path (content), not a subcommand.  The canonical leaf has
        subcommand=None, which matches the safe_shapes.yaml seed entry
        (verb=sqlite3, subcommand=None, flags=[]).  The result is Allow.
        """
        from nephoscope.learners.permission.match import Verdict, dispatch
        from nephoscope.learners.permission.seed import apply_fixtures

        conn = tmp_db
        apply_fixtures(conn, SAFE_SHAPES)

        verdict, _ = dispatch(
            tool_name="Bash",
            tool_input={"command": "sqlite3 /tmp/x.db .tables"},
            conn=conn,
            session_id=None,
            project_id=None,
        )
        assert verdict == Verdict.Allow, (
            f"sqlite3 with explicit path arg must return Allow; got {verdict}"
        )

    def test_dispatch_env_var_command_matches_plain_command(
        self,
        tmp_db,
        monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
    ) -> None:
        """sqlite3 "${DB:-/tmp/x.db}" with unset DB resolves identically to the plain form.

        After pre_resolve_env_vars the command becomes 'sqlite3 "/tmp/x.db" .tables',
        which canonicalizes identically to 'sqlite3 /tmp/x.db .tables'.  Both
        dispatch calls must return the same Verdict, proving that env-var resolution
        does not change the matching outcome.

        sqlite3 is in CONTENT_VERBS so first positional is content (subcommand=None),
        matching the safe_shapes seed.  Both forms return Allow.
        """
        from nephoscope.learners.permission.match import Verdict, dispatch
        from nephoscope.learners.permission.seed import apply_fixtures

        monkeypatch.delenv("DB", raising=False)
        conn = tmp_db
        apply_fixtures(conn, SAFE_SHAPES)

        verdict_plain, _ = dispatch(
            tool_name="Bash",
            tool_input={"command": "sqlite3 /tmp/x.db .tables"},
            conn=conn,
            session_id=None,
            project_id=None,
        )
        verdict_var, _ = dispatch(
            tool_name="Bash",
            tool_input={"command": 'sqlite3 "${DB:-/tmp/x.db}" .tables'},
            conn=conn,
            session_id=None,
            project_id=None,
        )
        assert verdict_var == verdict_plain, (
            f"env-var resolved command must dispatch identically to the plain form; "
            f"plain={verdict_plain}, var={verdict_var}"
        )
        # Both return Allow: sqlite3 is in CONTENT_VERBS, seed shape matches (subcommand=None).
        assert verdict_plain == Verdict.Allow

    def test_dispatch_with_fully_approved_verb_returns_allow(self, tmp_db) -> None:  # type: ignore[no-untyped-def]
        """A content verb with an approved seed rule returns Verdict.Allow.

        'cat' is in CONTENT_VERBS (subcommand=None) and approved in safe_shapes.yaml
        with flags=[] (empty). 'cat /tmp/x' produces a leaf matching that shape.
        """
        from nephoscope.learners.permission.match import Verdict, dispatch
        from nephoscope.learners.permission.seed import apply_fixtures

        conn = tmp_db
        apply_fixtures(conn, SAFE_SHAPES)

        verdict, _ = dispatch(
            tool_name="Bash",
            tool_input={"command": "cat /tmp/x"},
            conn=conn,
            session_id=None,
            project_id=None,
        )
        assert verdict == Verdict.Allow, (
            f"cat /tmp/x must return Allow with safe_shapes seeded; got {verdict}"
        )

    def test_dispatch_env_var_home_path_cat_returns_allow(self, tmp_db) -> None:  # type: ignore[no-untyped-def]
        """cat "${HOME}/.claude/observations.db" — HOME resolved, dispatch returns Allow.

        After resolution the command becomes cat <abs_path>.  cat is a CONTENT_VERB
        with an approved seed rule, so the dispatch should return Allow.
        """
        from nephoscope.learners.permission.match import Verdict, dispatch
        from nephoscope.learners.permission.seed import apply_fixtures

        home = os.environ.get("HOME", "")
        assert home, "HOME must be set in the test environment"

        conn = tmp_db
        apply_fixtures(conn, SAFE_SHAPES)

        verdict, _ = dispatch(
            tool_name="Bash",
            tool_input={"command": 'cat "${HOME}/.claude/observations.db"'},
            conn=conn,
            session_id=None,
            project_id=None,
        )
        assert verdict == Verdict.Allow, (
            f"cat with resolved HOME path must return Allow; got {verdict}"
        )

    def test_dispatch_empty_command_returns_no_opinion(self, tmp_db) -> None:  # type: ignore[no-untyped-def]
        """Empty command input gives NoOpinion — not a crash."""
        from nephoscope.learners.permission.match import Verdict, dispatch

        verdict, _ = dispatch(
            tool_name="Bash",
            tool_input={"command": ""},
            conn=tmp_db,
            session_id=None,
            project_id=None,
        )
        assert verdict == Verdict.NoOpinion

    def test_dispatch_command_with_unresolvable_var_returns_allow(
        self,
        tmp_db,
        monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
    ) -> None:
        """sqlite3 "${DB}" with DB unset — empty arg dropped, leaf matches seed shape.

        When DB is unset, ${DB} resolves to ''.  bashlex tokenizes 'sqlite3 "" .tables'
        and drops the empty-string arg, producing leaf with subcommand=None — which
        matches the safe_shapes seed entry (verb=sqlite3, subcommand=None, flags=[]).
        The dispatch therefore returns Allow.

        This is the one concrete sqlite3 invocation that does reach Allow with the
        current seed — a useful contrast to the explicit-path case above.
        """
        from nephoscope.learners.permission.match import Verdict, dispatch
        from nephoscope.learners.permission.seed import apply_fixtures

        monkeypatch.delenv("DB", raising=False)
        conn = tmp_db
        apply_fixtures(conn, SAFE_SHAPES)

        verdict, _ = dispatch(
            tool_name="Bash",
            tool_input={"command": 'sqlite3 "${DB}" .tables'},
            conn=conn,
            session_id=None,
            project_id=None,
        )
        assert verdict == Verdict.Allow, (
            f"sqlite3 with empty-string arg (DB unset) should return Allow; got {verdict}"
        )
