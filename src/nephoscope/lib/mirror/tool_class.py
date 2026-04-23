"""Tool-class mapping: verb → tool-class tag.

This is the single source of truth used by:

- ``lib.mirror.serializer`` — maps ``rule_shapes.verb`` (a shell command name
  such as ``"git"`` or a tool name such as ``"Read"``) to the class needed to
  pick the correct renderer.
- ``lib.mirror.ingester`` — maps the outer verb of a settings.json entry
  (``"Bash"``, ``"Read"``, ``"mcp__ns__tool"``, …) to the class needed to
  pick the correct parser.

Tool classes
------------
bash          — Shell commands dispatched through the Bash tool.  In
                serialiser context the ``rule_shapes.verb`` is the shell
                command name (e.g. ``"git"``).  In ingester context the
                outer prefix is the literal string ``"Bash"``.
file          — File I/O tools (Read, Edit, Write, MultiEdit, NotebookEdit);
                args are encoded as ``//abs/path``.
flat          — Tools with no meaningful argument structure for permission
                purposes (Grep, Glob, WebSearch).  Always serialised bare.
mcp           — Fully-qualified MCP tool names: ``mcp__<ns>__<tool>`` or
                ``mcp__<ns>__*``.
orchestration — Agent-coordination tools (Agent, Task*, SendMessage, …).
                Always default-allow; never appear in JSON mirror entries.

Default
-------
Any verb not matching the explicit sets above — including arbitrary shell
commands such as ``git``, ``npm``, ``python`` — defaults to ``"bash"``.
This is correct for the serialiser's use of ``rule_shapes.verb``.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Canonical verb sets
# ---------------------------------------------------------------------------

# Tools whose settings.json form is ``Verb(//abs/path)`` or bare ``Verb``.
FILE_VERBS: frozenset[str] = frozenset(
    {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"}
)

# Tools that are always serialised bare (no argument encoding).
FLAT_VERBS: frozenset[str] = frozenset({"Grep", "Glob", "WebSearch"})

# Tools that are default-allow and never appear in settings.json mirror
# entries.  Listed exhaustively so classification is deterministic even if
# an entry somehow shows up during ingestion.
ORCHESTRATION_VERBS: frozenset[str] = frozenset(
    {
        "Agent",
        "EnterPlanMode",
        "ExitPlanMode",
        "EnterWorktree",
        "ExitWorktree",
        "Task",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskUpdate",
        "TaskStop",
        "TaskOutput",
        "SendMessage",
        "TeamCreate",
        "TeamDelete",
        "ScheduleWakeup",
        "ToolSearch",
    }
)

# MCP tool names match ``mcp__<namespace>__<tool_or_wildcard>``.
# Namespace and tool segments allow word-chars and hyphens.
_MCP_RE = re.compile(r"^mcp__[\w-]+__(?:[\w-]+|\*)$")

# The literal prefix ``"Bash"`` appears in settings.json entries such as
# ``Bash(git push)`` and is the ingester's outer verb; handled explicitly so
# the ingester can call ``classify`` without a separate lookup.
_BASH_PREFIX = "Bash"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify(verb: str) -> str:
    """Return the tool-class tag for *verb*.

    Accepts both:

    - ``rule_shapes.verb`` values (shell command names such as ``"git"`` or
      tool names such as ``"Read"``, ``"mcp__ns__tool"``).
    - The outer prefix of a settings.json entry (``"Bash"``, ``"Read"``,
      ``"mcp__ns__tool"``).

    Returns one of ``"bash"``, ``"file"``, ``"flat"``, ``"mcp"``, or
    ``"orchestration"``.  Any verb not matching a named class is returned
    as ``"bash"`` — the correct default for unrecognised shell commands.

    Parameters
    ----------
    verb:
        Non-empty string.

    Raises
    ------
    ValueError
        If *verb* is not a non-empty string.
    """
    if not verb or not isinstance(verb, str):
        raise ValueError(f"verb must be a non-empty string, got {verb!r}")

    # MCP first — prefix check is fast and unambiguous.
    if _MCP_RE.match(verb):
        return "mcp"

    # Explicit Bash prefix from settings.json entries.
    if verb == _BASH_PREFIX:
        return "bash"

    if verb in FILE_VERBS:
        return "file"
    if verb in FLAT_VERBS:
        return "flat"
    if verb in ORCHESTRATION_VERBS:
        return "orchestration"

    # Default: any shell command or unrecognised verb is bash.
    return "bash"


# Alias used by W1B ingester — identical semantics, different call-site name.
tool_class_for = classify
