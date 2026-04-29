"""Schema tests for the verb_categories table.

Verifies DDL shape (columns, CHECK constraint, unique index) against the
live schema applied by tmp_db.
"""

from __future__ import annotations

import sqlite3

import pytest


class TestVerbCategoriesSchema:
    def test_table_exists(self, tmp_db: sqlite3.Connection) -> None:
        row = tmp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='verb_categories';"
        ).fetchone()
        assert row is not None, "verb_categories table is missing from schema"

    def test_columns_are_correct(self, tmp_db: sqlite3.Connection) -> None:
        cols = {
            row[1]: row
            for row in tmp_db.execute("PRAGMA table_info(verb_categories);").fetchall()
        }
        assert "id" in cols
        assert "verb" in cols
        assert "category" in cols
        assert "second_word" in cols

    def test_category_check_constraint_rejects_unknown(
        self, tmp_db: sqlite3.Connection
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            tmp_db.execute(
                "INSERT INTO verb_categories (verb, category, second_word)"
                " VALUES ('myverb', 'unknown_type', NULL);"
            )

    def test_unique_constraint_prevents_duplicate_verb_category_pair(
        self, tmp_db: sqlite3.Connection
    ) -> None:
        tmp_db.execute(
            "INSERT INTO verb_categories (verb, category, second_word)"
            " VALUES ('dedup_verb', 'content_verb', NULL);"
        )
        with pytest.raises(sqlite3.IntegrityError):
            tmp_db.execute(
                "INSERT INTO verb_categories (verb, category, second_word)"
                " VALUES ('dedup_verb', 'content_verb', NULL);"
            )

    def test_second_word_null_and_nonempty_are_distinct_rows(
        self, tmp_db: sqlite3.Connection
    ) -> None:
        # (verb='test', category='content_verb', second_word=NULL)
        tmp_db.execute(
            "INSERT INTO verb_categories (verb, category, second_word)"
            " VALUES ('test', 'content_verb', NULL);"
        )
        # (verb='test', category='task_runner', second_word='run') — different category+second_word
        tmp_db.execute(
            "INSERT INTO verb_categories (verb, category, second_word)"
            " VALUES ('test', 'task_runner', 'run');"
        )
        tmp_db.commit()
        count = tmp_db.execute(
            "SELECT COUNT(*) FROM verb_categories WHERE verb='test';"
        ).fetchone()[0]
        assert count == 2

    def test_second_word_empty_string_conflicts_with_null(
        self, tmp_db: sqlite3.Connection
    ) -> None:
        # UNIQUE INDEX uses IFNULL(second_word, ''), so NULL and '' map to the same key.
        tmp_db.execute(
            "INSERT INTO verb_categories (verb, category, second_word)"
            " VALUES ('xverb', 'content_verb', NULL);"
        )
        with pytest.raises(sqlite3.IntegrityError):
            tmp_db.execute(
                "INSERT INTO verb_categories (verb, category, second_word)"
                " VALUES ('xverb', 'content_verb', '');"
            )
