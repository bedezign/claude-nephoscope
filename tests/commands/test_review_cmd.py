"""Tests for cli.review_cmd — ``nephoscope-review`` console script.

Covers:
  - argparse happy path (default invocation, no candidates: exit 0, print message)
  - stdin-driven prompt walk for a review session (per-axis prompts)
  - MirrorHashMismatch path: monkeypatched promote raises MirrorHashMismatch → exit 1
  - Non-interactive subcommands (list / show / commit) for LLM-driven review.

Test isolation: all DB access goes through ``tmp_db`` / ``monkeypatch.setenv``
so no live DB is touched.  Prompt input is injected via ``monkeypatch.setattr``
on ``sys.stdin``.
"""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from collections.abc import Generator
from unittest import mock

import pytest

from nephoscope.cli.review_cmd import (
    main,
    _build_path_opts,
    _load_candidates,
    _read_line,
    _resolve_paths_arg,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_PATH = PROJECT_ROOT / "src" / "nephoscope" / "lib" / "schema.sql"


@pytest.fixture
def conn(tmp_db) -> Generator[sqlite3.Connection, None, None]:
    tmp_db.execute("INSERT OR IGNORE INTO tools(name) VALUES ('Bash')")
    tmp_db.execute(
        "INSERT OR IGNORE INTO permission_modes(name) VALUES"
        " ('default'),('acceptEdits'),('bypassPermissions'),('plan'),('auto')"
    )
    tmp_db.execute(
        "INSERT OR IGNORE INTO call_statuses(name) VALUES"
        " ('pending'),('ok'),('err'),('denied'),('orphan')"
    )
    tmp_db.commit()
    yield tmp_db


# ---------------------------------------------------------------------------
# _read_line — stdin helper
# ---------------------------------------------------------------------------


def test_read_line_from_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\n"))
    assert _read_line() == "hello"


def test_read_line_returns_empty_on_eof(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert _read_line() == ""


# ---------------------------------------------------------------------------
# main() — no candidates: exits 0, prints informative message
# ---------------------------------------------------------------------------


def test_main_no_candidates_exits_zero(conn, capsys):
    # No candidates seeded → propose_promotions returns [].
    rc = main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "no promotion candidates" in captured.out.lower()


# ---------------------------------------------------------------------------
# main() — with one candidate: stdin drives per-axis prompts
# ---------------------------------------------------------------------------


def _seed_candidate(conn: sqlite3.Connection, verb: str, flags_json: str) -> None:
    """Seed a single permission_candidates row that exceeds default thresholds."""
    now = "2024-01-01T00:00:00Z"
    conn.execute(
        "INSERT OR REPLACE INTO projects(cwd, name, root, first_seen, last_seen)"
        " VALUES ('/proj', 'proj', '/proj', ?, ?)",
        (now, now),
    )
    proj_id = conn.execute("SELECT id FROM projects WHERE cwd='/proj'").fetchone()[0]

    for i in range(6):
        uuid = f"sess-{i}"
        conn.execute(
            "INSERT OR IGNORE INTO sessions(session_uuid, project_id, transcript_path,"
            " started_at, last_activity)"
            " VALUES (?, ?, '/t', ?, ?)",
            (uuid, proj_id, now, now),
        )

    # Insert one candidate row with 6 observations across 6 sessions.
    conn.execute(
        "INSERT OR IGNORE INTO permission_candidates"
        " (verb, subcommand, flags, observations, distinct_sessions,"
        "  first_seen, last_seen)"
        " VALUES (?, NULL, ?, 6, 6, ?, ?)",
        (verb, flags_json, now, now),
    )
    conn.commit()


def test_main_skip_on_flags_axis(conn, monkeypatch, capsys):
    """'s' at Flags axis → candidate skipped; exit 0."""
    _seed_candidate(conn, "ls", "[]")

    # Provide answers: no verb pattern (skip axis 1), paths=a(any), flags=s(skip)
    responses = iter(["a", "s"])  # paths=a, flags=s
    monkeypatch.setattr("nephoscope.cli.review_cmd._read_line", lambda: next(responses))

    # Mock scan_candidates to return 0 (already scanned) and propose_promotions
    # to return a realistic Candidate.
    from nephoscope.learners.permission.learner import Candidate

    candidate = Candidate(
        id=1,
        verb="ls",
        subcommand=None,
        flags=frozenset(),
        observations=6,
        distinct_sessions=6,
    )

    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch(
            "nephoscope.cli.review_cmd.propose_promotions", return_value=[candidate]
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
    ):
        rc = main([])

    assert rc == 0
    out = capsys.readouterr().out
    assert "skipped" in out


def test_main_promote_global_tier(conn, monkeypatch, capsys):
    """Global tier selection calls promote and reports promoted count."""
    from nephoscope.learners.permission.learner import Candidate

    candidate = Candidate(
        id=1,
        verb="git",
        subcommand="status",
        flags=frozenset(),
        observations=6,
        distinct_sessions=6,
    )

    # Per-axis answers: paths=a, flags=l (literal), tier=g (global)
    responses = iter(["a", "l", "g"])
    monkeypatch.setattr("nephoscope.cli.review_cmd._read_line", lambda: next(responses))

    mock_promote = mock.MagicMock(return_value=0)

    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch(
            "nephoscope.cli.review_cmd.propose_promotions", return_value=[candidate]
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
        mock.patch("nephoscope.cli.review_cmd._do_promote", mock_promote),
    ):
        rc = main([])

    assert rc == 0
    assert mock_promote.called
    out = capsys.readouterr().out
    assert "promoted" in out


# ---------------------------------------------------------------------------
# MirrorHashMismatch propagation → exit 1
# ---------------------------------------------------------------------------


def test_main_mirror_hash_mismatch_exits_one(conn, monkeypatch, capsys):
    """When promote raises MirrorHashMismatch, review exits 1 with the expected message."""
    from nephoscope.learners.permission.learner import Candidate
    from nephoscope.lib.mirror.writer import MirrorHashMismatch

    candidate = Candidate(
        id=1,
        verb="git",
        subcommand="status",
        flags=frozenset(),
        observations=6,
        distinct_sessions=6,
    )

    # paths=a, flags=l, tier=g
    responses = iter(["a", "l", "g"])
    monkeypatch.setattr("nephoscope.cli.review_cmd._read_line", lambda: next(responses))

    def raise_mismatch(*_a, **_kw):
        raise MirrorHashMismatch("/path/to/settings.json: hash mismatch")

    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch(
            "nephoscope.cli.review_cmd.propose_promotions", return_value=[candidate]
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
        mock.patch("nephoscope.cli.review_cmd._do_promote", raise_mismatch),
    ):
        rc = main([])

    assert rc == 1
    out_err = capsys.readouterr()
    combined = out_err.out + out_err.err
    assert "edited externally" in combined or "reconcile" in combined


# ---------------------------------------------------------------------------
# quit at tier prompt stops the loop early
# ---------------------------------------------------------------------------


def test_main_quit_at_tier_exits_loop(conn, monkeypatch, capsys):
    from nephoscope.learners.permission.learner import Candidate

    candidate = Candidate(
        id=1,
        verb="rm",
        subcommand=None,
        flags=frozenset(["-rf"]),
        observations=6,
        distinct_sessions=6,
    )

    # paths=a, flags=l, tier=q (quit)
    responses = iter(["a", "l", "q"])
    monkeypatch.setattr("nephoscope.cli.review_cmd._read_line", lambda: next(responses))

    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch(
            "nephoscope.cli.review_cmd.propose_promotions", return_value=[candidate]
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
    ):
        rc = main([])

    assert rc == 0
    out = capsys.readouterr().out
    assert "quitting" in out.lower() or "quit" in out.lower()


# ---------------------------------------------------------------------------
# _build_path_opts — suggested kwarg
# ---------------------------------------------------------------------------


def test_build_path_opts_suggested_appears_at_position_zero():
    """Suggested path spec is inserted at position 0."""
    opts = _build_path_opts([], "/root", "/cwd", "/home", suggested="$PROJECT_ROOT/**")
    assert opts[0] == "$PROJECT_ROOT/**"


def test_build_path_opts_suggested_deduplicates_with_fallback():
    """When suggested matches a fallback entry, it is not duplicated."""
    opts = _build_path_opts([], "/root", "/cwd", "/home", suggested="$PROJECT_ROOT/**")
    assert opts.count("$PROJECT_ROOT/**") == 1


def test_build_path_opts_none_suggested_unchanged():
    """No suggested → same output as before (fallbacks only)."""
    opts = _build_path_opts([], "/root", "/cwd", "/home", suggested=None)
    assert opts == ["$PROJECT_ROOT/**", "$CWD/**", "$HOME/**"]


def test_build_path_opts_suggested_home_deduplicates_fallback():
    """$HOME/** as suggested still produces only one entry."""
    opts = _build_path_opts([], "", "", "/home", suggested="$HOME/**")
    assert opts == ["$HOME/**"]


# ---------------------------------------------------------------------------
# Non-interactive subcommands: list / show / commit
# ---------------------------------------------------------------------------


def _make_candidate(
    *,
    id: int = 1,
    verb: str = "git",
    subcommand: str | None = "status",
    flags: frozenset[str] = frozenset(),
    observations: int = 10,
    distinct_sessions: int = 4,
    positional_paths: tuple[str, ...] = (),
    suggested_path_spec: str | None = None,
):
    """Return a Candidate with sensible defaults; keyword-only overrides win."""
    from nephoscope.learners.permission.learner import Candidate

    return Candidate(
        id=id,
        verb=verb,
        subcommand=subcommand,
        flags=flags,
        observations=observations,
        distinct_sessions=distinct_sessions,
        positional_paths=positional_paths,
        suggested_path_spec=suggested_path_spec,
    )


# --- list ------------------------------------------------------------------


def test_list_no_candidates_returns_empty_array(conn, capsys):
    """`list` emits `[]` and exits 0 when there are no candidates."""
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[]),
    ):
        rc = main(["list"])

    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert json.loads(out) == []


def test_list_returns_candidate_summaries(conn, capsys):
    """`list` emits one row per candidate with the expected fields."""
    cands = [
        _make_candidate(id=7, verb="rg", subcommand=None, flags=frozenset({"-i"})),
        _make_candidate(
            id=8, verb="git", subcommand="status", flags=frozenset(), observations=42
        ),
    ]
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=cands),
    ):
        rc = main(["list"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list) and len(payload) == 2
    by_id = {row["id"]: row for row in payload}
    assert by_id[7]["verb"] == "rg"
    assert by_id[7]["subcommand"] is None
    assert by_id[7]["flags"] == ["-i"]
    assert by_id[8]["verb"] == "git"
    assert by_id[8]["subcommand"] == "status"
    assert by_id[8]["observations"] == 42
    assert "distinct_sessions" in by_id[8]


def test_list_text_mode(conn, capsys):
    """`list --text` emits human-readable lines that include id and verb."""
    cands = [_make_candidate(id=11, verb="curl", subcommand=None)]
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=cands),
    ):
        rc = main(["list", "--text"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "11" in out
    assert "curl" in out


# --- show ------------------------------------------------------------------


def test_show_unknown_id_exits_nonzero(conn, capsys):
    """Asking to show a candidate id that propose_promotions didn't return is an error."""
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[]),
    ):
        rc = main(["show", "999"])

    assert rc != 0
    err = capsys.readouterr().err
    assert "999" in err or "candidate" in err.lower()


def test_show_returns_full_axis_choices(conn, capsys):
    """`show <id>` emits the four-axis choice set + observation stats."""
    cand = _make_candidate(
        id=5,
        verb="head",
        subcommand=None,
        flags=frozenset({"-<N>"}),
        observations=200,
        distinct_sessions=12,
    )
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[cand]),
        mock.patch(
            "nephoscope.cli.review_cmd._resolve_context",
            return_value=("/home/u", "/work/proj", "/work/proj", 1, 2),
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": '["-<N>"]',
            },
        ),
    ):
        rc = main(["show", "5"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == 5
    assert payload["verb"] == "head"
    assert payload["observations"] == 200
    assert payload["distinct_sessions"] == 12
    axes = payload["axes"]
    # verb axis: literal always present; generalize is None when no pattern.
    assert axes["verb"]["literal"] == "head"
    assert axes["verb"]["generalize"] is None
    # paths axis: indexed list including the project_root / cwd / home fallbacks.
    paths_options = axes["paths"]["options"]
    assert paths_options[0]["index"] == 1
    specs = [opt["spec"] for opt in paths_options]
    assert "$PROJECT_ROOT/**" in specs
    # flags axis: literal + wildcard.
    assert axes["flags"]["literal"] == '["-<N>"]'
    assert axes["flags"]["wildcard"] == "*"
    # tier axis: all available since project_id and session_id were resolved.
    assert axes["tier"]["global"] == "ok"
    assert axes["tier"]["project"] == "ok"
    assert axes["tier"]["session"] == "ok"


def test_show_marks_unavailable_tiers(conn, capsys):
    """When cwd has no project/session record, tier shows 'unavailable'."""
    cand = _make_candidate(id=6, verb="ls")
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[cand]),
        mock.patch(
            "nephoscope.cli.review_cmd._resolve_context",
            return_value=("/home/u", "/work/proj", "", None, None),
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
    ):
        rc = main(["show", "6"])

    assert rc == 0
    axes = json.loads(capsys.readouterr().out)["axes"]
    assert axes["tier"]["global"] == "ok"
    assert axes["tier"]["project"] != "ok"
    assert axes["tier"]["session"] != "ok"


def test_show_includes_verb_pattern_when_substituted(conn, capsys):
    """When verb has a $VAR pattern variant, it surfaces under axes.verb.generalize."""
    cand = _make_candidate(id=9, verb="/home/u/script.sh")
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[cand]),
        mock.patch(
            "nephoscope.cli.review_cmd._resolve_context",
            return_value=("/home/u", "/work/proj", "/work/proj", None, None),
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": "$HOME/script.sh",
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
    ):
        rc = main(["show", "9"])

    assert rc == 0
    axes = json.loads(capsys.readouterr().out)["axes"]
    assert axes["verb"]["literal"] == "/home/u/script.sh"
    assert axes["verb"]["generalize"] == "$HOME/script.sh"


def test_show_text_mode(conn, capsys):
    """`show --text` produces a human-readable rendering."""
    cand = _make_candidate(id=12, verb="curl")
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[cand]),
        mock.patch(
            "nephoscope.cli.review_cmd._resolve_context",
            return_value=("/home/u", "/work/proj", "/work/proj", None, None),
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
    ):
        rc = main(["show", "12", "--text"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "curl" in out
    # Should not be JSON in text mode.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


# --- commit ----------------------------------------------------------------


def test_commit_unknown_id_exits_nonzero(conn, capsys):
    """commit on an unknown id is an error."""
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[]),
    ):
        rc = main(["commit", "999", "--tier", "global"])

    assert rc != 0


def test_commit_promotes_with_global_tier(conn, capsys):
    """commit dispatches to _do_promote with the chosen verb/flags/path/tier."""
    cand = _make_candidate(id=20, verb="rg", subcommand=None, flags=frozenset({"-i"}))
    promote = mock.MagicMock(return_value=None)
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[cand]),
        mock.patch(
            "nephoscope.cli.review_cmd._resolve_context",
            return_value=("/home/u", "/work/proj", "/work/proj", 1, 2),
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": '["-i"]',
            },
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._count_concrete_siblings", return_value=0
        ),
        mock.patch("nephoscope.cli.review_cmd._do_promote", promote),
    ):
        rc = main(["commit", "20", "--tier", "global"])

    assert rc == 0
    assert promote.called
    call = promote.call_args
    # _do_promote(verb, subcommand, flags_json, path_spec, tier, sess_id, proj_id)
    assert call.args[0] == "rg"  # verb literal by default
    assert call.args[1] is None  # subcommand from candidate
    assert call.args[2] == '["-i"]'  # flags literal by default
    assert call.args[3] is None  # paths default to "any"
    assert call.args[4] == "global"
    assert call.args[5] is None  # session_id (global tier)
    assert call.args[6] is None  # project_id (global tier)


def test_commit_paths_index_resolves_to_spec(conn, capsys):
    """`--paths 1` resolves to the first option from _build_path_opts."""
    cand = _make_candidate(id=21)
    promote = mock.MagicMock(return_value=None)
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[cand]),
        mock.patch(
            "nephoscope.cli.review_cmd._resolve_context",
            return_value=("/home/u", "/work/proj", "/work/proj", 1, 2),
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._count_concrete_siblings", return_value=0
        ),
        mock.patch("nephoscope.cli.review_cmd._do_promote", promote),
    ):
        rc = main(["commit", "21", "--tier", "global", "--paths", "1"])

    assert rc == 0
    # First fallback option for project_root="/work/proj", cwd="/work/proj", home="/home/u"
    # (with no path_specs and no suggested) is "$PROJECT_ROOT/**".
    assert promote.call_args.args[3] == "$PROJECT_ROOT/**"


def test_commit_paths_literal_in_opts(conn, capsys):
    """`--paths $HOME/**` accepted when present in opts."""
    cand = _make_candidate(id=22)
    promote = mock.MagicMock(return_value=None)
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[cand]),
        mock.patch(
            "nephoscope.cli.review_cmd._resolve_context",
            return_value=("/home/u", "/work/proj", "/work/proj", 1, 2),
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._count_concrete_siblings", return_value=0
        ),
        mock.patch("nephoscope.cli.review_cmd._do_promote", promote),
    ):
        rc = main(["commit", "22", "--tier", "global", "--paths", "$HOME/**"])

    assert rc == 0
    assert promote.call_args.args[3] == "$HOME/**"


def test_commit_paths_literal_not_in_opts_rejected(conn, capsys):
    """A literal path_spec not present in the candidate's opts is rejected."""
    cand = _make_candidate(id=23)
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[cand]),
        mock.patch(
            "nephoscope.cli.review_cmd._resolve_context",
            return_value=("/home/u", "/work/proj", "/work/proj", 1, 2),
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
    ):
        rc = main(["commit", "23", "--tier", "global", "--paths", "$DOES_NOT_EXIST/**"])

    assert rc != 0


def test_commit_flags_wildcard(conn, capsys):
    """`--flags wildcard` becomes '*'."""
    cand = _make_candidate(id=24, flags=frozenset({"-i", "-v"}))
    promote = mock.MagicMock(return_value=None)
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[cand]),
        mock.patch(
            "nephoscope.cli.review_cmd._resolve_context",
            return_value=("/home/u", "/work/proj", "/work/proj", 1, 2),
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": '["-i","-v"]',
            },
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._count_concrete_siblings", return_value=0
        ),
        mock.patch("nephoscope.cli.review_cmd._do_promote", promote),
    ):
        rc = main(["commit", "24", "--tier", "global", "--flags", "wildcard"])

    assert rc == 0
    assert promote.call_args.args[2] == "*"


def test_commit_verb_generalize_uses_pattern(conn, capsys):
    """`--verb generalize` substitutes the verb pattern."""
    cand = _make_candidate(id=25, verb="/home/u/script.sh")
    promote = mock.MagicMock(return_value=None)
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[cand]),
        mock.patch(
            "nephoscope.cli.review_cmd._resolve_context",
            return_value=("/home/u", "/work/proj", "/work/proj", 1, 2),
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": "$HOME/script.sh",
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._count_concrete_siblings", return_value=0
        ),
        mock.patch("nephoscope.cli.review_cmd._do_promote", promote),
    ):
        rc = main(["commit", "25", "--tier", "global", "--verb", "generalize"])

    assert rc == 0
    assert promote.call_args.args[0] == "$HOME/script.sh"


def test_commit_verb_generalize_without_pattern_rejected(conn, capsys):
    """`--verb generalize` with no available pattern is an error."""
    cand = _make_candidate(id=26)
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[cand]),
        mock.patch(
            "nephoscope.cli.review_cmd._resolve_context",
            return_value=("/home/u", "/work/proj", "/work/proj", 1, 2),
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
    ):
        rc = main(["commit", "26", "--tier", "global", "--verb", "generalize"])

    assert rc != 0


def test_commit_tier_project_no_record_rejected(conn, capsys):
    """`--tier project` without a project record is an error, not a silent global fallback."""
    cand = _make_candidate(id=27)
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[cand]),
        mock.patch(
            "nephoscope.cli.review_cmd._resolve_context",
            return_value=("/home/u", "/work/proj", "", None, None),
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
    ):
        rc = main(["commit", "27", "--tier", "project"])

    assert rc != 0


def test_commit_tier_session_no_record_rejected(conn, capsys):
    """`--tier session` without a session record is an error."""
    cand = _make_candidate(id=28)
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[cand]),
        mock.patch(
            "nephoscope.cli.review_cmd._resolve_context",
            return_value=("/home/u", "/work/proj", "", None, None),
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
    ):
        rc = main(["commit", "28", "--tier", "session"])

    assert rc != 0


def test_commit_hash_mismatch_returns_one(conn, capsys):
    """MirrorHashMismatch from _do_promote surfaces as exit 1."""
    from nephoscope.lib.mirror.writer import MirrorHashMismatch

    cand = _make_candidate(id=29)

    def raise_mismatch(*_a, **_kw):
        raise MirrorHashMismatch("mismatch")

    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[cand]),
        mock.patch(
            "nephoscope.cli.review_cmd._resolve_context",
            return_value=("/home/u", "/work/proj", "/work/proj", 1, 2),
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
        mock.patch("nephoscope.cli.review_cmd._do_promote", raise_mismatch),
    ):
        rc = main(["commit", "29", "--tier", "global"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "reconcile" in err.lower()


def test_commit_subsume_count_reported_for_wildcard(conn, capsys):
    """When flags=wildcard, the post-commit JSON reports the subsumable sibling count."""
    cand = _make_candidate(id=30, flags=frozenset({"-i"}))
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[cand]),
        mock.patch(
            "nephoscope.cli.review_cmd._resolve_context",
            return_value=("/home/u", "/work/proj", "/work/proj", 1, 2),
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": '["-i"]',
            },
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._count_concrete_siblings", return_value=3
        ),
        mock.patch("nephoscope.cli.review_cmd._do_promote", return_value=None),
    ):
        rc = main(["commit", "30", "--tier", "global", "--flags", "wildcard"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == "promoted"
    assert payload["subsumable_concrete_siblings"] == 3


# ---------------------------------------------------------------------------
# Gap 1 — _load_candidates partial-failure: DB connection is closed on error
# ---------------------------------------------------------------------------


def test_load_candidates_scan_raises_propagates_and_closes_conn():
    """When scan_candidates raises, the exception propagates AND conn is closed."""
    mock_conn = mock.MagicMock()
    with (
        mock.patch("nephoscope.cli.review_cmd.connect", return_value=mock_conn),
        mock.patch(
            "nephoscope.cli.review_cmd.scan_candidates",
            side_effect=RuntimeError("scan failed"),
        ),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions"),
    ):
        with pytest.raises(RuntimeError, match="scan failed"):
            _load_candidates()

    mock_conn.close.assert_called_once()


def test_load_candidates_propose_raises_propagates_and_closes_conn():
    """When propose_promotions raises, the exception propagates AND conn is closed."""
    mock_conn = mock.MagicMock()
    with (
        mock.patch("nephoscope.cli.review_cmd.connect", return_value=mock_conn),
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch(
            "nephoscope.cli.review_cmd.propose_promotions",
            side_effect=RuntimeError("propose failed"),
        ),
    ):
        with pytest.raises(RuntimeError, match="propose failed"):
            _load_candidates()

    mock_conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# Gap 2 — _cmd_commit idempotency: second call is not filtered out by CLI
# ---------------------------------------------------------------------------


def test_commit_idempotent_second_call_not_suppressed(conn, capsys):
    """Calling commit twice with the same candidate dispatches _do_promote both times."""
    cand = _make_candidate(id=40, verb="git", subcommand="status", flags=frozenset())
    promote = mock.MagicMock(return_value=None)

    shared_mocks = (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[cand]),
        mock.patch(
            "nephoscope.cli.review_cmd._resolve_context",
            return_value=("/home/u", "/work/proj", "/work/proj", 1, 2),
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._count_concrete_siblings", return_value=0
        ),
        mock.patch("nephoscope.cli.review_cmd._do_promote", promote),
    )

    with (
        shared_mocks[0],
        shared_mocks[1],
        shared_mocks[2],
        shared_mocks[3],
        shared_mocks[4],
        shared_mocks[5],
    ):
        rc1 = main(["commit", "40", "--tier", "global"])
        rc2 = main(["commit", "40", "--tier", "global"])

    assert rc1 == 0
    assert rc2 == 0
    assert promote.call_count == 2
    first_call, second_call = promote.call_args_list
    assert first_call == second_call


# ---------------------------------------------------------------------------
# Gap 3 — _resolve_paths_arg: empty path_opts error branches
# ---------------------------------------------------------------------------


def test_resolve_paths_arg_numeric_on_empty_opts_returns_error():
    """raw='1' with path_opts=[] reports 'no path options for this candidate'."""
    spec, err = _resolve_paths_arg("1", [])
    assert spec is None
    assert err is not None
    assert "no path options for this candidate" in err


def test_resolve_paths_arg_literal_not_in_opts_returns_error():
    """A literal spec not present in opts returns an error mentioning the option."""
    spec, err = _resolve_paths_arg("$HOME/**", [])
    assert spec is None
    assert err is not None
    assert "not in this candidate" in err


def test_resolve_paths_arg_literal_present_in_opts_succeeds():
    """A literal spec that is present in opts is accepted."""
    spec, err = _resolve_paths_arg("$PROJECT_ROOT/**", ["$PROJECT_ROOT/**", "$CWD/**"])
    assert err is None
    assert spec == "$PROJECT_ROOT/**"


def test_resolve_paths_arg_any_returns_none():
    """raw='any' always succeeds with no path constraint."""
    spec, err = _resolve_paths_arg("any", [])
    assert spec is None
    assert err is None


# ---------------------------------------------------------------------------
# Gap 5 — _cmd_show: empty path_specs AND suggested_path_spec=None → fallbacks
# ---------------------------------------------------------------------------


def test_show_empty_path_specs_and_no_suggested_uses_fallbacks(conn, capsys):
    """show with path_specs=[] and suggested_path_spec=None falls back to three defaults."""
    cand = _make_candidate(
        id=50,
        verb="grep",
        subcommand=None,
        flags=frozenset(),
        suggested_path_spec=None,
    )
    with (
        mock.patch("nephoscope.cli.review_cmd.scan_candidates", return_value=0),
        mock.patch("nephoscope.cli.review_cmd.propose_promotions", return_value=[cand]),
        mock.patch(
            "nephoscope.cli.review_cmd._resolve_context",
            return_value=("/home/u", "/work/proj", "/work/proj", 1, 2),
        ),
        mock.patch(
            "nephoscope.cli.review_cmd._pattern_variants",
            return_value={
                "verb_pattern": None,
                "path_specs": [],
                "flags_literal": "[]",
            },
        ),
    ):
        rc = main(["show", "50"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    axes = payload["axes"]

    # suggested is None when the candidate has no suggested_path_spec.
    assert axes["paths"]["suggested"] is None

    # Fallbacks must all be present when path_specs is empty and suggested is None.
    specs = [opt["spec"] for opt in axes["paths"]["options"]]
    assert "$PROJECT_ROOT/**" in specs
    assert "$CWD/**" in specs
    assert "$HOME/**" in specs
    assert len(axes["paths"]["options"]) >= 1
