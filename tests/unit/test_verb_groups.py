"""Unit tests for VERB_GROUPS constants in lib.mirror.tool_class."""

from __future__ import annotations

from nephoscope.lib.mirror.tool_class import (
    FULL_ACCESS_VERBS,
    READING_VERBS,
    VERB_GROUPS,
    WRITING_VERBS,
)


def test_reading_verbs_contains_expected_members() -> None:
    assert READING_VERBS == frozenset({"Read", "Glob", "Grep", "LSP"})


def test_writing_verbs_contains_expected_members() -> None:
    assert WRITING_VERBS == frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


def test_full_access_verbs_is_union_of_reading_and_writing() -> None:
    assert FULL_ACCESS_VERBS == READING_VERBS | WRITING_VERBS


def test_verb_groups_full_access_is_same_object() -> None:
    """VERB_GROUPS['Full Access'] must be the FULL_ACCESS_VERBS object, not a copy."""
    assert VERB_GROUPS["Full Access"] is FULL_ACCESS_VERBS


def test_multiedit_in_writing_verbs() -> None:
    assert "MultiEdit" in WRITING_VERBS


def test_multiedit_in_full_access_verbs() -> None:
    assert "MultiEdit" in FULL_ACCESS_VERBS


def test_verb_groups_reading_is_same_object() -> None:
    assert VERB_GROUPS["Reading"] is READING_VERBS


def test_verb_groups_writing_is_same_object() -> None:
    assert VERB_GROUPS["Writing"] is WRITING_VERBS
