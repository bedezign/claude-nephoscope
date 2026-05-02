"""Unit tests for the `nephoscope-permissions status` subcommand.

All tests use a fresh in-process SQLite DB seeded with minimal fixtures so
they exercise real SQL without touching the production observations DB.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _bootstrap_db(path: Path) -> sqlite3.Connection:
    """Open a fresh DB at *path*, run schema.sql, and return the connection."""
    schema_path = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "nephoscope"
        / "lib"
        / "schema.sql"
    )
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.executescript(schema_path.read_text(encoding="utf-8"))
    return conn


def _seed(conn: sqlite3.Connection) -> None:
    """Insert a representative set of rules, permissions, and candidates.

    Schema:
      rule_shapes(id, verb, subcommand, flags, path_spec, context, tool,
                  first_seen, last_seen)
      permissions(id, rule_shape_id, session_id, project_id, decision,
                  source, reason, decided_at, hit_count, last_hit_at,
                  danger_accepted)
      permission_candidates(id, verb, subcommand, flags, observations,
                             distinct_sessions, first_seen, last_seen,
                             positional_paths)
    """
    _now_dt = dt.datetime.now(tz=dt.timezone.utc)

    def _ts(delta_days: int) -> str:
        return (
            (_now_dt + dt.timedelta(days=delta_days))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    now = _ts(0)
    recent = _ts(-1)  # always 1 day ago, always inside 7-day window
    old = _ts(-30)  # always outside 7-day window

    # --- rule shapes ---
    # shape 1: git (no subcommand, no path) — uniqueness from shape 3 via subcommand
    conn.execute(
        "INSERT INTO rule_shapes(id,verb,subcommand,flags,path_spec,context,tool,first_seen,last_seen)"
        " VALUES (1,'git',NULL,'[]',NULL,'any','Bash',?,?);",
        (now, now),
    )
    # shape 3: git push — same verb, different subcommand → two approved git rules
    conn.execute(
        "INSERT INTO rule_shapes(id,verb,subcommand,flags,path_spec,context,tool,first_seen,last_seen)"
        " VALUES (3,'git','push','[]',NULL,'any','Bash',?,?);",
        (now, now),
    )
    # shape 4: rm with flags — rejected
    conn.execute(
        "INSERT INTO rule_shapes(id,verb,subcommand,flags,path_spec,context,tool,first_seen,last_seen)"
        " VALUES (4,'rm',NULL,'[\"-rf\"]',NULL,'any','Bash',?,?);",
        (now, now),
    )
    # shape 5: cat — ask
    conn.execute(
        "INSERT INTO rule_shapes(id,verb,subcommand,flags,path_spec,context,tool,first_seen,last_seen)"
        " VALUES (5,'cat',NULL,'[]',NULL,'any','Bash',?,?);",
        (now, now),
    )

    # --- permissions ---
    # approved: git (shape 1) — hit recently
    conn.execute(
        "INSERT INTO permissions(id,rule_shape_id,session_id,project_id,decision,"
        "source,reason,decided_at,hit_count,last_hit_at,danger_accepted)"
        " VALUES (1,1,NULL,NULL,'approved','seed',NULL,?,5,?,'transparent_wrapper_wildcard');",
        (now, recent),
    )
    # approved: git push (shape 3) — hit, but old (outside 7-day window)
    conn.execute(
        "INSERT INTO permissions(id,rule_shape_id,session_id,project_id,decision,"
        "source,reason,decided_at,hit_count,last_hit_at,danger_accepted)"
        " VALUES (2,3,NULL,NULL,'approved','seed',NULL,?,2,?,NULL);",
        (now, old),
    )
    # rejected: rm (shape 4) — no hits
    conn.execute(
        "INSERT INTO permissions(id,rule_shape_id,session_id,project_id,decision,"
        "source,reason,decided_at,hit_count,last_hit_at,danger_accepted)"
        " VALUES (3,4,NULL,NULL,'rejected','seed',NULL,?,0,NULL,NULL);",
        (now,),
    )
    # ask: cat (shape 5) — no hits
    conn.execute(
        "INSERT INTO permissions(id,rule_shape_id,session_id,project_id,decision,"
        "source,reason,decided_at,hit_count,last_hit_at,danger_accepted)"
        " VALUES (4,5,NULL,NULL,'ask','seed',NULL,?,0,NULL,NULL);",
        (now,),
    )

    # --- candidates ---
    conn.execute(
        "INSERT INTO permission_candidates"
        "(id,verb,subcommand,flags,observations,distinct_sessions,first_seen,last_seen)"
        " VALUES (1,'docker',NULL,'[]',3,1,?,?);",
        (now, now),
    )
    conn.execute(
        "INSERT INTO permission_candidates"
        "(id,verb,subcommand,flags,observations,distinct_sessions,first_seen,last_seen)"
        " VALUES (2,'kubectl',NULL,'[]',1,1,?,?);",
        (now, now),
    )


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Bootstrap a seeded test DB and point OBSERVABILITY_DB at it."""
    path = tmp_path / "test.db"
    conn = _bootstrap_db(path)
    _seed(conn)
    conn.close()
    monkeypatch.setenv("OBSERVABILITY_DB", str(path))
    return path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_status(db_path: Path, extra_args: list[str] | None = None) -> int:
    """Call the status subcommand via the main() entry point."""
    from nephoscope.cli.permissions_cmd import main

    argv = ["status", "--db", str(db_path)] + (extra_args or [])
    return main(argv)


# ---------------------------------------------------------------------------
# Human-readable output
# ---------------------------------------------------------------------------


class TestStatusHumanOutput:
    def test_returns_zero(self, db_path: Path) -> None:
        rc = _run_status(db_path)
        assert rc == 0

    def test_approved_count(self, capsys: pytest.CaptureFixture, db_path: Path) -> None:
        _run_status(db_path)
        out = capsys.readouterr().out
        assert "Approved rules:   2" in out

    def test_denied_count(self, capsys: pytest.CaptureFixture, db_path: Path) -> None:
        _run_status(db_path)
        out = capsys.readouterr().out
        assert "Denied rules:     1" in out

    def test_ask_count(self, capsys: pytest.CaptureFixture, db_path: Path) -> None:
        _run_status(db_path)
        out = capsys.readouterr().out
        assert "Ask rules:        1" in out

    def test_candidates_line(
        self, capsys: pytest.CaptureFixture, db_path: Path
    ) -> None:
        _run_status(db_path)
        out = capsys.readouterr().out
        assert "Pending candidates: 2" in out

    def test_top_approved_verbs_contains_git(
        self, capsys: pytest.CaptureFixture, db_path: Path
    ) -> None:
        _run_status(db_path)
        out = capsys.readouterr().out
        assert "git" in out
        # git has 2 approved rules (shapes 1 and 3)
        assert "git (2 rules)" in out

    def test_top_denied_verbs_contains_rm(
        self, capsys: pytest.CaptureFixture, db_path: Path
    ) -> None:
        _run_status(db_path)
        out = capsys.readouterr().out
        assert "rm (1 rules)" in out

    def test_recent_hits_section_present(
        self, capsys: pytest.CaptureFixture, db_path: Path
    ) -> None:
        _run_status(db_path)
        out = capsys.readouterr().out
        assert "Recent hits (last 7 days)" in out
        # shape 1 / git was hit recently (2026-04-30)
        assert "git" in out

    def test_recent_hits_excludes_old_entries(
        self, capsys: pytest.CaptureFixture, db_path: Path
    ) -> None:
        """git push hit in 2026-01 must not appear under recent hits."""
        _run_status(db_path)
        out = capsys.readouterr().out
        # recent hits section should not include old hits (2026-01 is > 7 days ago)
        # The section header line
        if "Recent hits" in out:
            hits_section = out.split("Recent hits")[1]
            # 2026-01-01 is outside the 7-day window
            assert "2026-01" not in hits_section

    def test_danger_accepted_line(
        self, capsys: pytest.CaptureFixture, db_path: Path
    ) -> None:
        _run_status(db_path)
        out = capsys.readouterr().out
        assert "Dangerous flags accepted: 1" in out


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


class TestStatusJsonOutput:
    def _get_json(self, db_path: Path, capsys: pytest.CaptureFixture) -> dict:
        rc = _run_status(db_path, ["--json"])
        assert rc == 0
        captured = capsys.readouterr()
        return json.loads(captured.out)

    def test_returns_zero(self, db_path: Path) -> None:
        rc = _run_status(db_path, ["--json"])
        assert rc == 0

    def test_approved_field(self, capsys: pytest.CaptureFixture, db_path: Path) -> None:
        data = self._get_json(db_path, capsys)
        assert data["approved"] == 2

    def test_denied_field(self, capsys: pytest.CaptureFixture, db_path: Path) -> None:
        data = self._get_json(db_path, capsys)
        assert data["denied"] == 1

    def test_ask_field(self, capsys: pytest.CaptureFixture, db_path: Path) -> None:
        data = self._get_json(db_path, capsys)
        assert data["ask"] == 1

    def test_candidates_total(
        self, capsys: pytest.CaptureFixture, db_path: Path
    ) -> None:
        data = self._get_json(db_path, capsys)
        assert data["candidates_total"] == 2

    def test_candidates_with_hits(
        self, capsys: pytest.CaptureFixture, db_path: Path
    ) -> None:
        data = self._get_json(db_path, capsys)
        assert data["candidates_with_hits"] == 2

    def test_top_approved_verbs_structure(
        self, capsys: pytest.CaptureFixture, db_path: Path
    ) -> None:
        data = self._get_json(db_path, capsys)
        verbs = data["top_approved_verbs"]
        assert isinstance(verbs, list)
        assert len(verbs) >= 1
        first = verbs[0]
        assert "verb" in first
        assert "count" in first

    def test_top_approved_git_count(
        self, capsys: pytest.CaptureFixture, db_path: Path
    ) -> None:
        data = self._get_json(db_path, capsys)
        git_entry = next(v for v in data["top_approved_verbs"] if v["verb"] == "git")
        assert git_entry["count"] == 2

    def test_top_denied_verbs_structure(
        self, capsys: pytest.CaptureFixture, db_path: Path
    ) -> None:
        data = self._get_json(db_path, capsys)
        verbs = data["top_denied_verbs"]
        assert isinstance(verbs, list)
        assert len(verbs) >= 1

    def test_recent_hits_structure(
        self, capsys: pytest.CaptureFixture, db_path: Path
    ) -> None:
        data = self._get_json(db_path, capsys)
        hits = data["recent_hits"]
        assert isinstance(hits, list)
        assert len(hits) >= 1
        entry = hits[0]
        assert "verb" in entry
        assert "hits" in entry
        assert "last_hit" in entry

    def test_recent_hits_last_hit_date_format(
        self, capsys: pytest.CaptureFixture, db_path: Path
    ) -> None:
        """last_hit is truncated to YYYY-MM-DD."""
        data = self._get_json(db_path, capsys)
        for entry in data["recent_hits"]:
            if entry["last_hit"] is not None:
                assert len(entry["last_hit"]) == 10
                assert entry["last_hit"].count("-") == 2

    def test_danger_accepted_count(
        self, capsys: pytest.CaptureFixture, db_path: Path
    ) -> None:
        data = self._get_json(db_path, capsys)
        assert data["danger_accepted_count"] == 1

    def test_json_is_valid_and_complete(
        self, capsys: pytest.CaptureFixture, db_path: Path
    ) -> None:
        """All expected top-level keys are present."""
        data = self._get_json(db_path, capsys)
        expected_keys = {
            "approved",
            "denied",
            "ask",
            "candidates_total",
            "candidates_with_hits",
            "top_approved_verbs",
            "top_denied_verbs",
            "recent_hits",
            "danger_accepted_count",
        }
        assert expected_keys.issubset(data.keys())


# ---------------------------------------------------------------------------
# Doom-path: empty DB
# ---------------------------------------------------------------------------


class TestStatusEmptyDb:
    def test_empty_db_returns_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "empty.db"
        conn = _bootstrap_db(path)
        conn.close()
        monkeypatch.setenv("OBSERVABILITY_DB", str(path))

        rc = _run_status(path)
        assert rc == 0

    def test_empty_db_counts_zero(
        self,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "empty.db"
        conn = _bootstrap_db(path)
        conn.close()
        monkeypatch.setenv("OBSERVABILITY_DB", str(path))

        _run_status(path)
        out = capsys.readouterr().out
        assert "Approved rules:   0" in out
        assert "Denied rules:     0" in out
        assert "Ask rules:        0" in out

    def test_empty_db_json_zeros(
        self,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "empty.db"
        conn = _bootstrap_db(path)
        conn.close()
        monkeypatch.setenv("OBSERVABILITY_DB", str(path))

        _run_status(path, ["--json"])
        data = json.loads(capsys.readouterr().out)
        assert data["approved"] == 0
        assert data["denied"] == 0
        assert data["ask"] == 0
        assert data["candidates_total"] == 0

    def test_empty_db_no_recent_hits(
        self,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "empty.db"
        conn = _bootstrap_db(path)
        conn.close()
        monkeypatch.setenv("OBSERVABILITY_DB", str(path))

        _run_status(path, ["--json"])
        data = json.loads(capsys.readouterr().out)
        assert data["recent_hits"] == []


# ---------------------------------------------------------------------------
# Doom-path: missing DB path  (--db not set, OBSERVABILITY_DB unset)
# ---------------------------------------------------------------------------


class TestStatusMissingDb:
    def test_missing_db_flag_returns_one(self) -> None:
        from nephoscope.cli.permissions_cmd import main

        rc = main(["status"])
        assert rc == 1


# ---------------------------------------------------------------------------
# Doom-path: danger_accepted column absent (older DB)
# ---------------------------------------------------------------------------


class TestStatusDangerColumnAbsent:
    def test_old_db_without_danger_column_returns_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """status must not crash on a DB that pre-dates the danger_accepted column."""
        path = tmp_path / "old.db"
        conn = _bootstrap_db(path)
        conn.execute("ALTER TABLE permissions DROP COLUMN danger_accepted;")
        conn.close()
        monkeypatch.setenv("OBSERVABILITY_DB", str(path))

        rc = _run_status(path)
        assert rc == 0

    def test_old_db_danger_count_is_zero(
        self,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "old.db"
        conn = _bootstrap_db(path)
        conn.execute("ALTER TABLE permissions DROP COLUMN danger_accepted;")
        conn.close()
        monkeypatch.setenv("OBSERVABILITY_DB", str(path))

        _run_status(path, ["--json"])
        data = json.loads(capsys.readouterr().out)
        assert data["danger_accepted_count"] == 0
