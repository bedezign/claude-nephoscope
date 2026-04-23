"""Structured DB row → canonical permission string.

Each tool class has its own renderer.  The public entry point is
:func:`serialize`, which dispatches via :mod:`lib.mirror.tool_class`.

Canonical string grammar
------------------------
Bash
    ``Bash(<verb>)``              — verb only, no subcommand, no flags pattern
    ``Bash(<verb> <sub>)``        — literal verb + subcommand
    ``Bash(<verb> *)``            — flags wildcard, no subcommand → "any <verb>"
    ``Bash(<verb> <sub> *)``      — literal subcommand + flags wildcard

File (Read / Edit / Write / MultiEdit / NotebookEdit)
    ``<Verb>``                    — bare; no path constraint (path_spec None or "")
    ``<Verb>(//<abs_path>)``      — double-slash prefix before the absolute path

Flat (Grep / Glob / WebSearch)
    ``<Verb>``                    — always bare; no argument encoding

MCP
    ``mcp__<ns>__<tool>``         — fully-qualified literal (stored as verb)
    ``mcp__<ns>__*``              — namespace wildcard (stored as verb)

Orchestration (Agent, TaskCreate, …)
    Not serialised.  :func:`serialize` returns ``None`` for these rows.
    Orchestration tools are default-allow; they have no JSON mirror entry.

Input row
---------
The ``row`` argument is any mapping with the following keys (all from
``rule_shapes``):

    verb       — str, non-empty
    subcommand — str | None
    flags      — str; a JSON array like ``'["-q"]'`` or the sentinel ``"*"``
    path_spec  — str | None; ``None`` or ``""`` means "no path constraint"

:func:`serialize` raises :class:`ValueError` for malformed inputs
(missing/empty verb, unsupported path_spec type, etc.).  It never silently
normalises bad data.
"""

from __future__ import annotations

from typing import Any, Mapping

from nephoscope.lib.mirror.tool_class import classify  # type: ignore[import-untyped]

# Sentinel used in rule_shapes.flags to indicate the flags-wildcard variant.
_FLAGS_WILDCARD = "*"


# ---------------------------------------------------------------------------
# Per-class renderers
# ---------------------------------------------------------------------------


def _render_bash(row: Mapping[str, Any]) -> str:
    """Render a Bash rule row to its canonical string.

    Flags-wildcard (``flags == "*"``) is rendered as a trailing `` *``.
    Specific flag sets (JSON arrays) are not encoded — only the wildcard
    sentinel is meaningful for mirror serialisation.
    """
    verb: str = row["verb"]
    sub: str | None = row.get("subcommand")
    flags: str = row.get("flags") or "[]"

    is_wildcard = flags == _FLAGS_WILDCARD

    if sub is not None:
        if is_wildcard:
            return f"Bash({verb} {sub} *)"
        return f"Bash({verb} {sub})"
    # No subcommand.
    if is_wildcard:
        return f"Bash({verb} *)"
    return f"Bash({verb})"


def _render_file(row: Mapping[str, Any]) -> str:
    """Render a file-tool rule row to its canonical string.

    Path encoding uses a double-slash prefix: ``Read(//abs/path/**)``.
    A bare tool (no path_spec, or empty path_spec) serialises to the
    verb alone.

    Two storage conventions are accepted for ``path_spec``:

    - **Ingester-produced** (``//`` prefix already present): stored as
      ``"//var/log/**"``.  The serialiser wraps it directly:
      ``Read(//var/log/**)``.
    - **DB-native** (single ``/`` absolute path): stored as
      ``"/var/log/**"``.  The serialiser strips one ``/`` and
      adds ``//``: ``Read(//var/log/**)``.

    Both produce identical canonical output.

    Raises
    ------
    ValueError
        If path_spec is non-empty but does not start with ``/``.
        Unresolved ``$VAR`` tokens must be expanded before calling this
        function.
    """
    verb: str = row["verb"]
    path_spec: str | None = row.get("path_spec") or None

    if not path_spec:
        return verb

    if path_spec.startswith("//"):
        # Ingester convention: path already has the canonical // prefix.
        return f"{verb}({path_spec})"

    if path_spec.startswith("/"):
        # DB-native convention: single-slash absolute path → add // prefix
        # by replacing the leading / with //.
        return f"{verb}(//{path_spec[1:]})"

    raise ValueError(
        f"file tool path_spec must be an absolute path (starting with / or //), "
        f"got {path_spec!r} for verb {verb!r}.  Resolve $VAR tokens before serialising."
    )


def _render_flat(_row: Mapping[str, Any]) -> str:
    """Render a flat tool row to its canonical string (bare verb only)."""
    return _row["verb"]


def _render_mcp(row: Mapping[str, Any]) -> str:
    """Render an MCP tool row to its canonical string.

    MCP tools store their fully-qualified name (``mcp__ns__tool``) or
    wildcard (``mcp__ns__*``) directly in ``verb``; the canonical string
    is the verb itself.
    """
    return row["verb"]


def _render_orchestration(_row: Mapping[str, Any]) -> None:
    """Orchestration tools are never serialised — they are default-allow.

    Always returns ``None``.
    """
    return None


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_RENDERERS = {
    "bash": _render_bash,
    "file": _render_file,
    "flat": _render_flat,
    "mcp": _render_mcp,
    "orchestration": _render_orchestration,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def serialize(row: Mapping[str, Any]) -> str | None:
    """Serialize a structured rule row to its canonical permission string.

    Returns ``None`` for orchestration-class tools (default-allow; no mirror
    entry needed).

    Parameters
    ----------
    row:
        Mapping with at minimum a ``"verb"`` key.  Recognised keys:
        ``verb``, ``subcommand``, ``flags``, ``path_spec``.

    Raises
    ------
    ValueError
        If ``verb`` is missing, empty, or not a string; or if the row
        contains data that cannot be rendered (e.g. a relative path_spec
        for a file tool).
    """
    verb = row.get("verb")
    if not verb or not isinstance(verb, str):
        raise ValueError(f"row['verb'] must be a non-empty string, got {verb!r}")

    tool_class = classify(verb)
    renderer = _RENDERERS[tool_class]
    return renderer(row)
