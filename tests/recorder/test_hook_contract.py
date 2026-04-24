"""Contract tests for the recorder against Claude Code's real hook payload schema.

Why a dedicated contract file:

The post-Phase-8.5 recorder rewrite invented payload keys (``tool``,
``session_uuid``, ``project_cwd``, ``fields.command``, etc.) that did not match
Claude Code's actual hook payload shape (``tool_name``, ``session_id``, ``cwd``,
``tool_input.command``). Every hook invocation early-returned at the
"missing core identifiers" guard and nothing was recorded. The existing
``test_recorder.py`` suite passed because it built fake payloads using the same
invented keys — tests validated the broken implementation against its own
made-up contract.

These tests pin the contract to Claude Code's canonical hook payload shape.
Running them against a recorder that reads non-canonical keys must fail.

References:
- https://docs.claude.com/en/docs/claude-code/hooks#pretooluse-input
- https://docs.claude.com/en/docs/claude-code/hooks#posttooluse-input
"""

from __future__ import annotations

import io
import json
from typing import Any
from unittest.mock import patch

import pytest

from nephoscope.lib.paths import canonicalize


@pytest.fixture
def recorder(tmp_db):
    """Import the recorder module under the tmp_db fixture's env."""
    from nephoscope.recorder import run as mod

    return mod


def canonical_pre_payload(**overrides: Any) -> dict[str, Any]:
    """Build a Claude Code PreToolUse payload with canonical keys.

    Mutating the returned dict or passing overrides is the intended way tests
    exercise variations. Any field Claude Code actually sends must appear here.
    """
    base: dict[str, Any] = {
        "session_id": "019673a0-1111-7000-8000-000000000001",
        "transcript_path": "/home/steve/.claude/projects/whatever/transcript.jsonl",
        "cwd": "/home/steve/data/clients/example/projects/foo",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hello", "description": "smoke"},
        "tool_use_id": "toolu_019673a000000001",
        "permission_mode": "default",
    }
    base.update(overrides)
    return base


def canonical_post_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": "019673a0-1111-7000-8000-000000000001",
        "transcript_path": "/home/steve/.claude/projects/whatever/transcript.jsonl",
        "cwd": "/home/steve/data/clients/example/projects/foo",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hello"},
        "tool_response": {"stdout": "hello\n", "stderr": "", "is_error": False},
        "tool_use_id": "toolu_019673a000000001",
    }
    base.update(overrides)
    return base


class TestCanonicalPayloadContract:
    """The recorder must read from Claude Code's real hook payload keys."""

    def test_pre_bash_inserts_row_with_command(self, tmp_db, recorder):
        recorder._handle("pre", canonical_pre_payload())
        row = tmp_db.execute(
            "SELECT tool_use_id, command FROM tool_calls WHERE tool_use_id=?;",
            ("toolu_019673a000000001",),
        ).fetchone()
        assert row is not None, (
            "recorder dropped a canonical Claude Code PreToolUse payload — "
            "hook contract regression"
        )
        assert row[0] == "toolu_019673a000000001"
        assert row[1] == "echo hello"

    def test_pre_resolves_tool_name_from_canonical_key(self, tmp_db, recorder):
        recorder._handle(
            "pre",
            canonical_pre_payload(
                tool_name="Read", tool_input={"file_path": "/a/b.py"}
            ),
        )
        tool = tmp_db.execute(
            "SELECT t.name FROM tool_calls tc "
            "JOIN tools t ON t.id = tc.tool_id "
            "WHERE tc.tool_use_id=?;",
            ("toolu_019673a000000001",),
        ).fetchone()
        assert tool is not None and tool[0] == "Read"

    def test_pre_resolves_cwd_to_project(self, tmp_db, recorder):
        recorder._handle(
            "pre", canonical_pre_payload(cwd="/home/steve/data/clients/foo/bar")
        )
        cwd = tmp_db.execute(
            "SELECT p.cwd FROM tool_calls tc "
            "JOIN projects p ON p.id = tc.project_id "
            "WHERE tc.tool_use_id=?;",
            ("toolu_019673a000000001",),
        ).fetchone()
        assert cwd is not None
        assert cwd[0] == "/home/steve/data/clients/foo/bar"

    def test_pre_resolves_session_id_to_session(self, tmp_db, recorder):
        recorder._handle(
            "pre",
            canonical_pre_payload(session_id="019673a0-ffff-7000-8000-000000000002"),
        )
        uuid = tmp_db.execute(
            "SELECT s.session_uuid FROM tool_calls tc "
            "JOIN sessions s ON s.id = tc.session_id "
            "WHERE tc.tool_use_id=?;",
            ("toolu_019673a000000001",),
        ).fetchone()
        assert uuid is not None
        assert uuid[0] == "019673a0-ffff-7000-8000-000000000002"

    def test_pre_stores_full_payload_as_extra(self, tmp_db, recorder):
        recorder._handle("pre", canonical_pre_payload())
        extra = tmp_db.execute(
            "SELECT value FROM tool_extras te "
            "JOIN tool_calls tc ON tc.id = te.tool_call_id "
            "WHERE tc.tool_use_id=? AND te.name='payload';",
            ("toolu_019673a000000001",),
        ).fetchone()
        assert extra is not None
        parsed = json.loads(extra[0])
        assert parsed["tool_name"] == "Bash"
        assert parsed["session_id"] == "019673a0-1111-7000-8000-000000000001"
        assert parsed["cwd"].endswith("/foo")

    def test_pre_sets_transcript_path_once(self, tmp_db, recorder, tmp_path):
        first = str(tmp_path / "a" / "transcript.jsonl")
        second = str(tmp_path / "b" / "transcript.jsonl")
        recorder._handle("pre", canonical_pre_payload(transcript_path=first))
        recorder._handle(
            "pre",
            canonical_pre_payload(
                transcript_path=second, tool_use_id="toolu_019673a000000002"
            ),
        )
        rows = tmp_db.execute(
            "SELECT session_uuid, transcript_path FROM sessions;"
        ).fetchall()
        assert len(rows) == 1
        # Stored form is canonicalized; tmp_path is already absolute and
        # symlink-free, so the literal input and canonicalize() agree on
        # any host.
        assert rows[0][1] == canonicalize(first), (
            "transcript_path was overwritten — set-once violated"
        )

    def test_post_updates_pre_row_on_is_error_false(self, tmp_db, recorder):
        recorder._handle("pre", canonical_pre_payload())
        recorder._handle("post", canonical_post_payload())
        row = tmp_db.execute(
            "SELECT tc.ok, cs.name, tc.completed_ts FROM tool_calls tc "
            "JOIN call_statuses cs ON cs.id = tc.status_id "
            "WHERE tc.tool_use_id=?;",
            ("toolu_019673a000000001",),
        ).fetchone()
        assert row is not None
        assert row[0] == 1, "ok should be 1 for is_error=false"
        assert row[1] == "ok"
        assert row[2] is not None

    def test_post_marks_err_on_is_error_true(self, tmp_db, recorder):
        recorder._handle("pre", canonical_pre_payload())
        recorder._handle(
            "post",
            canonical_post_payload(tool_response={"error": "boom", "is_error": True}),
        )
        row = tmp_db.execute(
            "SELECT tc.ok, cs.name FROM tool_calls tc "
            "JOIN call_statuses cs ON cs.id = tc.status_id "
            "WHERE tc.tool_use_id=?;",
            ("toolu_019673a000000001",),
        ).fetchone()
        assert row is not None
        assert row[0] == 0
        assert row[1] == "err"

    def test_post_orphan_without_pre_still_inserts(self, tmp_db, recorder):
        recorder._handle(
            "post", canonical_post_payload(tool_use_id="toolu_orphan_test")
        )
        row = tmp_db.execute(
            "SELECT tool_use_id, completed_ts, ok FROM tool_calls WHERE tool_use_id=?;",
            ("toolu_orphan_test",),
        ).fetchone()
        assert row is not None
        assert row[1] is not None
        assert row[0] == "toolu_orphan_test"

    def test_post_stores_response_extra(self, tmp_db, recorder):
        recorder._handle("pre", canonical_pre_payload())
        recorder._handle(
            "post",
            canonical_post_payload(tool_response={"stdout": "abc", "is_error": False}),
        )
        extra = tmp_db.execute(
            "SELECT value FROM tool_extras te "
            "JOIN tool_calls tc ON tc.id = te.tool_call_id "
            "WHERE tc.tool_use_id=? AND te.name='response';",
            ("toolu_019673a000000001",),
        ).fetchone()
        assert extra is not None
        assert "abc" in extra[0]


class TestToolInputFlattening:
    """Per-tool field extraction from ``tool_input``."""

    def test_bash_command_from_tool_input(self, tmp_db, recorder):
        recorder._handle(
            "pre",
            canonical_pre_payload(
                tool_name="Bash",
                tool_input={"command": "ls -la /tmp", "description": "peek"},
            ),
        )
        row = tmp_db.execute(
            "SELECT command, description FROM tool_calls WHERE tool_use_id=?;",
            ("toolu_019673a000000001",),
        ).fetchone()
        assert row == ("ls -la /tmp", "peek")

    def test_read_file_path_from_tool_input(self, tmp_db, recorder):
        recorder._handle(
            "pre",
            canonical_pre_payload(
                tool_name="Read", tool_input={"file_path": "/path/to/file.py"}
            ),
        )
        path = tmp_db.execute(
            "SELECT fp.path FROM tool_calls tc "
            "JOIN file_paths fp ON fp.id = tc.file_path_id "
            "WHERE tc.tool_use_id=?;",
            ("toolu_019673a000000001",),
        ).fetchone()
        assert path == ("/path/to/file.py",)

    def test_write_file_path_from_tool_input(self, tmp_db, recorder):
        recorder._handle(
            "pre",
            canonical_pre_payload(
                tool_name="Write",
                tool_input={"file_path": "/tmp/out.txt", "content": "..."},
            ),
        )
        path = tmp_db.execute(
            "SELECT fp.path FROM tool_calls tc "
            "JOIN file_paths fp ON fp.id = tc.file_path_id "
            "WHERE tc.tool_use_id=?;",
            ("toolu_019673a000000001",),
        ).fetchone()
        assert path == ("/tmp/out.txt",)

    def test_grep_pattern_from_tool_input(self, tmp_db, recorder):
        recorder._handle(
            "pre",
            canonical_pre_payload(
                tool_name="Grep", tool_input={"pattern": "TODO.*fix"}
            ),
        )
        pattern = tmp_db.execute(
            "SELECT pattern FROM tool_calls WHERE tool_use_id=?;",
            ("toolu_019673a000000001",),
        ).fetchone()
        assert pattern == ("TODO.*fix",)

    def test_task_subagent_type_from_tool_input(self, tmp_db, recorder):
        recorder._handle(
            "pre",
            canonical_pre_payload(
                tool_name="Task",
                tool_input={
                    "subagent_type": "code-reviewer",
                    "description": "review last change",
                },
            ),
        )
        sub = tmp_db.execute(
            "SELECT st.name FROM tool_calls tc "
            "JOIN subagent_types st ON st.id = tc.subagent_type_id "
            "WHERE tc.tool_use_id=?;",
            ("toolu_019673a000000001",),
        ).fetchone()
        assert sub == ("code-reviewer",)


class TestDegradedPayloads:
    """The recorder must not crash or silently drop on partial payloads."""

    def test_missing_tool_use_id_gets_synthetic_fallback(self, tmp_db, recorder):
        payload = canonical_pre_payload()
        del payload["tool_use_id"]
        recorder._handle("pre", payload)
        rows = tmp_db.execute("SELECT tool_use_id FROM tool_calls;").fetchall()
        assert len(rows) == 1
        assert rows[0][0].startswith("synthetic::"), (
            "synthetic tool_use_id fallback is missing — data would be lost "
            "for hook payloads without a tool_use_id"
        )

    def test_main_silently_exits_on_malformed_json(self, recorder):
        """main() swallows bad stdin so the user's tool call never breaks."""
        with (
            patch("sys.stdin", io.StringIO("{not valid json")),
            patch("sys.argv", ["run.py", "pre"]),
        ):
            recorder.main()  # no exception

    def test_main_silently_exits_on_empty_stdin(self, recorder):
        with patch("sys.stdin", io.StringIO("")), patch("sys.argv", ["run.py", "pre"]):
            recorder.main()


class TestEndToEndMain:
    """Drive the recorder through ``main()`` the same way the hook invokes it."""

    def test_main_pre_inserts_row(self, tmp_db, recorder):
        payload = canonical_pre_payload(tool_use_id="toolu_e2e_pre_001")
        with (
            patch("sys.stdin", io.StringIO(json.dumps(payload))),
            patch("sys.argv", ["run.py", "pre"]),
        ):
            recorder.main()
        row = tmp_db.execute(
            "SELECT command FROM tool_calls WHERE tool_use_id=?;",
            ("toolu_e2e_pre_001",),
        ).fetchone()
        assert row is not None, (
            "main() dropped a canonical hook payload — the integration path is broken"
        )
        assert row[0] == "echo hello"

    def test_main_pre_then_post_completes_row(self, tmp_db, recorder):
        pre = canonical_pre_payload(tool_use_id="toolu_e2e_lifecycle")
        post = canonical_post_payload(tool_use_id="toolu_e2e_lifecycle")

        with (
            patch("sys.stdin", io.StringIO(json.dumps(pre))),
            patch("sys.argv", ["run.py", "pre"]),
        ):
            recorder.main()
        with (
            patch("sys.stdin", io.StringIO(json.dumps(post))),
            patch("sys.argv", ["run.py", "post"]),
        ):
            recorder.main()

        row = tmp_db.execute(
            "SELECT tc.ok, tc.completed_ts, cs.name FROM tool_calls tc "
            "JOIN call_statuses cs ON cs.id = tc.status_id "
            "WHERE tool_use_id=?;",
            ("toolu_e2e_lifecycle",),
        ).fetchone()
        assert row is not None
        assert row[0] == 1
        assert row[1] is not None
        assert row[2] == "ok"
