"""Tests for lib.mirror.serializer and lib.mirror.tool_class.

Covers every canonical form documented in the Phase 8.5 pre-flight checklist:
- Bash: literal, pattern (subcommand wildcard), flags-wildcard
- File: path-specified (glob + exact), bare (no path_spec)
- Flat: bare verb only
- MCP: fully-qualified literal, namespace wildcard
- Orchestration: always returns None (default-allow, no mirror entry)

Also covers:
- classify() / tool_class_for() classification
- Error paths: missing/empty verb, relative path in file rule
- Edge: empty string path_spec treated as bare
"""

from __future__ import annotations

import pytest

from nephoscope.lib.mirror.tool_class import (
    FILE_VERBS,
    FLAT_VERBS,
    ORCHESTRATION_VERBS,
    classify,
    tool_class_for,
)
from nephoscope.lib.mirror.serializer import serialize


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def row(
    verb: str,
    subcommand: str | None = None,
    flags: str = "[]",
    path_spec: str | None = None,
) -> dict:
    """Convenience builder for a rule_shapes-like dict."""
    return {
        "verb": verb,
        "subcommand": subcommand,
        "flags": flags,
        "path_spec": path_spec,
    }


# ---------------------------------------------------------------------------
# classify() / tool_class_for()
# ---------------------------------------------------------------------------


class TestClassify:
    def test_bash_prefix_string(self):
        assert classify("Bash") == "bash"

    def test_bash_shell_command_git(self):
        assert classify("git") == "bash"

    def test_bash_shell_command_npm(self):
        assert classify("npm") == "bash"

    def test_bash_shell_command_python(self):
        assert classify("python") == "bash"

    def test_bash_shell_command_grep_lowercase(self):
        # lowercase grep is a shell command — Bash class
        assert classify("grep") == "bash"

    def test_file_read(self):
        assert classify("Read") == "file"

    def test_file_edit(self):
        assert classify("Edit") == "file"

    def test_file_write(self):
        assert classify("Write") == "file"

    def test_file_multiedit(self):
        assert classify("MultiEdit") == "file"

    def test_file_notebookedit(self):
        assert classify("NotebookEdit") == "file"

    def test_flat_grep_uppercase(self):
        assert classify("Grep") == "flat"

    def test_flat_glob(self):
        assert classify("Glob") == "flat"

    def test_flat_websearch(self):
        assert classify("WebSearch") == "flat"

    def test_mcp_literal(self):
        assert classify("mcp__claude-peers__send_message") == "mcp"

    def test_mcp_wildcard(self):
        assert classify("mcp__claude-peers__*") == "mcp"

    def test_mcp_different_namespace(self):
        assert classify("mcp__bookstack__bookstack_pages_read") == "mcp"

    def test_orchestration_agent(self):
        assert classify("Agent") == "orchestration"

    def test_orchestration_task_create(self):
        assert classify("TaskCreate") == "orchestration"

    def test_orchestration_task_update(self):
        assert classify("TaskUpdate") == "orchestration"

    def test_orchestration_send_message(self):
        assert classify("SendMessage") == "orchestration"

    def test_orchestration_team_create(self):
        assert classify("TeamCreate") == "orchestration"

    def test_orchestration_team_delete(self):
        assert classify("TeamDelete") == "orchestration"

    def test_orchestration_schedule_wakeup(self):
        assert classify("ScheduleWakeup") == "orchestration"

    def test_orchestration_tool_search(self):
        assert classify("ToolSearch") == "orchestration"

    def test_classify_raises_on_empty_string(self):
        with pytest.raises(ValueError):
            classify("")

    def test_classify_raises_on_none(self):
        with pytest.raises(ValueError):
            classify(None)  # type: ignore[arg-type]

    def test_tool_class_for_alias_matches_classify(self):
        """tool_class_for is the ingester alias — must be identical."""
        for verb in ["Bash", "git", "Read", "Grep", "mcp__ns__*", "Agent"]:
            assert tool_class_for(verb) == classify(verb)

    def test_all_file_verbs_classified(self):
        for v in FILE_VERBS:
            assert classify(v) == "file", f"Expected 'file' for {v!r}"

    def test_all_flat_verbs_classified(self):
        for v in FLAT_VERBS:
            assert classify(v) == "flat", f"Expected 'flat' for {v!r}"

    def test_all_orchestration_verbs_classified(self):
        for v in ORCHESTRATION_VERBS:
            assert classify(v) == "orchestration", f"Expected 'orchestration' for {v!r}"


# ---------------------------------------------------------------------------
# serialize() — Bash class
# ---------------------------------------------------------------------------


class TestSerializeBash:
    def test_literal_verb_and_subcommand(self):
        """Bash(git push) — the canonical literal form."""
        assert serialize(row("git", subcommand="push")) == "Bash(git push)"

    def test_literal_verb_only(self):
        """Bash(git) — verb with no subcommand, no flags pattern."""
        assert serialize(row("git")) == "Bash(git)"

    def test_pattern_flags_wildcard_no_subcommand(self):
        """Bash(git *) — flags wildcard, no subcommand → "any git command"."""
        assert serialize(row("git", flags="*")) == "Bash(git *)"

    def test_pattern_flags_wildcard_with_subcommand(self):
        """Bash(git push *) — literal subcommand + flags wildcard."""
        assert serialize(row("git", subcommand="push", flags="*")) == "Bash(git push *)"

    def test_literal_with_specific_flags_json_ignored(self):
        """Specific flags in JSON are not encoded in the canonical string."""
        result = serialize(row("git", subcommand="push", flags='["--force"]'))
        assert result == "Bash(git push)"

    def test_empty_flags_json_array(self):
        assert (
            serialize(row("git", subcommand="commit", flags="[]")) == "Bash(git commit)"
        )

    def test_bash_verb_lowercase_grep(self):
        """lowercase grep is a shell command → bash class."""
        assert serialize(row("grep")) == "Bash(grep)"

    def test_bash_flags_wildcard_lowercase_grep(self):
        assert serialize(row("grep", flags="*")) == "Bash(grep *)"

    def test_bash_npm_run(self):
        assert serialize(row("npm", subcommand="test")) == "Bash(npm test)"

    def test_bash_uv_run(self):
        assert (
            serialize(row("uv", subcommand="pytest", flags="*")) == "Bash(uv pytest *)"
        )

    def test_bash_none_flags_treated_as_empty(self):
        """None flags treated as no-flags (same as empty array)."""
        result = serialize(row("git", subcommand="status", flags=None))  # type: ignore[arg-type]
        assert result == "Bash(git status)"


# ---------------------------------------------------------------------------
# serialize() — File class
# ---------------------------------------------------------------------------


class TestSerializeFile:
    # ---- Ingester convention: path_spec has // prefix (from parse_entry) ----
    # The ingester stores path_spec including the // prefix so that
    # serialize(ingester_row) → canonical string without double-adding //.

    def test_read_ingester_format_double_slash(self):
        """Ingester-produced path_spec (// prefix) → wrapped directly."""
        assert serialize(row("Read", path_spec="//home/user/.claude/**")) == (
            "Read(//home/user/.claude/**)"
        )

    def test_edit_ingester_format_double_slash(self):
        assert serialize(row("Edit", path_spec="//home/user/data/clients/**")) == (
            "Edit(//home/user/data/clients/**)"
        )

    def test_write_ingester_format_double_slash(self):
        assert serialize(row("Write", path_spec="//home/user/data/**")) == (
            "Write(//home/user/data/**)"
        )

    # ---- DB-native convention: path_spec is a real absolute path (single /) ----
    # The mirror writer resolves $VAR tokens to absolute paths and passes
    # them to serialize(); the serializer converts /abs → //abs.

    def test_read_with_abs_glob_single_slash(self):
        """DB-native single-slash absolute path → canonical //."""
        assert serialize(row("Read", path_spec="/abs/**")) == "Read(//abs/**)"

    def test_read_with_abs_glob_home_single_slash(self):
        result = serialize(row("Read", path_spec="/home/user/.claude/**"))
        assert result == "Read(//home/user/.claude/**)"

    def test_edit_with_exact_abs_path_single_slash(self):
        """Edit with a specific absolute file path."""
        assert serialize(row("Edit", path_spec="/abs/path/file.py")) == (
            "Edit(//abs/path/file.py)"
        )

    def test_write_with_abs_glob_single_slash(self):
        assert serialize(row("Write", path_spec="/tmp/scratch/**")) == (
            "Write(//tmp/scratch/**)"
        )

    def test_multiedit_with_path_single_slash(self):
        assert serialize(row("MultiEdit", path_spec="/home/user/projects/**")) == (
            "MultiEdit(//home/user/projects/**)"
        )

    # ---- Bare form (no path constraint) ----

    def test_read_bare_no_path_spec(self):
        """Read alone — no path_spec means no path constraint."""
        assert serialize(row("Read")) == "Read"

    def test_edit_bare_none_path_spec(self):
        assert serialize(row("Edit", path_spec=None)) == "Edit"

    def test_write_bare_empty_path_spec(self):
        """Empty string path_spec treated as bare."""
        assert serialize(row("Write", path_spec="")) == "Write"

    def test_notebookedit_bare(self):
        assert serialize(row("NotebookEdit")) == "NotebookEdit"

    # ---- Error cases ----

    def test_file_relative_path_raises(self):
        """Relative path_spec must raise — not silently accepted."""
        with pytest.raises(ValueError, match="absolute"):
            serialize(row("Read", path_spec="relative/path"))

    def test_file_path_spec_starts_double_dollar_raises(self):
        """$VAR-anchored path_spec must be resolved before serialisation."""
        with pytest.raises(ValueError, match="absolute"):
            serialize(row("Read", path_spec="$HOME/.claude/**"))


# ---------------------------------------------------------------------------
# serialize() — Flat class
# ---------------------------------------------------------------------------


class TestSerializeFlat:
    def test_grep_uppercase_bare(self):
        assert serialize(row("Grep")) == "Grep"

    def test_glob_bare(self):
        assert serialize(row("Glob")) == "Glob"

    def test_websearch_bare(self):
        assert serialize(row("WebSearch")) == "WebSearch"

    def test_flat_ignores_subcommand(self):
        """Flat tools are always bare — subcommand field is not encoded."""
        assert serialize(row("Grep", subcommand="ignored")) == "Grep"

    def test_flat_ignores_flags(self):
        """Flat tools are always bare — flags field is not encoded."""
        assert serialize(row("Grep", flags="*")) == "Grep"

    def test_flat_ignores_path_spec(self):
        """Flat tools are always bare — path_spec is not encoded."""
        assert serialize(row("Grep", path_spec="/some/path")) == "Grep"


# ---------------------------------------------------------------------------
# serialize() — MCP class
# ---------------------------------------------------------------------------


class TestSerializeMcp:
    def test_mcp_literal_tool(self):
        """mcp__ns__tool — fully-qualified literal form."""
        assert serialize(row("mcp__claude-peers__send_message")) == (
            "mcp__claude-peers__send_message"
        )

    def test_mcp_wildcard(self):
        """mcp__ns__* — namespace wildcard."""
        assert serialize(row("mcp__claude-peers__*")) == "mcp__claude-peers__*"

    def test_mcp_bookstack(self):
        assert serialize(row("mcp__bookstack__bookstack_pages_read")) == (
            "mcp__bookstack__bookstack_pages_read"
        )

    def test_mcp_bookstack_wildcard(self):
        assert serialize(row("mcp__bookstack__*")) == "mcp__bookstack__*"

    def test_mcp_ignores_subcommand(self):
        """MCP verb is fully-qualified; subcommand field is not used."""
        assert serialize(row("mcp__ns__tool", subcommand="extra")) == "mcp__ns__tool"

    def test_mcp_ignores_flags(self):
        assert serialize(row("mcp__ns__tool", flags="*")) == "mcp__ns__tool"


# ---------------------------------------------------------------------------
# serialize() — Orchestration class
# ---------------------------------------------------------------------------


class TestSerializeOrchestration:
    def test_agent_returns_none(self):
        """Agent is always default-allow — serialize returns None."""
        assert serialize(row("Agent")) is None

    def test_task_create_returns_none(self):
        assert serialize(row("TaskCreate")) is None

    def test_task_update_returns_none(self):
        assert serialize(row("TaskUpdate")) is None

    def test_task_get_returns_none(self):
        assert serialize(row("TaskGet")) is None

    def test_task_list_returns_none(self):
        assert serialize(row("TaskList")) is None

    def test_task_stop_returns_none(self):
        assert serialize(row("TaskStop")) is None

    def test_send_message_returns_none(self):
        assert serialize(row("SendMessage")) is None

    def test_team_create_returns_none(self):
        assert serialize(row("TeamCreate")) is None

    def test_team_delete_returns_none(self):
        assert serialize(row("TeamDelete")) is None

    def test_schedule_wakeup_returns_none(self):
        assert serialize(row("ScheduleWakeup")) is None

    def test_tool_search_returns_none(self):
        assert serialize(row("ToolSearch")) is None


# ---------------------------------------------------------------------------
# serialize() — Error paths
# ---------------------------------------------------------------------------


class TestSerializeErrors:
    def test_missing_verb_key_raises(self):
        with pytest.raises(ValueError):
            serialize({"subcommand": "push"})

    def test_empty_verb_raises(self):
        with pytest.raises(ValueError):
            serialize(row(""))

    def test_none_verb_raises(self):
        with pytest.raises(ValueError):
            serialize({"verb": None})

    def test_non_string_verb_raises(self):
        with pytest.raises(ValueError):
            serialize({"verb": 42})


# ---------------------------------------------------------------------------
# Phase B3 — file-tool relative glob serialization
# ---------------------------------------------------------------------------


class TestSerializeFileRelativeGlob:
    """File-tool rows with relative glob path_specs serialize as Verb(glob)."""

    def test_read_double_star_env_file(self):
        """Read row with '**/.env' path_spec serializes as Read(**/.env)."""
        assert serialize(row("Read", path_spec="**/.env")) == "Read(**/.env)"

    def test_write_double_star_env_file(self):
        """Write row with '**/.env' path_spec serializes as Write(**/.env)."""
        assert serialize(row("Write", path_spec="**/.env")) == "Write(**/.env)"

    def test_edit_double_star_env_file(self):
        """Edit row with '**/.env' path_spec serializes as Edit(**/.env)."""
        assert serialize(row("Edit", path_spec="**/.env")) == "Edit(**/.env)"

    def test_read_double_star_env_local(self):
        assert (
            serialize(row("Read", path_spec="**/.env.local")) == "Read(**/.env.local)"
        )

    def test_read_extension_glob(self):
        """Read row with '**/*.pem' extension glob serializes correctly."""
        assert serialize(row("Read", path_spec="**/*.pem")) == "Read(**/*.pem)"

    def test_read_directory_glob(self):
        """Read row with '**/secrets/**' directory glob serializes correctly."""
        assert (
            serialize(row("Read", path_spec="**/secrets/**")) == "Read(**/secrets/**)"
        )

    def test_read_deep_path_glob(self):
        """Read row with '**/config/database.yml' deep-path glob serializes correctly."""
        assert serialize(row("Read", path_spec="**/config/database.yml")) == (
            "Read(**/config/database.yml)"
        )

    def test_read_bare_star_path(self):
        """Read row with '*' single-star glob serializes correctly."""
        assert serialize(row("Read", path_spec="*")) == "Read(*)"

    def test_existing_absolute_path_unaffected(self):
        """Existing absolute path rendering is unchanged by the new code path."""
        assert serialize(row("Read", path_spec="/abs/path/**")) == "Read(//abs/path/**)"

    def test_existing_double_slash_unaffected(self):
        """Existing //-prefixed path rendering is unchanged."""
        assert (
            serialize(row("Read", path_spec="//abs/path/**")) == "Read(//abs/path/**)"
        )

    def test_relative_non_glob_still_raises(self):
        """Non-glob relative paths (no leading * or **) still raise ValueError."""
        with pytest.raises(ValueError, match="absolute"):
            serialize(row("Read", path_spec="relative/path"))

    def test_relative_non_glob_plain_word_raises(self):
        """Plain word path_spec (no glob chars) still raises ValueError."""
        with pytest.raises(ValueError, match="absolute"):
            serialize(row("Read", path_spec="somefile.env"))
