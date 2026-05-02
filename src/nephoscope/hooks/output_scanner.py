"""PostToolUse output scanner hook — redacts secrets in tool output.

Reads a Claude Code PostToolUse payload from stdin and emits one of:

- ``{}``                                                — passthrough (no
  scanning tool, no matches, or internal failure).
- ``{"hookSpecificOutput": {"hookEventName": "PostToolUse",
   "updatedToolOutput": "<redacted-text>"}}``           — at least one match.

Scanning tools (run the scanner): ``Bash``, ``Grep``, ``Read``.
All other tools fall through immediately.

The hook always exits 0 — domain rule: hooks never block the user's tool call.
Internal exceptions are surfaced on stderr per observability-hygiene rule, but
do not propagate.

After a successful redaction, the hook records one row per match in
``redaction_events`` for later stats. The DB write is fire-and-forget: any
failure is logged to stderr but never affects the hook's response — the
redacted output is the user-visible contract.
"""

from __future__ import annotations

import datetime as _dt
import importlib.resources as _pkg_resources
import json
import logging as _logging
import sys

# Top-level imports so tests can patch them via ``mock.patch.object``. The
# resilience test patches ``redact`` here and relies on it being resolved
# through the module global at call time. Same applies to ``load_patterns``.
from nephoscope.lib.scanner.patterns import load_patterns
from nephoscope.lib.scanner.redact import redact

_log = _logging.getLogger(__name__)

_PATTERNS: list | None = None


def _get_patterns() -> list:
    global _PATTERNS
    if _PATTERNS is None:
        _PATTERNS = load_patterns(
            _pkg_resources.files("nephoscope.lib.scanner").joinpath("output_scanner.yaml")
        )
    return _PATTERNS


_SCANNING_TOOLS = frozenset({"Bash", "Grep", "Read"})


def _now_iso() -> str:
    return (
        _dt.datetime.now(tz=_dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _record_redaction_events(
    pattern_names: list[str], tool_name: str, tool_use_id: str | None
) -> None:
    """Append one ``redaction_events`` row per match.

    Resolves ``session_id`` from ``tool_calls.tool_use_id``; on miss, writes
    ``NULL``. Wrapped in a broad ``except`` per the hook's exit-0 contract:
    a DB failure here must never affect the redaction response.
    """
    # Local imports keep the no-match fast path from paying the DB import cost.
    from nephoscope.lib.db import _open  # noqa: PLC0415

    try:
        conn = _open()
    except Exception as exc:  # noqa: BLE001
        _log.error("output-scanner db-open error: %s", exc)
        return

    try:
        session_id: int | None = None
        if tool_use_id:
            row = conn.execute(
                "SELECT session_id FROM tool_calls WHERE tool_use_id = ?;",
                (tool_use_id,),
            ).fetchone()
            if row is not None and row[0] is not None:
                session_id = int(row[0])

        ts = _now_iso()
        conn.executemany(
            "INSERT INTO redaction_events(session_id, pattern_name, tool_name, ts)"
            " VALUES (?, ?, ?, ?);",
            [(session_id, name, tool_name, ts) for name in pattern_names],
        )
    except Exception as exc:  # noqa: BLE001
        _log.error("output-scanner db-write error: %s", exc)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:  # NOSONAR S3516 - hook entry points must always exit 0 (domain rule)
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)

        tool_name = data.get("tool_name")
        if tool_name not in _SCANNING_TOOLS:
            print("{}")
            return 0

        patterns = _get_patterns()

        tool_output = data.get("tool_output", "")
        result = redact(tool_output, patterns)

        if result.matches:
            response = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "updatedToolOutput": result.text,
                }
            }
            print(json.dumps(response, ensure_ascii=False))
            # Best-effort ledger write. Any failure is swallowed by
            # _record_redaction_events itself so the redaction response
            # above remains the only user-visible artifact.
            try:
                _record_redaction_events(
                    [m.name for m in result.matches],
                    tool_name,
                    data.get("tool_use_id"),
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("output-scanner ledger error: %s", exc)
        else:
            print("{}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print("{}")
        _log.error("output-scanner error: %s", exc)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
