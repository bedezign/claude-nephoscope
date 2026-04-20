"""Tests for learners.permission.seed — the fixture round-trip loader."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from learners.permission.seed import (
    _load_fixture,
    apply_fixture,
    export_rejected,
    export_shapes,
    write_fixture,
)


def _active_count(conn) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM permission_active;").fetchone()[0]
    )


def _rejected_count(conn) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM permission_rejected;").fetchone()[0]
    )


def _shape_count(conn) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM command_shapes;").fetchone()[0]
    )


def test_fixture_loads_without_error():
    active, rejected = _load_fixture()
    assert isinstance(active, list)
    assert isinstance(rejected, list)
    for entry in active:
        assert isinstance(entry.get("verb"), str) and entry["verb"]


def test_apply_fixture_idempotent(tmp_db):
    active, rejected = _load_fixture()
    counts1 = apply_fixture(tmp_db, active, rejected)
    assert counts1["active"] == (len(active), 0)
    assert counts1["rejected"] == (len(rejected), 0)

    counts2 = apply_fixture(tmp_db, active, rejected)
    assert counts2["active"] == (0, len(active))
    assert counts2["rejected"] == (0, len(rejected))

    assert _active_count(tmp_db) == len(active)
    assert _rejected_count(tmp_db) == len(rejected)


def test_apply_fixture_fills_gap(tmp_db):
    active, _ = _load_fixture()
    first = apply_fixture(tmp_db, active[:-1])
    assert first["active"] == (len(active) - 1, 0)

    second = apply_fixture(tmp_db, active)
    assert second["active"] == (1, len(active) - 1)


def test_applies_expected_source_tag(tmp_db):
    active, rejected = _load_fixture()
    apply_fixture(tmp_db, active, rejected)
    sources = {
        row[0]
        for row in tmp_db.execute(
            "SELECT DISTINCT source FROM permission_active;"
        ).fetchall()
    }
    if active:
        assert sources == {"manual"}


def test_missing_verb_rejected(tmp_db):
    with pytest.raises(ValueError, match="missing 'verb'"):
        apply_fixture(tmp_db, [{"flags": ["-l"]}])


def test_flags_order_independent(tmp_db):
    apply_fixture(tmp_db, [{"verb": "ls", "flags": ["-l", "-a"]}])
    counts = apply_fixture(tmp_db, [{"verb": "ls", "flags": ["-a", "-l"]}])
    assert counts["active"] == (0, 1)
    assert _shape_count(tmp_db) == 1
    assert _active_count(tmp_db) == 1


def test_export_reflects_db_state(tmp_db):
    apply_fixture(
        tmp_db,
        [
            {"verb": "ls"},
            {"verb": "ls", "flags": ["-l", "-a"]},
            {"verb": "grep", "flags": ["-E"], "source": "learner"},
        ],
    )
    entries = export_shapes(tmp_db)
    assert len(entries) == 3
    assert [e["verb"] for e in entries] == ["grep", "ls", "ls"]
    grep_entry = next(e for e in entries if e["verb"] == "grep")
    assert grep_entry.get("source") == "learner"
    assert "source" not in next(
        e for e in entries if e["verb"] == "ls" and "flags" not in e
    )
    ls_flagged = next(e for e in entries if "flags" in e and e["verb"] == "ls")
    assert ls_flagged["flags"] == ["-a", "-l"]


def test_export_then_apply_round_trip(tmp_db, tmp_path: Path):
    active_in = [
        {"verb": "echo"},
        {"verb": "ls", "flags": ["-l"]},
        {"verb": "grep", "flags": ["-E", "-v"], "source": "learner"},
    ]
    rejected_in = [
        {"verb": "du", "flags": ["-s", "-a"], "reason": "too noisy"},
    ]
    apply_fixture(tmp_db, active_in, rejected_in)
    exported_active = export_shapes(tmp_db)
    exported_rejected = export_rejected(tmp_db)

    out = tmp_path / "safe_shapes.yaml"
    write_fixture(exported_active, exported_rejected, path=out)

    active, rejected = _load_fixture(path=out)
    assert len(active) == len(active_in)
    assert len(rejected) == len(rejected_in)

    # Apply to a clean slate and verify counts + source/reason preserved.
    tmp_db.execute("DELETE FROM permission_active;")
    tmp_db.execute("DELETE FROM permission_rejected;")
    tmp_db.execute("DELETE FROM command_shapes;")
    tmp_db.commit()
    counts = apply_fixture(tmp_db, active, rejected)
    assert counts["active"] == (len(active_in), 0)
    assert counts["rejected"] == (len(rejected_in), 0)

    grep_row = tmp_db.execute(
        """
        SELECT pa.source FROM permission_active pa
          JOIN command_shapes cs ON cs.id = pa.command_shape_id
         WHERE cs.verb = 'grep';
        """
    ).fetchone()
    assert grep_row[0] == "learner"

    du_row = tmp_db.execute(
        """
        SELECT r.reason FROM permission_rejected r
          JOIN command_shapes cs ON cs.id = r.command_shape_id
         WHERE cs.verb = 'du';
        """
    ).fetchone()
    assert du_row[0] == "too noisy"


def test_export_deterministic_ordering(tmp_db):
    apply_fixture(
        tmp_db,
        [
            {"verb": "ls", "flags": ["-l"]},
            {"verb": "echo"},
            {"verb": "ls"},
            {"verb": "cat"},
        ],
    )
    entries = export_shapes(tmp_db)
    verbs_and_flags = [
        (e["verb"], tuple(e.get("flags") or [])) for e in entries
    ]
    assert verbs_and_flags == sorted(verbs_and_flags)


def test_write_fixture_preserves_header(tmp_db, tmp_path: Path):
    apply_fixture(tmp_db, [{"verb": "echo"}])
    out = tmp_path / "fixture.yaml"
    write_fixture(export_shapes(tmp_db), export_rejected(tmp_db), path=out)
    text = out.read_text()
    assert "Permission-learner fixture" in text
    assert "round-trip" in text.lower()
    data = yaml.safe_load(text)
    assert data["active"][0]["verb"] == "echo"


def test_apply_rejects_invalid_source(tmp_db):
    with pytest.raises(ValueError, match="invalid source"):
        apply_fixture(tmp_db, [{"verb": "echo", "source": "bogus"}])


def test_legacy_shapes_key_still_loads(tmp_path: Path):
    """Back-compat: a fixture using the old ``shapes:`` top-level must load
    as active-only (rejected empty)."""
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text("shapes:\n- {verb: echo}\n- {verb: ls, flags: [-l]}\n")
    active, rejected = _load_fixture(path=legacy)
    assert [e["verb"] for e in active] == ["echo", "ls"]
    assert rejected == []


def test_rejected_entry_with_reason(tmp_db):
    apply_fixture(
        tmp_db,
        [],
        [{"verb": "find", "flags": ["-delete"], "reason": "destructive"}],
    )
    assert _rejected_count(tmp_db) == 1
    row = tmp_db.execute(
        "SELECT cs.verb, r.reason FROM permission_rejected r "
        "JOIN command_shapes cs ON cs.id=r.command_shape_id;"
    ).fetchone()
    assert row == ("find", "destructive")
