"""Tests for plugin hook registration in hooks/hooks.json and pyproject.toml.

These tests verify that the PostToolUse output scanner is correctly wired
into the plugin manifest so Claude Code picks it up automatically (without
runtime registration via nephoscope-init).

The plugin system reads hooks/hooks.json and merges it into settings.json.
The console script must be installed via pyproject.toml [project.scripts].
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
PYPROJECT_TOML = REPO_ROOT / "pyproject.toml"


def test_hooks_json_is_valid_json() -> None:
    """hooks.json must parse without error."""
    text = HOOKS_JSON.read_text(encoding="utf-8")
    data = json.loads(text)
    assert isinstance(data, dict)


def test_posttooluse_output_scanner_registered() -> None:
    """At least one PostToolUse entry must invoke nephoscope-output-scanner."""
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    post_tool_use = data["hooks"]["PostToolUse"]

    found = False
    for entry in post_tool_use:
        for hook in entry.get("hooks", []):
            command = hook.get("command", "")
            if "nephoscope-output-scanner" in command:
                found = True
                break

    assert found, (
        "No PostToolUse entry references nephoscope-output-scanner; "
        "the output scanner hook is not wired into hooks/hooks.json"
    )


def test_output_scanner_console_script_registered() -> None:
    """pyproject.toml must declare the nephoscope-output-scanner console script."""
    text = PYPROJECT_TOML.read_text(encoding="utf-8")
    assert "nephoscope-output-scanner" in text, (
        "pyproject.toml does not declare the nephoscope-output-scanner "
        "console script under [project.scripts]"
    )


def test_hooks_json_posttooluse_has_recorder() -> None:
    """The existing recorder entry with '*' matcher must remain (regression guard)."""
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    post_tool_use = data["hooks"]["PostToolUse"]

    found = False
    for entry in post_tool_use:
        if entry.get("matcher") != "*":
            continue
        for hook in entry.get("hooks", []):
            command = hook.get("command", "")
            if "nephoscope-recorder post" in command:
                found = True
                break

    assert found, (
        'The PostToolUse recorder entry with "*" matcher is missing; '
        "this is a regression — output scanner registration must not "
        "displace the recorder"
    )
