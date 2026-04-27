"""Summarize unprocessed ``tool_calls`` rows for the observer agent.

Queries via ``v_tool_calls`` so the summariser stays decoupled from the
FK lookup layout. The output file is a plain text report; a downstream
Haiku observer reads it and writes instinct ``.md`` files into the
configured instinct directory.

Subcommands:
    write   --output PATH    write summary; emit {"rows": N, "max_id": M} JSON
    commit  --max-id N       advance the observer cursor to N

Return codes:
    0  success
    1  error (path, db)
    2  nothing new to analyze (rows < --min-rows, default 10)

Cursor name is ``instinct-summarizer`` in ``consumer_cursors`` — distinct
from the permission-learner's cursor so the two consumers advance
independently.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from nephoscope.lib.db import _open

CONSUMER = "instinct-summarizer"
MAX_SEQUENCES = 10
MAX_ERRORS = 10
SEQ_LEN = 3
_UNKNOWN = "(unknown)"


def _now() -> str:
    return (
        _dt.datetime.now(tz=_dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _open_row_factory() -> sqlite3.Connection:
    conn = _open()
    conn.row_factory = sqlite3.Row
    return conn


def _cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT last_processed_id FROM consumer_cursors WHERE consumer = ?;",
        (CONSUMER,),
    ).fetchone()
    return int(row["last_processed_id"]) if row else 0


def _advance(conn: sqlite3.Connection, max_id: int) -> None:
    conn.execute(
        """
        INSERT INTO consumer_cursors(consumer, last_processed_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(consumer) DO UPDATE SET
          last_processed_id = excluded.last_processed_id,
          updated_at = excluded.updated_at;
        """,
        (CONSUMER, max_id, _now()),
    )


def _fetch(conn: sqlite3.Connection, since_id: int) -> list[sqlite3.Row]:
    """Read new calls via v_tool_calls so FK resolution is handled in SQL."""
    return conn.execute(
        """
        SELECT id, ts, session_uuid, tool, ok,
               subagent_type, command, file_path, pattern, description,
               args_json, project_name, project_cwd
          FROM v_tool_calls
         WHERE id > ?
         ORDER BY id ASC;
        """,
        (since_id,),
    ).fetchall()


def _format_row_snippet(row: sqlite3.Row) -> str:
    tool = row["tool"]
    if tool == "Bash" and row["command"]:
        return f"Bash: {row['command']}"
    if tool in ("Task", "Agent"):
        parts: list[str] = []
        if row["subagent_type"]:
            parts.append(row["subagent_type"])
        if row["description"]:
            parts.append(row["description"])
        return f"{tool}: {' — '.join(parts) or '(no details)'}"
    if (
        tool in ("Edit", "Write", "Read", "MultiEdit", "NotebookEdit")
        and row["file_path"]
    ):
        return f"{tool}: {row['file_path']}"
    if tool in ("Grep", "Glob") and row["pattern"]:
        return f"{tool}: {row['pattern']}"
    return tool or "(unknown tool)"


def _count_rows(
    rows: list[sqlite3.Row],
) -> tuple[
    Counter,
    Counter,
    Counter,
    list[sqlite3.Row],
]:
    """Tally tool/project/subagent counts and collect error rows."""
    tool_counts: Counter[str] = Counter()
    project_counts: Counter[str] = Counter()
    subagent_counts: Counter[str] = Counter()
    errors: list[sqlite3.Row] = []
    for row in rows:
        tool_counts[row["tool"] or _UNKNOWN] += 1
        project_counts[row["project_name"] or _UNKNOWN] += 1
        if row["subagent_type"]:
            subagent_counts[row["subagent_type"]] += 1
        if row["ok"] == 0:
            errors.append(row)
    return tool_counts, project_counts, subagent_counts, errors


def _build_seq_counts(rows: list[sqlite3.Row]) -> Counter:
    """Count repeated N-tool sequences grouped by session."""
    session_tools: dict[str, list[str]] = {}
    for row in rows:
        key = row["session_uuid"] or "?"
        session_tools.setdefault(key, []).append(row["tool"] or _UNKNOWN)

    seq_counts: Counter[tuple[str, ...]] = Counter()
    for tools in session_tools.values():
        if len(tools) < SEQ_LEN:
            continue
        for i in range(len(tools) - SEQ_LEN + 1):
            seq_counts[tuple(tools[i : i + SEQ_LEN])] += 1
    return seq_counts


def _subagent_section(subagent_counts: Counter) -> list[str]:
    """Return the subagents section lines, or empty list if none."""
    if not subagent_counts:
        return []
    lines = ["## Subagents used"]
    for name, n in subagent_counts.most_common():
        lines.append(f"  {name:<30} {n}")
    lines.append("")
    return lines


def _sequences_section(seq_counts: Counter) -> list[str]:
    """Return the common sequences section lines, or empty list if none repeated."""
    repeated = [(seq, n) for seq, n in seq_counts.most_common(MAX_SEQUENCES) if n >= 2]
    if not repeated:
        return []
    lines = [f"## Common {SEQ_LEN}-tool sequences (repeated within a session)"]
    for seq, n in repeated:
        lines.append(f"  {' → '.join(seq)}  ×{n}")
    lines.append("")
    return lines


def _errors_section(errors: list[sqlite3.Row]) -> list[str]:
    """Return the errors section lines, or empty list if no errors."""
    if not errors:
        return []
    lines = [f"## Recent errors ({len(errors)} total, showing up to {MAX_ERRORS})"]
    for row in errors[-MAX_ERRORS:]:
        lines.append(f"  [{row['ts']}] {_format_row_snippet(row)}")
    lines.append("")
    return lines


def _render_summary_lines(
    rows: list[sqlite3.Row],
    tool_counts: Counter,
    project_counts: Counter,
    subagent_counts: Counter,
    seq_counts: Counter,
    errors: list[sqlite3.Row],
) -> list[str]:
    """Assemble the text lines of the summary report."""
    start_ts = rows[0]["ts"]
    end_ts = rows[-1]["ts"]

    lines: list[str] = [
        f"Observation summary ({len(rows)} tool calls, {start_ts} → {end_ts})",
        "",
        "## Tool frequency",
    ]
    for tool, n in tool_counts.most_common():
        lines.append(f"  {tool:<14} {n}")
    lines.append("")

    lines.append("## Per-project activity")
    for name, n in project_counts.most_common():
        lines.append(f"  {name:<30} {n}")
    lines.append("")

    lines.extend(_subagent_section(subagent_counts))
    lines.extend(_sequences_section(seq_counts))
    lines.extend(_errors_section(errors))

    lines.append("## Sample recent calls (last 20)")
    for row in rows[-20:]:
        status = "" if row["ok"] is None else ("ok" if row["ok"] else "ERR")
        lines.append(f"  [{row['ts']}] {status:<3} {_format_row_snippet(row)}")
    lines.append("")

    return lines


def _summarize(rows: list[sqlite3.Row]) -> str:
    """Render a summary. Format mirrors CL-v2 so the observer agent is stable."""
    if not rows:
        return "No activity.\n"

    tool_counts, project_counts, subagent_counts, errors = _count_rows(rows)
    seq_counts = _build_seq_counts(rows)
    lines = _render_summary_lines(
        rows, tool_counts, project_counts, subagent_counts, seq_counts, errors
    )
    return "\n".join(lines)


def cmd_write(args: argparse.Namespace) -> int:
    conn = _open_row_factory()
    try:
        since = _cursor(conn)
        rows = _fetch(conn, since)
        if len(rows) < args.min_rows:
            return 2
        summary = _summarize(rows)
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(summary, encoding="utf-8")
        max_id = int(rows[-1]["id"])
        print(json.dumps({"rows": len(rows), "max_id": max_id, "output": str(out)}))
        return 0
    finally:
        conn.close()


def cmd_commit(args: argparse.Namespace) -> int:
    conn = _open_row_factory()
    try:
        _advance(conn, args.max_id)
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_write = sub.add_parser("write", help="write summary for unprocessed rows")
    p_write.add_argument("--output", required=True)
    p_write.add_argument("--min-rows", type=int, default=10)
    p_write.set_defaults(func=cmd_write)

    p_commit = sub.add_parser("commit", help="advance observer cursor")
    p_commit.add_argument("--max-id", type=int, required=True)
    p_commit.set_defaults(func=cmd_commit)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
