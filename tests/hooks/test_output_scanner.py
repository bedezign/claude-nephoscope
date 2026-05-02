"""Tests for the PostToolUse output scanner hook.

The hook reads a PostToolUse payload from stdin and emits one of:

- ``{}`` when no redaction is needed (passthrough), or
- ``{"hookSpecificOutput": {"hookEventName": "PostToolUse",
   "updatedToolOutput": "<redacted-text>"}}`` when at least one secret was
  matched in the output.

Scanning tools (run the scanner): Bash, Grep, Read.
Non-scanning tools (passthrough immediately): Edit, Write, LS, Glob, MultiEdit.

The hook always exits 0 — even when an internal step raises.

Note on the helper: ``_run`` normalizes empty stdout to ``{}``. This means
passthrough cases assert against an empty dict regardless of whether the hook
emitted nothing or emitted a literal ``{}``. The redaction tests therefore
also assert on the presence of nested keys, not merely on the dict being
non-empty, to keep proper RED behaviour against the stub.
"""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

import nephoscope.hooks.output_scanner as scanner_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_payload(
    tool_name: str,
    tool_output: str,
    *,
    tool_input: dict[str, Any] | None = None,
    tool_use_id: str = "toolu_test_001",
    session_id: str = "sess_001",
    cwd: str = "/home/user/project",
) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "tool_input": tool_input or {},
        "tool_output": tool_output,
        "tool_use_id": tool_use_id,
        "session_id": session_id,
        "cwd": cwd,
    }


def _run(payload: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
    """Invoke scanner_mod.main() with the given payload.

    Returns:
        A tuple ``(exit_code, parsed_stdout_json)``. Empty stdout is
        normalized to an empty dict so passthrough cases compare cleanly.
    """
    raw = json.dumps(payload) if payload is not None else ""
    captured_out = io.StringIO()

    with (
        mock.patch("sys.stdin", io.StringIO(raw)),
        mock.patch("sys.stdout", captured_out),
    ):
        exit_code = scanner_mod.main()

    text = captured_out.getvalue().strip()
    parsed: dict[str, Any] = {} if not text else json.loads(text)
    return exit_code, parsed


# ---------------------------------------------------------------------------
# Redaction tests
# ---------------------------------------------------------------------------


class TestRedaction:
    """Scanner-tool outputs containing matched secrets are redacted."""

    def test_bash_output_with_anthropic_key_is_redacted(self):
        payload = _make_payload(
            tool_name="Bash",
            tool_output="Authorization: Bearer sk-ant-api03-abc",
        )
        exit_code, parsed = _run(payload)

        assert exit_code == 0
        hook_output = parsed.get("hookSpecificOutput", {})
        # Contract: hookEventName is the exact string Claude Code reads.
        # If this string drifts, Claude Code silently drops the response.
        assert hook_output.get("hookEventName") == "PostToolUse"

        updated = hook_output.get("updatedToolOutput", "")
        assert "[REDACTED:anthropic_api_key]" in updated
        # The literal secret prefix must not survive in the response.
        assert "sk-ant-" not in updated

    def test_grep_output_with_aws_key_is_redacted(self):
        payload = _make_payload(
            tool_name="Grep",
            tool_output="config.py:  AKIAIOSFODNN7EXAMPLE",
        )
        exit_code, parsed = _run(payload)

        assert exit_code == 0
        hook_output = parsed.get("hookSpecificOutput", {})
        assert hook_output.get("hookEventName") == "PostToolUse"

        updated = hook_output.get("updatedToolOutput", "")
        assert "[REDACTED:aws_access_key_id]" in updated
        assert "AKIAIOSFODNN7EXAMPLE" not in updated


# ---------------------------------------------------------------------------
# Passthrough tests
# ---------------------------------------------------------------------------


class TestPassthrough:
    """Non-scanning tools and clean output produce an empty response."""

    def test_edit_tool_passes_through_without_scanning(self):
        payload = _make_payload(
            tool_name="Edit",
            tool_output="anything could be in here including sk-ant-xxx",
        )
        exit_code, parsed = _run(payload)

        assert exit_code == 0
        assert parsed == {}

    def test_bash_with_no_credential_passes_through(self):
        payload = _make_payload(
            tool_name="Bash",
            tool_output="build succeeded",
        )
        exit_code, parsed = _run(payload)

        assert exit_code == 0
        assert parsed == {}


# ---------------------------------------------------------------------------
# Resilience tests
# ---------------------------------------------------------------------------


class TestResilience:
    """The hook never blocks the user's tool call, even on internal failure."""

    def test_scanner_exception_falls_through_with_logging(self, caplog):
        payload = _make_payload(
            tool_name="Bash",
            tool_output="Authorization: Bearer sk-ant-api03-abc",
        )

        import logging

        with (
            mock.patch.object(scanner_mod, "redact", side_effect=RuntimeError("boom")),
            caplog.at_level(logging.ERROR, logger="nephoscope.hooks.output_scanner"),
        ):
            exit_code, parsed = _run(payload)

        assert exit_code == 0
        assert parsed == {}

        # The hook must surface failure via logging (observability-hygiene
        # rule); silent swallow with no signal is the regression we guard
        # against.
        assert any("output-scanner error" in r.message for r in caplog.records)

    def test_malformed_json_input_falls_through(self):
        """Non-JSON stdin must produce exit 0 and an empty response."""
        captured_out = io.StringIO()
        with (
            mock.patch("sys.stdin", io.StringIO("not-json{")),
            mock.patch("sys.stdout", captured_out),
        ):
            exit_code = scanner_mod.main()

        assert exit_code == 0
        text = captured_out.getvalue().strip()
        assert text == "{}"

    def test_empty_stdin_falls_through(self):
        """Empty stdin must produce exit 0 and an empty response."""
        captured_out = io.StringIO()
        with (
            mock.patch("sys.stdin", io.StringIO("")),
            mock.patch("sys.stdout", captured_out),
        ):
            exit_code = scanner_mod.main()

        assert exit_code == 0
        text = captured_out.getvalue().strip()
        assert text == "{}"


# ---------------------------------------------------------------------------
# Exit-code invariant
# ---------------------------------------------------------------------------


class TestExitCode:
    """main() always returns 0 — domain rule: hooks never block tool calls."""

    @pytest.mark.parametrize(
        "tool_name,tool_output",
        [
            ("Bash", "Authorization: Bearer sk-ant-api03-abc"),  # redact path
            ("Bash", "build succeeded"),  # passthrough
        ],
    )
    def test_main_returns_zero(self, tool_name, tool_output):
        payload = _make_payload(tool_name=tool_name, tool_output=tool_output)
        exit_code, _ = _run(payload)
        assert exit_code == 0


# ---------------------------------------------------------------------------
# DB-write tests
# ---------------------------------------------------------------------------


def _seed_tool_call(
    db_path: Path, *, tool_use_id: str, session_uuid: str = "sess_001"
) -> int:
    """Seed a sessions row + tool_calls row with the given tool_use_id.

    Returns the integer session_id so callers can assert against it.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO sessions(session_uuid, project_id, started_at, last_activity)"
            " VALUES (?, NULL, '2026-05-02T00:00:00Z', '2026-05-02T00:00:00Z');",
            (session_uuid,),
        )
        sess_row = conn.execute(
            "SELECT id FROM sessions WHERE session_uuid = ?;", (session_uuid,)
        ).fetchone()
        session_id = int(sess_row[0])
        conn.execute(
            "INSERT INTO tool_calls(ts, session_id, tool_use_id)"
            " VALUES ('2026-05-02T00:00:01Z', ?, ?);",
            (session_id, tool_use_id),
        )
        conn.commit()
        return session_id
    finally:
        conn.close()


class TestRedactionEventsDbWrite:
    """After a successful redaction, the hook writes a redaction_events row per match."""

    def test_after_redaction_row_inserted_with_matching_session(
        self, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        # Trigger schema creation by calling _open() once and closing.
        from nephoscope.lib.db import _open

        conn = _open()
        conn.close()

        session_id = _seed_tool_call(db_path, tool_use_id="toolu_test_redact")

        payload = _make_payload(
            tool_name="Bash",
            tool_output="Authorization: Bearer sk-ant-api03-abc",
            tool_use_id="toolu_test_redact",
        )
        exit_code, parsed = _run(payload)

        assert exit_code == 0
        # Redaction must still happen — hook output contract.
        assert "[REDACTED:anthropic_api_key]" in parsed.get(
            "hookSpecificOutput", {}
        ).get("updatedToolOutput", "")

        # DB should have one redaction_events row.
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT session_id, pattern_name, tool_name, ts"
                " FROM redaction_events ORDER BY id;"
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 1
        assert rows[0][0] == session_id
        assert rows[0][1] == "anthropic_api_key"
        assert rows[0][2] == "Bash"
        assert rows[0][3]  # ts is populated

    def test_multiple_matches_produce_multiple_rows(self, tmp_path, monkeypatch):
        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        from nephoscope.lib.db import _open

        conn = _open()
        conn.close()

        _seed_tool_call(db_path, tool_use_id="toolu_test_multi")

        payload = _make_payload(
            tool_name="Grep",
            tool_output=("one: AKIAIOSFODNN7EXAMPLE\ntwo: AKIAJANOTHERKEYEXAMPL\n"),
            tool_use_id="toolu_test_multi",
        )
        exit_code, _ = _run(payload)
        assert exit_code == 0

        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM redaction_events"
                " WHERE pattern_name = 'aws_access_key_id';"
            ).fetchone()[0]
        finally:
            conn.close()

        assert count == 2

    def test_unknown_tool_use_id_records_null_session(self, tmp_path, monkeypatch):
        """Hook ordering may put us before the recorder — tool_use_id may be unknown.

        The redaction event must still be recorded, with session_id = NULL.
        """
        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        from nephoscope.lib.db import _open

        conn = _open()
        conn.close()

        # No seeding — tool_use_id will not match any tool_calls row.

        payload = _make_payload(
            tool_name="Bash",
            tool_output="Authorization: Bearer sk-ant-api03-abc",
            tool_use_id="toolu_unknown",
        )
        exit_code, _ = _run(payload)
        assert exit_code == 0

        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT session_id, pattern_name FROM redaction_events;"
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 1
        assert rows[0][0] is None
        assert rows[0][1] == "anthropic_api_key"

    def test_no_match_path_does_not_touch_db(self, tmp_path, monkeypatch):
        """The lean fast path: when nothing matches, the hook must not open the DB."""
        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        # Patch _open to fail loudly if it's ever called on the no-match path.
        # If the hook calls _open here, the test fails with the boom message.
        with mock.patch(
            "nephoscope.lib.db._open",
            side_effect=AssertionError("DB should not be opened on no-match path"),
        ):
            payload = _make_payload(
                tool_name="Bash",
                tool_output="build succeeded",
            )
            exit_code, parsed = _run(payload)

        assert exit_code == 0
        assert parsed == {}
        assert not db_path.exists()

    def test_db_failure_does_not_drop_redacted_output(
        self, tmp_path, monkeypatch, capsys
    ):
        """A DB error must never prevent the redaction from reaching stdout.

        The user-visible contract is the hook's ``updatedToolOutput`` — the
        redaction-events ledger is best-effort and must not interfere.
        """
        db_path = tmp_path / "observations.db"
        monkeypatch.setenv("OBSERVABILITY_DB", str(db_path))

        # Patch _open to raise — simulates a disk-full / locked-DB scenario
        # during the *ledger* write path. The hook must still emit a
        # full hookSpecificOutput response.
        with mock.patch(
            "nephoscope.lib.db._open",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            payload = _make_payload(
                tool_name="Bash",
                tool_output="Authorization: Bearer sk-ant-api03-abc",
                tool_use_id="toolu_dbfail",
            )
            exit_code, parsed = _run(payload)

        assert exit_code == 0
        hook_output = parsed.get("hookSpecificOutput", {})
        assert hook_output.get("hookEventName") == "PostToolUse"
        updated = hook_output.get("updatedToolOutput", "")
        assert "[REDACTED:anthropic_api_key]" in updated


# ---------------------------------------------------------------------------
# _now_iso() helper
# ---------------------------------------------------------------------------


class TestNowIso:
    """_now_iso() returns a well-formed UTC ISO-8601 timestamp with milliseconds."""

    def test_returns_string_ending_in_z(self):
        result = scanner_mod._now_iso()
        assert isinstance(result, str)
        assert result.endswith("Z")

    def test_parses_as_valid_utc_datetime(self):
        import datetime

        result = scanner_mod._now_iso()
        dt = datetime.datetime.fromisoformat(result.replace("Z", "+00:00"))
        assert dt.tzinfo is not None

    def test_contains_millisecond_precision(self):
        """The timestamp includes a '.' before the trailing Z (millisecond part)."""
        result = scanner_mod._now_iso()
        time_part = result.rstrip("Z")
        assert "." in time_part
