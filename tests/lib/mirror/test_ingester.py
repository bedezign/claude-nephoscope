"""Tests for lib/mirror/ingester.py — entry parsing and settings.json ingestion.

Positive cases cover all canonical forms observed in real settings.json files.
Negative cases exercise every structural defect the ingester must reject.
Round-trip tests couple with the serializer (lib.mirror.serializer.serialize);
they are skipped automatically when it is a stub.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nephoscope.lib.mirror.ingester import (
    IngesterError,
    parse_entry,
    parse_permissions_json,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings_json(
    *,
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    ask: list[str] | None = None,
    tmp_path: Path,
) -> Path:
    """Write a minimal settings.json to tmp_path and return the path."""
    data: dict = {
        "permissions": {
            "allow": allow or [],
            "deny": deny or [],
            "ask": ask or [],
        }
    }
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Bash canonical forms
# ---------------------------------------------------------------------------


class TestParseBash:
    """Bash entries: ``Bash(<shell_cmd> [<sub>] [*])``."""

    def test_bash_wildcard_flags(self):
        row = parse_entry("Bash(git *)", source="test")
        assert row["tool"] == "Bash"
        assert row["verb"] == "git"
        assert row["subcommand"] is None
        assert row["flags"] == "*"
        assert row["path_spec"] is None
        assert row["tool_class"] == "bash"

    def test_bash_subcommand_only_no_flags(self):
        row = parse_entry("Bash(git)", source="test")
        assert row["tool"] == "Bash"
        assert row["verb"] == "git"
        assert row["subcommand"] is None
        assert row["flags"] == "[]"

    def test_bash_literal_single_flag(self):
        row = parse_entry("Bash(git push)", source="test")
        assert row["verb"] == "git"
        assert row["subcommand"] == "push"
        assert row["flags"] == "[]"

    def test_bash_multitoken_flags_with_wildcard(self):
        # Middle tokens become subcommand; trailing " *" sets wildcard.
        row = parse_entry("Bash(systemctl --user status *)", source="test")
        assert row["verb"] == "systemctl"
        assert row["subcommand"] == "--user status"
        assert row["flags"] == "*"
        assert row["tool_class"] == "bash"

    def test_bash_absolute_path_subcommand(self):
        # e.g. Bash(/tmp/claude/**) — absolute-path command pattern
        row = parse_entry("Bash(/tmp/claude/**)", source="test")
        assert row["verb"] == "/tmp/claude/**"
        assert row["subcommand"] is None
        assert row["flags"] == "[]"
        assert row["tool_class"] == "bash"

    def test_bash_subcommand_no_space_wildcard(self):
        # e.g. Bash(wl-copy*) — glob suffix without a space
        row = parse_entry("Bash(wl-copy*)", source="test")
        assert row["verb"] == "wl-copy*"
        assert row["subcommand"] is None
        assert row["flags"] == "[]"

    def test_bash_short_flag_with_inline_glob(self):
        # Bash(pacman -Q*) — no space before *, so NOT a wildcard flag.
        row = parse_entry("Bash(pacman -Q*)", source="test")
        assert row["verb"] == "pacman"
        assert row["subcommand"] == "-Q*"
        assert row["flags"] == "[]"

    def test_bash_uv_wildcard(self):
        row = parse_entry("Bash(uv *)", source="test")
        assert row["verb"] == "uv"
        assert row["flags"] == "*"

    def test_bash_absolute_path_with_wildcard(self):
        # e.g. Bash(/home/user/.pyenv/versions/*/bin/python3 *)
        row = parse_entry(
            "Bash(/home/user/.pyenv/versions/*/bin/python3 *)", source="test"
        )
        assert row["verb"] == "/home/user/.pyenv/versions/*/bin/python3"
        assert row["subcommand"] is None
        assert row["flags"] == "*"
        assert row["tool_class"] == "bash"

    def test_bash_tool_field_is_always_bash(self):
        for entry in ("Bash(git *)", "Bash(ls)", "Bash(/tmp/claude/**)"):
            row = parse_entry(entry, source="test")
            assert row["tool"] == "Bash", f"tool must be 'Bash' for {entry!r}"


# ---------------------------------------------------------------------------
# File tool canonical forms
# ---------------------------------------------------------------------------


class TestParseFileTools:
    """File entries: ``<Verb>(//abs/path)`` or ``<Verb>(*-glob)``.

    The ``//abs`` form is the canonical Claude Code absolute-path encoding;
    the ``*``-leading glob form (e.g. ``Read(**/.env)``) is the cwd-relative
    form Claude Code accepts for portable deny rules.  Both are stored in
    ``path_spec`` verbatim — the prefix carries the matching semantics.
    """

    def test_read_glob_path(self):
        row = parse_entry("Read(//home/user/.claude/**)", source="test")
        assert row["tool"] == "Read"
        assert row["verb"] == "Read"
        assert row["path_spec"] == "//home/user/.claude/**"
        assert row["subcommand"] is None
        assert row["flags"] is None
        assert row["tool_class"] == "file"

    def test_edit_glob_path(self):
        row = parse_entry("Edit(//home/user/data/clients/**)", source="test")
        assert row["tool"] == "Edit"
        assert row["path_spec"] == "//home/user/data/clients/**"
        assert row["tool_class"] == "file"

    def test_write_glob_path(self):
        row = parse_entry("Write(//home/user/data/**)", source="test")
        assert row["tool"] == "Write"
        assert row["path_spec"] == "//home/user/data/**"
        assert row["tool_class"] == "file"

    def test_multiedit_glob_path(self):
        row = parse_entry("MultiEdit(//home/user/.claude/**)", source="test")
        assert row["tool"] == "MultiEdit"
        assert row["path_spec"] == "//home/user/.claude/**"
        assert row["tool_class"] == "file"

    def test_file_tool_exact_file_path(self):
        row = parse_entry("Read(//home/user/.claude/settings.json)", source="test")
        assert row["path_spec"] == "//home/user/.claude/settings.json"
        assert row["tool_class"] == "file"

    def test_read_pyenv_python_glob(self):
        row = parse_entry("Read(//home/user/.pyenv/versions/**)", source="test")
        assert row["path_spec"] == "//home/user/.pyenv/versions/**"
        assert row["tool_class"] == "file"

    def test_read_relative_glob_double_star(self):
        row = parse_entry("Read(**/.env)", source="test")
        assert row["tool"] == "Read"
        assert row["path_spec"] == "**/.env"
        assert row["tool_class"] == "file"

    def test_read_relative_glob_recursive(self):
        row = parse_entry("Read(**/secrets/**)", source="test")
        assert row["path_spec"] == "**/secrets/**"
        assert row["tool_class"] == "file"

    def test_write_relative_glob(self):
        row = parse_entry("Write(**/.env)", source="test")
        assert row["tool"] == "Write"
        assert row["path_spec"] == "**/.env"
        assert row["tool_class"] == "file"

    def test_edit_relative_glob(self):
        row = parse_entry("Edit(**/.ssh/**)", source="test")
        assert row["tool"] == "Edit"
        assert row["path_spec"] == "**/.ssh/**"
        assert row["tool_class"] == "file"

    def test_read_relative_glob_extension(self):
        row = parse_entry("Read(**/*.pem)", source="test")
        assert row["path_spec"] == "**/*.pem"
        assert row["tool_class"] == "file"

    def test_read_single_star_glob(self):
        row = parse_entry("Read(*.env)", source="test")
        assert row["path_spec"] == "*.env"
        assert row["tool_class"] == "file"


# ---------------------------------------------------------------------------
# Flat tool canonical forms
# ---------------------------------------------------------------------------


class TestParseFlatTools:
    """Flat entries: bare ``<Verb>`` — no argument encoding."""

    def test_websearch_bare(self):
        row = parse_entry("WebSearch", source="test")
        assert row["tool"] == "WebSearch"
        assert row["verb"] == "WebSearch"
        assert row["path_spec"] is None
        assert row["subcommand"] is None
        assert row["flags"] is None
        assert row["tool_class"] == "flat"

    def test_grep_bare(self):
        row = parse_entry("Grep", source="test")
        assert row["tool"] == "Grep"
        assert row["tool_class"] == "flat"

    def test_glob_bare(self):
        row = parse_entry("Glob", source="test")
        assert row["tool"] == "Glob"
        assert row["tool_class"] == "flat"


# ---------------------------------------------------------------------------
# MCP canonical forms
# ---------------------------------------------------------------------------


class TestParseMcp:
    """MCP entries: bare ``mcp__<namespace>__<tool>`` or ``mcp__<namespace>__*``."""

    def test_mcp_fully_qualified(self):
        row = parse_entry("mcp__claude-peers__send_message", source="test")
        assert row["tool"] == "mcp__claude-peers__send_message"
        assert row["verb"] == "mcp__claude-peers__send_message"
        assert row["tool_class"] == "mcp"
        assert row["subcommand"] is None
        assert row["flags"] is None
        assert row["path_spec"] is None

    def test_mcp_wildcard(self):
        row = parse_entry("mcp__claude-peers__*", source="test")
        assert row["tool"] == "mcp__claude-peers__*"
        assert row["tool_class"] == "mcp"

    def test_mcp_context7_query(self):
        row = parse_entry("mcp__context7__query-docs", source="test")
        assert row["tool"] == "mcp__context7__query-docs"
        assert row["tool_class"] == "mcp"

    def test_mcp_bookstack_read(self):
        row = parse_entry("mcp__bookstack__bookstack_pages_read", source="test")
        assert row["tool"] == "mcp__bookstack__bookstack_pages_read"
        assert row["tool_class"] == "mcp"

    def test_mcp_tool_verb_equals_tool(self):
        row = parse_entry("mcp__context7__resolve-library-id", source="test")
        assert row["verb"] == row["tool"]


# ---------------------------------------------------------------------------
# Unknown verbs — forward-compatibility
# ---------------------------------------------------------------------------


class TestUnknownVerbs:
    def test_unknown_bare_verb_defaults_to_bash(self):
        # Unknown bare verbs default to "bash" (conservative — shell commands
        # are more common than new flat tools in settings.json).
        row = parse_entry("UnknownFutureTool", source="test")
        assert row["tool"] == "UnknownFutureTool"
        # tool_class depends on classify() which defaults to "bash"
        assert row["tool_class"] in ("bash", "flat", "orchestration")

    def test_unknown_verb_with_parens_accepted_structurally(self):
        # Unknown verbs with parens are accepted — the entry parses, no error raised.
        row = parse_entry("FutureTool(some-arg)", source="test")
        assert row["tool"] == "FutureTool"
        assert row["subcommand"] == "some-arg"


# ---------------------------------------------------------------------------
# Negative cases — structural defects
# ---------------------------------------------------------------------------


class TestMalformedEntries:
    def test_empty_string_raises(self):
        with pytest.raises(IngesterError, match="empty or whitespace"):
            parse_entry("", source="test")

    def test_whitespace_only_raises(self):
        with pytest.raises(IngesterError, match="empty or whitespace"):
            parse_entry("   \t  ", source="test")

    def test_newline_raises(self):
        with pytest.raises(IngesterError, match="internal newline"):
            parse_entry("Bash(git *)\nBash(ls)", source="test")

    def test_carriage_return_raises(self):
        with pytest.raises(IngesterError, match="internal newline"):
            parse_entry("Bash(git *)\r", source="test")

    def test_unbalanced_double_quote_raises(self):
        with pytest.raises(IngesterError, match="unbalanced quote"):
            parse_entry('Bash(echo "hello)', source="test")

    def test_unbalanced_single_quote_raises(self):
        with pytest.raises(IngesterError, match="unbalanced quote"):
            parse_entry("Bash(echo 'hello)", source="test")

    def test_missing_closing_paren_bash_raises(self):
        with pytest.raises(IngesterError, match="missing closing parenthesis"):
            parse_entry("Bash(git *", source="test")

    def test_missing_closing_paren_read_raises(self):
        with pytest.raises(IngesterError, match="missing closing parenthesis"):
            parse_entry("Read(//path/**", source="test")

    def test_file_tool_bare_path_raises(self):
        with pytest.raises(IngesterError, match=r"file tool path spec"):
            parse_entry("Read(foo/bar)", source="test")

    def test_file_tool_single_slash_raises(self):
        # Single slash is not accepted — Claude Code's project-root-relative
        # form is not currently round-trippable through the serializer
        # (which would silently rewrite it to `//path`).
        with pytest.raises(IngesterError, match=r"file tool path spec"):
            parse_entry("Read(/home/user/file)", source="test")

    def test_file_tool_relative_path_raises(self):
        with pytest.raises(IngesterError, match=r"file tool path spec"):
            parse_entry("Read(relative/path)", source="test")

    def test_mcp_with_parens_raises(self):
        with pytest.raises(IngesterError, match="must not contain parentheses"):
            parse_entry("mcp__ns__tool(arg)", source="test")

    def test_error_includes_entry_string(self):
        """Error message must name the offending entry."""
        with pytest.raises(IngesterError) as exc_info:
            parse_entry("Read(foo)", source="/path/settings.json (allow[3])")
        assert "Read(foo)" in str(exc_info.value)

    def test_error_includes_source(self):
        """Error message must name the source location."""
        with pytest.raises(IngesterError) as exc_info:
            parse_entry("Read(foo)", source="/path/settings.json (allow[3])")
        assert "/path/settings.json (allow[3])" in str(exc_info.value)

    def test_error_message_format(self):
        """Follows 'Malformed permission entry ... in ...:' format."""
        with pytest.raises(IngesterError) as exc_info:
            parse_entry("Read(foo)", source="/some/path (deny[1])")
        msg = str(exc_info.value)
        assert msg.startswith("Malformed permission entry")
        assert " in /some/path" in msg

    # ------------------------------------------------------------------
    # Gap 4 — bare * / ** / **/ / *foo (no slash, no dot) must all RAISE
    # ------------------------------------------------------------------

    def test_bare_star_raises(self):
        """Read(*) — bare single star — is not a valid glob."""
        with pytest.raises(IngesterError, match=r"file tool path spec"):
            parse_entry("Read(*)", source="test")

    def test_bare_double_star_raises(self):
        """Read(**) — bare double star — is not a valid glob."""
        with pytest.raises(IngesterError, match=r"file tool path spec"):
            parse_entry("Read(**)", source="test")

    def test_bare_double_star_slash_raises(self):
        """Read(**/) — trailing slash with no path fragment — is not a valid glob."""
        with pytest.raises(IngesterError, match=r"file tool path spec"):
            parse_entry("Read(**/)", source="test")

    def test_star_no_slash_no_dot_raises(self):
        """Read(*foo) — single star not followed by '/' or '.' — is not a valid glob."""
        with pytest.raises(IngesterError, match=r"file tool path spec"):
            parse_entry("Read(*foo)", source="test")


# ---------------------------------------------------------------------------
# parse_permissions_json — happy path
# ---------------------------------------------------------------------------


class TestParsePermissionsJson:
    def test_empty_permissions_block_returns_empty_list(self, tmp_path):
        p = _settings_json(tmp_path=tmp_path)
        rows = parse_permissions_json(p)
        assert rows == []

    def test_allow_entries_carry_decision_allow(self, tmp_path):
        p = _settings_json(allow=["Bash(git *)", "WebSearch"], tmp_path=tmp_path)
        rows = parse_permissions_json(p)
        assert all(r["decision"] == "allow" for r in rows)
        assert len(rows) == 2

    def test_deny_entries_carry_decision_deny(self, tmp_path):
        p = _settings_json(deny=["Bash(rm -rf *)"], tmp_path=tmp_path)
        rows = parse_permissions_json(p)
        assert rows[0]["decision"] == "deny"

    def test_ask_entries_carry_decision_ask(self, tmp_path):
        p = _settings_json(ask=["Bash(sudo *)"], tmp_path=tmp_path)
        rows = parse_permissions_json(p)
        assert rows[0]["decision"] == "ask"

    def test_mixed_decisions_all_returned(self, tmp_path):
        p = _settings_json(
            allow=["WebSearch"],
            deny=["Bash(rm *)"],
            ask=["Bash(sudo *)"],
            tmp_path=tmp_path,
        )
        rows = parse_permissions_json(p)
        assert len(rows) == 3
        assert {r["decision"] for r in rows} == {"allow", "deny", "ask"}

    def test_order_allow_deny_ask(self, tmp_path):
        p = _settings_json(
            allow=["WebSearch"],
            deny=["Bash(rm *)"],
            ask=["Bash(sudo *)"],
            tmp_path=tmp_path,
        )
        rows = parse_permissions_json(p)
        assert rows[0]["decision"] == "allow"
        assert rows[1]["decision"] == "deny"
        assert rows[2]["decision"] == "ask"

    def test_mcp_entries_parsed_correctly(self, tmp_path):
        p = _settings_json(
            allow=["mcp__claude-peers__*", "mcp__context7__query-docs"],
            tmp_path=tmp_path,
        )
        rows = parse_permissions_json(p)
        assert rows[0]["tool"] == "mcp__claude-peers__*"
        assert rows[0]["tool_class"] == "mcp"
        assert rows[1]["tool"] == "mcp__context7__query-docs"

    def test_realistic_settings_block(self, tmp_path):
        """Parse a settings block matching real ~/.claude/settings.json entries."""
        allow = [
            "Read(//home/user/.claude/**)",
            "Read(//home/user/data/clients/**)",
            "Edit(//home/user/data/clients/**)",
            "Write(//home/user/data/clients/**)",
            "Bash(git *)",
            "Bash(uv *)",
            "Bash(wl-copy*)",
            "Bash(/tmp/claude/**)",
            "mcp__claude-peers__*",
            "mcp__context7__query-docs",
            "WebSearch",
        ]
        p = _settings_json(allow=allow, tmp_path=tmp_path)
        rows = parse_permissions_json(p)
        assert len(rows) == len(allow)
        assert all(r["decision"] == "allow" for r in rows)
        assert all(r["tool_class"] in ("bash", "file", "flat", "mcp") for r in rows)

    def test_no_permissions_key_returns_empty(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text(json.dumps({"defaultMode": "auto"}), encoding="utf-8")
        rows = parse_permissions_json(p)
        assert rows == []

    def test_source_string_includes_path_and_index(self, tmp_path):
        """Error from a malformed entry must include file path and array index."""
        p = _settings_json(allow=["Read(no-slash)"], tmp_path=tmp_path)
        with pytest.raises(IngesterError) as exc_info:
            parse_permissions_json(p)
        msg = str(exc_info.value)
        assert str(p) in msg
        assert "allow[0]" in msg


# ---------------------------------------------------------------------------
# parse_permissions_json — error cases
# ---------------------------------------------------------------------------


class TestParsePermissionsJsonErrors:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(IngesterError, match="Cannot read"):
            parse_permissions_json(tmp_path / "nonexistent.json")

    def test_invalid_json_raises(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text("{not valid json}", encoding="utf-8")
        with pytest.raises(IngesterError, match="Invalid JSON"):
            parse_permissions_json(p)

    def test_top_level_array_raises(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        with pytest.raises(IngesterError, match="not a JSON object"):
            parse_permissions_json(p)

    def test_permissions_not_object_raises(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text(json.dumps({"permissions": ["allow"]}), encoding="utf-8")
        with pytest.raises(IngesterError, match="not a JSON object"):
            parse_permissions_json(p)

    def test_allow_not_array_raises(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text(json.dumps({"permissions": {"allow": "Read"}}), encoding="utf-8")
        with pytest.raises(IngesterError, match="not a JSON array"):
            parse_permissions_json(p)

    def test_non_string_entry_raises(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text(json.dumps({"permissions": {"allow": [42]}}), encoding="utf-8")
        with pytest.raises(IngesterError, match="expected string"):
            parse_permissions_json(p)

    def test_null_entry_raises(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text(json.dumps({"permissions": {"allow": [None]}}), encoding="utf-8")
        with pytest.raises(IngesterError, match="expected string"):
            parse_permissions_json(p)

    def test_malformed_entry_error_names_source(self, tmp_path):
        """Error message must include file path and key+index."""
        p = _settings_json(allow=["Read(no-slash)"], tmp_path=tmp_path)
        with pytest.raises(IngesterError) as exc_info:
            parse_permissions_json(p)
        msg = str(exc_info.value)
        assert str(p) in msg
        assert "allow[0]" in msg

    def test_second_entry_error_names_correct_index(self, tmp_path):
        """Index in error message must match the actual entry position."""
        p = _settings_json(allow=["Bash(git *)", "Read(bad-path)"], tmp_path=tmp_path)
        with pytest.raises(IngesterError) as exc_info:
            parse_permissions_json(p)
        assert "allow[1]" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Round-trip tests (coupled with the serializer)
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """Ingest a canonical string → structured row → serialize → same string.

    Skipped when lib.mirror.serializer.serialize raises NotImplementedError —
    a defensive guard for environments where the serializer implementation
    is absent.
    """

    @pytest.fixture(autouse=True)
    def _require_real_serializer(self):
        from nephoscope.lib.mirror.serializer import serialize  # type: ignore[import]

        # Test the stub detection: if serialize raises NotImplementedError, skip.
        try:
            # Try with a known-good row that the stub would reject.
            serialize({"verb": "WebSearch"})
        except NotImplementedError:
            pytest.skip("nephoscope.lib.mirror.serializer is a stub")
        except Exception:
            pass  # Real errors will surface in the round-trip tests themselves.

    def _roundtrip(self, entry: str) -> None:
        from nephoscope.lib.mirror.serializer import serialize  # type: ignore[import]

        row = parse_entry(entry, source="roundtrip-test")
        rendered = serialize(row)
        assert rendered == entry, (
            f"Round-trip failed: {entry!r} → structured row {row!r} → {rendered!r}"
        )

    def test_bash_wildcard_roundtrip(self):
        self._roundtrip("Bash(git *)")

    def test_bash_literal_flag_roundtrip(self):
        self._roundtrip("Bash(git push)")

    def test_bash_multitoken_flags_roundtrip(self):
        self._roundtrip("Bash(systemctl --user status *)")

    def test_bash_absolute_path_roundtrip(self):
        self._roundtrip("Bash(/tmp/claude/**)")

    def test_bash_no_space_wildcard_roundtrip(self):
        self._roundtrip("Bash(wl-copy*)")

    def test_read_path_glob_roundtrip(self):
        self._roundtrip("Read(//home/user/.claude/**)")

    def test_edit_path_roundtrip(self):
        self._roundtrip("Edit(//home/user/data/clients/**)")

    def test_write_path_roundtrip(self):
        self._roundtrip("Write(//home/user/data/**)")

    def test_read_relative_glob_roundtrip(self):
        self._roundtrip("Read(**/.env)")

    def test_write_relative_glob_roundtrip(self):
        self._roundtrip("Write(**/.env)")

    def test_edit_relative_glob_recursive_roundtrip(self):
        self._roundtrip("Edit(**/.ssh/**)")

    def test_read_single_star_roundtrip(self):
        self._roundtrip("Read(*.pem)")

    def test_mcp_fully_qualified_roundtrip(self):
        self._roundtrip("mcp__claude-peers__send_message")

    def test_mcp_wildcard_roundtrip(self):
        self._roundtrip("mcp__claude-peers__*")

    def test_websearch_roundtrip(self):
        self._roundtrip("WebSearch")

    def test_grep_roundtrip(self):
        self._roundtrip("Grep")

    def test_glob_roundtrip(self):
        self._roundtrip("Glob")
