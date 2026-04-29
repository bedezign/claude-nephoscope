"""Tests for apply_verb_types() in seed.py.

Each test writes a small YAML fixture to a tmp_path file and calls
apply_verb_types against the isolated tmp_db.  Real profile files are
resolved relative to the seed module so the path never depends on cwd.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from nephoscope.learners.permission.seed import apply_verb_types

# Resolve profile dir from the seed module's location.
import nephoscope.learners.permission.seed as _seed_module
import nephoscope.learners.permission.canonicalize as _canon_module

_PROFILES_DIR = Path(_seed_module.__file__).parent / "config" / "fixtures" / "profiles"
_SCHEMA_SQL = Path(_seed_module.__file__).parents[2] / "lib" / "schema.sql"

# Capture the real (unwrapped) _load_verb_categories before the autouse fixture
# in conftest.py replaces the module attribute with a lambda.  __wrapped__ is
# set by functools.lru_cache to point at the original function body.
_real_load_verb_categories = _canon_module._load_verb_categories.__wrapped__


class TestApplyVerbTypes:
    def test_inserts_content_verb_rows(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        fixture = tmp_path / "verbs.yaml"
        fixture.write_text(
            yaml.dump(
                [
                    {"verb": "alpha", "category": "content_verb"},
                    {"verb": "beta", "category": "content_verb"},
                ]
            )
        )
        apply_verb_types(tmp_db, fixture)
        count = tmp_db.execute(
            "SELECT COUNT(*) FROM verb_categories WHERE category='content_verb';"
        ).fetchone()[0]
        assert count == 2

    def test_inserts_task_runner_with_second_word(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        fixture = tmp_path / "runner.yaml"
        fixture.write_text(
            yaml.dump(
                [{"verb": "cargo", "category": "task_runner", "second_word": "run"}]
            )
        )
        apply_verb_types(tmp_db, fixture)
        row = tmp_db.execute(
            "SELECT second_word FROM verb_categories"
            " WHERE verb='cargo' AND category='task_runner';"
        ).fetchone()
        assert row is not None
        assert row[0] == "run"

    def test_inserts_script_runner(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        fixture = tmp_path / "script.yaml"
        fixture.write_text(yaml.dump([{"verb": "ruby", "category": "script_runner"}]))
        apply_verb_types(tmp_db, fixture)
        row = tmp_db.execute(
            "SELECT id FROM verb_categories WHERE verb='ruby' AND category='script_runner';"
        ).fetchone()
        assert row is not None

    def test_idempotent_second_apply_does_not_duplicate(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        fixture = tmp_path / "idem.yaml"
        fixture.write_text(yaml.dump([{"verb": "myverb", "category": "content_verb"}]))
        apply_verb_types(tmp_db, fixture)
        apply_verb_types(tmp_db, fixture)
        count = tmp_db.execute(
            "SELECT COUNT(*) FROM verb_categories WHERE verb='myverb';"
        ).fetchone()[0]
        assert count == 1

    def test_returns_fixture_row_count(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        entries = [
            {"verb": "v1", "category": "content_verb"},
            {"verb": "v2", "category": "content_verb"},
            {"verb": "v3", "category": "script_runner"},
        ]
        fixture = tmp_path / "count.yaml"
        fixture.write_text(yaml.dump(entries))
        result = apply_verb_types(tmp_db, fixture)
        assert result == len(entries)

    def test_returns_fixture_row_count_even_when_all_ignored(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        # Idempotency: second call returns the entry count, not actual inserts.
        fixture = tmp_path / "dup.yaml"
        fixture.write_text(
            yaml.dump([{"verb": "dup_verb", "category": "content_verb"}])
        )
        apply_verb_types(tmp_db, fixture)
        result = apply_verb_types(tmp_db, fixture)
        assert result == 1

    def test_rejects_missing_verb(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        fixture = tmp_path / "no_verb.yaml"
        fixture.write_text(yaml.dump([{"category": "content_verb"}]))
        with pytest.raises(ValueError, match="missing verb"):
            apply_verb_types(tmp_db, fixture)

    def test_rejects_invalid_category(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        fixture = tmp_path / "bad_cat.yaml"
        fixture.write_text(yaml.dump([{"verb": "myverb", "category": "bogus"}]))
        with pytest.raises(ValueError, match="invalid category"):
            apply_verb_types(tmp_db, fixture)

    def test_applies_core_profile(self, tmp_db: sqlite3.Connection) -> None:
        core = _PROFILES_DIR / "core.yaml"
        result = apply_verb_types(tmp_db, core)
        assert result >= 50
        count = tmp_db.execute(
            "SELECT COUNT(*) FROM verb_categories WHERE category='content_verb';"
        ).fetchone()[0]
        assert count >= 50

    def test_applies_python_profile(self, tmp_db: sqlite3.Connection) -> None:
        python_profile = _PROFILES_DIR / "python.yaml"
        apply_verb_types(tmp_db, python_profile)
        verbs = {
            row[0]
            for row in tmp_db.execute(
                "SELECT verb FROM verb_categories WHERE category='script_runner';"
            ).fetchall()
        }
        assert "python3" in verbs
        assert "python" in verbs

    def test_empty_fixture_returns_zero(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        fixture = tmp_path / "empty.yaml"
        fixture.write_text("[]")
        result = apply_verb_types(tmp_db, fixture)
        assert result == 0
        count = tmp_db.execute("SELECT COUNT(*) FROM verb_categories;").fetchone()[0]
        assert count == 0

    def test_rejects_empty_string_verb(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        fixture = tmp_path / "empty_verb.yaml"
        fixture.write_text(yaml.dump([{"verb": "", "category": "content_verb"}]))
        with pytest.raises(ValueError, match="missing verb"):
            apply_verb_types(tmp_db, fixture)

    def test_invalid_entry_does_not_insert_preceding_valid_entries(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        fixture = tmp_path / "partial.yaml"
        fixture.write_text(
            yaml.dump(
                [
                    {"verb": "good_verb", "category": "content_verb"},
                    {"verb": "bad_verb", "category": "not_a_real_category"},
                ]
            )
        )
        with pytest.raises(ValueError, match="invalid category"):
            apply_verb_types(tmp_db, fixture)
        count = tmp_db.execute(
            "SELECT COUNT(*) FROM verb_categories WHERE verb='good_verb';"
        ).fetchone()[0]
        assert count == 0, "partial fixture must not insert rows before the bad entry"


class TestLoadVerbCategoriesDbRead:
    """Write-then-read contract: apply_verb_types writes rows that _load_verb_categories reads.

    Uses _real_load_verb_categories (the unwrapped function body) with
    observations_db_path monkeypatched to a fresh temp DB so the real SELECT
    query is exercised — the autouse fixture in conftest.py never calls it.
    """

    def test_script_runner_inserted_by_apply_is_read_by_loader(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "obs.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_SCHEMA_SQL.read_text(encoding="utf-8"))

        fixture = tmp_path / "ruby.yaml"
        fixture.write_text(yaml.dump([{"verb": "ruby", "category": "script_runner"}]))
        apply_verb_types(conn, fixture)
        conn.commit()
        conn.close()

        # canonicalize.py imports observations_db_path directly into its namespace,
        # so we must patch the name there, not in lib.paths.
        monkeypatch.setattr(_canon_module, "observations_db_path", lambda: db_path)

        result = _real_load_verb_categories()
        assert "ruby" in result["script_runner"], (
            "_load_verb_categories did not read the script_runner row written by apply_verb_types"
        )
