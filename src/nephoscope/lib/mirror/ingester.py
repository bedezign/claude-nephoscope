"""Parse permissions.allow/deny/ask entry strings → structured row dicts.

Inverse of lib.mirror.serializer. Used by the reconcile engine and
import tooling to ingest settings.json content into structured form.

Structured row fields
---------------------
  tool        — Claude Code tool name (``"Bash"``, ``"Read"``, ``"mcp__ns__tool"``).
                For Bash entries, this is ``"Bash"`` while ``verb`` is the shell
                command; for all other entries ``tool == verb``.
  verb        — The ``rule_shapes.verb`` value expected by the serializer.
                For Bash: the shell command (e.g. ``"git"``).
                For file / flat / MCP: the tool name (same as ``tool``).
  path_spec   — Path glob for file tools (content between the parens, including
                the ``//`` prefix), e.g. ``"//home/steve/.claude/**"``.
                ``None`` for non-file entries.
  subcommand  — For Bash: the tokens between the shell command and any trailing
                `` *``, e.g. ``"--user status"`` from ``Bash(systemctl --user status *)``.
                ``None`` when there are no middle tokens.
  flags       — ``"*"`` when the entry ends with `` *`` (flags wildcard).
                ``"[]"`` when there is no wildcard.
                ``None`` for non-Bash entries.
  tool_class  — ``"bash"`` | ``"file"`` | ``"flat"`` | ``"mcp"`` | ``"orchestration"``

When returned by ``parse_permissions_json``, each dict also carries:
  decision    — ``"allow"`` | ``"deny"`` | ``"ask"``

Rejection policy
----------------
Any structural defect raises ``IngesterError`` naming **both** the offending
string and its source (file path + JSON key + index).  No silent normalization,
no fuzzy matching, no "helpful" coercion.

Bash parsing detail
-------------------
The canonical form ``Bash(<shell_cmd> [<sub>] [*])`` is parsed as:

1. If ``args`` ends with `` *`` (space + asterisk): flags wildcard is set,
   strip the suffix, then split the remainder on first whitespace to get
   ``(verb, subcommand)``.
2. Otherwise: no wildcard (``flags="[]"``), split args on first whitespace
   to get ``(verb, subcommand)``.

Examples::

    "Bash(git *)"                       → verb="git",  sub=None,             flags="*"
    "Bash(git push)"                    → verb="git",  sub="push",           flags="[]"
    "Bash(systemctl --user status *)"   → verb="systemctl", sub="--user status", flags="*"
    "Bash(wl-copy*)"                    → verb="wl-copy*",  sub=None,        flags="[]"
    "Bash(/tmp/claude/**)"              → verb="/tmp/claude/**", sub=None,   flags="[]"
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nephoscope.lib.mirror.tool_class import classify


class IngesterError(ValueError):
    """Raised when a permission entry cannot be parsed.

    The message always includes:
    - the offending entry string (repr-quoted)
    - the source location (file path + JSON key + index)
    - a short description of the defect
    """


# Flags wildcard sentinel — must match lib.mirror.serializer._FLAGS_WILDCARD.
_FLAGS_WILDCARD = "*"
_FLAGS_NONE = "[]"

# The Bash entry outer prefix.
_BASH_PREFIX = "Bash"


def _parse_bash_args(args: str) -> tuple[str, str | None, str]:
    """Split Bash entry args into ``(verb, subcommand, flags)``.

    Parameters
    ----------
    args:
        The content inside the parentheses, e.g. ``"git *"`` or
        ``"systemctl --user status *"`` or ``"wl-copy*"``.

    Returns
    -------
    Tuple of ``(verb, subcommand, flags)`` where:
    - ``verb`` is the first whitespace-delimited token (the shell command).
    - ``subcommand`` is the middle token(s) if present, else ``None``.
    - ``flags`` is ``"*"`` if the original args ended with `` *``, else ``"[]"``.
    """
    is_wildcard = args.endswith(" *")
    if is_wildcard:
        remainder = args[:-2]  # strip trailing " *"
        flags = _FLAGS_WILDCARD
    else:
        remainder = args
        flags = _FLAGS_NONE

    parts = remainder.split(None, 1)
    verb = parts[0]
    subcommand = parts[1] if len(parts) > 1 else None
    return verb, subcommand, flags


def parse_entry(entry: str, *, source: str) -> dict[str, Any]:
    """Parse one permissions entry string into a structured row dict.

    Parameters
    ----------
    entry:
        The raw string from a ``permissions.allow/deny/ask`` JSON array,
        e.g. ``"Bash(git *)"`` or ``"Read(//home/steve/.claude/**)"`` or
        ``"mcp__claude-peers__*"``.
    source:
        Location descriptor for error messages.  Convention:
        ``"<path> (<key>[<index>])"`` — e.g.
        ``"/home/steve/.claude/settings.json (allow[3])"``.

    Returns
    -------
    dict with keys: ``tool``, ``verb``, ``path_spec``, ``subcommand``,
    ``flags``, ``tool_class``.  ``decision`` is NOT set here;
    ``parse_permissions_json`` attaches it after iterating over the arrays.

    Raises
    ------
    IngesterError
        On any structural defect.  Message format:
        ``Malformed permission entry '<entry>' in <source>: <detail>``
    """

    def _err(detail: str) -> IngesterError:
        return IngesterError(
            f"Malformed permission entry {entry!r} in {source}: {detail}"
        )

    # --- Reject empty / whitespace-only --------------------------------
    if not entry or not entry.strip():
        raise _err("entry is empty or whitespace-only")

    # --- Reject entries with internal newlines -------------------------
    if "\n" in entry or "\r" in entry:
        raise _err("entry contains an internal newline")

    # --- Reject unbalanced quotes -------------------------------------
    # Canonical forms never use quote characters.  An odd count signals
    # a truncated or malformed string (e.g. Bash(echo "hello)).
    if entry.count('"') % 2 != 0 or entry.count("'") % 2 != 0:
        raise _err("entry contains unbalanced quote characters")

    # --- MCP: mcp__namespace__tool[name|*] ----------------------------
    # MCP entries are bare names (no parens).  A paren inside an MCP name
    # is a structural error.
    if entry.startswith("mcp__"):
        if "(" in entry or ")" in entry:
            raise _err("MCP tool names must not contain parentheses")
        return {
            "tool": entry,
            "verb": entry,
            "path_spec": None,
            "subcommand": None,
            "flags": None,
            "tool_class": "mcp",
        }

    # --- Verb(args) form ----------------------------------------------
    if "(" in entry:
        if not entry.endswith(")"):
            raise _err("missing closing parenthesis")

        paren_pos = entry.index("(")
        outer_verb = entry[:paren_pos]
        # Strip the outermost ( … ) — inner parens are part of the args.
        args = entry[paren_pos + 1 : -1]

        if not outer_verb:
            raise _err("tool name is empty before parenthesis")

        tc = classify(outer_verb)

        # --- Bash: only when the outer verb is literally "Bash" ------------
        # Unknown verbs that happen to classify as "bash" (the default class
        # for unrecognised verbs) go to the forward-compatibility branch below.
        if outer_verb == _BASH_PREFIX:
            stripped = args.strip()
            if not stripped:
                raise _err("Bash entry has an empty argument list")
            shell_verb, subcommand, flags = _parse_bash_args(stripped)
            return {
                "tool": _BASH_PREFIX,  # always "Bash" for Bash entries
                "verb": shell_verb,  # shell command (e.g. "git")
                "path_spec": None,
                "subcommand": subcommand,
                "flags": flags,
                "tool_class": "bash",
            }

        if tc == "file":
            # File tools (Read, Edit, Write, MultiEdit, NotebookEdit) require
            # a // path prefix inside the parens.
            if not args.startswith("//"):
                raise _err(f"file tool path spec must start with '//' (got {args!r})")
            return {
                "tool": outer_verb,
                "verb": outer_verb,
                # path_spec includes the // prefix as stored in the entry.
                # Serializer should use f"{verb}({path_spec})" not f"{verb}(//{path_spec})".
                "path_spec": args,
                "subcommand": None,
                "flags": None,
                "tool_class": tc,
            }

        # Unknown verb with parens — accept for forward-compatibility.
        # Store outer_verb as both tool and verb; treat args as subcommand.
        return {
            "tool": outer_verb,
            "verb": outer_verb,
            "path_spec": None,
            "subcommand": args if args else None,
            "flags": None,
            "tool_class": tc,
        }

    # --- Bare tool name (no parens) -----------------------------------
    # Covers flat tools (WebSearch, Grep, Glob, …) and any unknown future
    # tool that doesn't carry arguments.
    tc = classify(entry)
    return {
        "tool": entry,
        "verb": entry,
        "path_spec": None,
        "subcommand": None,
        "flags": None,
        "tool_class": tc,
    }


def parse_permissions_json(path: Path) -> list[dict[str, Any]]:
    """Parse a settings.json-shaped file into structured permission rows.

    Reads the ``permissions.allow``, ``permissions.deny``, and
    ``permissions.ask`` arrays.  Each returned dict has all fields from
    ``parse_entry`` plus a ``decision`` key (``"allow"``, ``"deny"``, or
    ``"ask"``).

    Parameters
    ----------
    path:
        Absolute path to the JSON file (e.g. ``~/.claude/settings.json`` or
        ``<project>/.claude/settings.local.json``).

    Returns
    -------
    List of structured row dicts, one per entry across all three arrays.
    Order: allow entries first, then deny, then ask (matching array order
    within each key).

    Raises
    ------
    IngesterError
        On file read failure, JSON parse failure, malformed ``permissions``
        block, or any malformed entry string.  Error messages name the
        offending entry AND its exact source location.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise IngesterError(f"Cannot read {path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IngesterError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise IngesterError(f"Top-level value in {path} is not a JSON object")

    permissions = data.get("permissions", {})
    if not isinstance(permissions, dict):
        raise IngesterError(f"'permissions' key in {path} is not a JSON object")

    rows: list[dict[str, Any]] = []
    for decision in ("allow", "deny", "ask"):
        entries = permissions.get(decision, [])
        if not isinstance(entries, list):
            raise IngesterError(
                f"'permissions.{decision}' in {path} is not a JSON array"
            )
        for idx, entry in enumerate(entries):
            if not isinstance(entry, str):
                raise IngesterError(
                    f"Malformed permission entry at {path} ({decision}[{idx}]): "
                    f"expected string, got {type(entry).__name__!r}"
                )
            source = f"{path} ({decision}[{idx}])"
            row = parse_entry(entry, source=source)
            row["decision"] = decision
            rows.append(row)

    return rows
