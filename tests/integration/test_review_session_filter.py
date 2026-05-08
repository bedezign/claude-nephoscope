"""Integration tests for nephoscope-review --session filter.

End-to-end exercise of the env-var + flag plumbing through to the
JSON output of ``nephoscope-review list``. Verifies that only the
candidates linked to the in-scope session are returned.

All DB access is via ``tmp_db``; env is via ``monkeypatch.setenv``.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from nephoscope.cli.review_cmd import main
from nephoscope.lib import db as lib_db_mod


def _now() -> str:
    return lib_db_mod._now()


def _seed_candidate(
    conn: sqlite3.Connection,
    verb: str,
    flags_json: str = "[]",
    observations: int = 5,
    distinct_sessions: int = 2,
) -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO permission_candidates"
        "(verb, subcommand, flags, observations, distinct_sessions,"
        " first_seen, last_seen)"
        " VALUES (?, NULL, ?, ?, ?, ?, ?)",
        (verb, flags_json, observations, distinct_sessions, now, now),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _link_candidate_session(
    conn: sqlite3.Connection, candidate_id: int, session_id: int
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO permission_candidate_sessions"
        "(candidate_id, session_id, last_seen) VALUES (?, ?, ?)",
        (candidate_id, session_id, _now()),
    )
    conn.commit()


@pytest.fixture
def seeded(tmp_db, tmp_path):
    """Seed two sessions and three candidates: A↔1, B↔2, C↔both. Return state."""
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    proj_id = lib_db_mod.upsert_project(tmp_db, str(proj_dir), _now())
    sess1_id = lib_db_mod.upsert_session(tmp_db, "uuid-sess-1", proj_id, _now())
    sess2_id = lib_db_mod.upsert_session(tmp_db, "uuid-sess-2", proj_id, _now())
    cand_a = _seed_candidate(tmp_db, "verb-a")
    cand_b = _seed_candidate(tmp_db, "verb-b")
    cand_c = _seed_candidate(tmp_db, "verb-c")
    _link_candidate_session(tmp_db, cand_a, sess1_id)
    _link_candidate_session(tmp_db, cand_b, sess2_id)
    _link_candidate_session(tmp_db, cand_c, sess1_id)
    _link_candidate_session(tmp_db, cand_c, sess2_id)
    tmp_db.commit()
    return {
        "proj_dir": proj_dir,
        "proj_id": proj_id,
        "sess1_id": sess1_id,
        "sess2_id": sess2_id,
        "cand_a": cand_a,
        "cand_b": cand_b,
        "cand_c": cand_c,
    }


def test_review_list_session_current(seeded, monkeypatch, tmp_path, capsys):
    """env=session1 + no flag → only session 1's candidates (A, C) — not B."""
    monkeypatch.chdir(seeded["proj_dir"])
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "uuid-sess-1")

    rc = main(["list"])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    verbs = {row["verb"] for row in payload}
    assert verbs == {"verb-a", "verb-c"}
    assert "verb-b" not in verbs


def test_review_list_session_all(seeded, monkeypatch, tmp_path, capsys):
    """env=session1 + flag=all → all candidates (A, B, C)."""
    monkeypatch.chdir(seeded["proj_dir"])
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "uuid-sess-1")

    rc = main(["list", "--session=all"])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    verbs = {row["verb"] for row in payload}
    assert verbs == {"verb-a", "verb-b", "verb-c"}


def test_review_list_no_env_no_flag(seeded, monkeypatch, tmp_path, capsys):
    """env unset + no flag → all candidates (existing behaviour preserved)."""
    monkeypatch.chdir(seeded["proj_dir"])
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    rc = main(["list"])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    verbs = {row["verb"] for row in payload}
    assert verbs == {"verb-a", "verb-b", "verb-c"}
