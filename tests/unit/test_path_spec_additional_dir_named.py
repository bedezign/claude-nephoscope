"""Tests for $ADDITIONAL_DIR named placeholder in to_pattern_form."""

from __future__ import annotations

from nephoscope.learners.permission.canonicalize import parse_command, to_pattern_form

_EXTRA_DIR = "/opt/company/shared"


def _leaf(cmd: str):
    leaves = parse_command(cmd)
    assert leaves, f"parse_command returned empty for {cmd!r}"
    return leaves[0]


def test_path_under_additional_dir_emits_named_glob():
    """Path under an additional_dir emits $ADDITIONAL_DIR/** alongside absolute spec."""
    leaf = _leaf("cp /opt/company/shared/build/output /tmp/out")
    result = to_pattern_form(leaf, {}, additional_dirs=[_EXTRA_DIR])
    path_specs = {v.path_spec for v in result}
    assert "$ADDITIONAL_DIR/**" in path_specs


def test_path_under_additional_dir_emits_named_tail():
    """Path under an additional_dir emits $ADDITIONAL_DIR/<tail> alongside absolute spec."""
    leaf = _leaf("cp /opt/company/shared/build/output /tmp/out")
    result = to_pattern_form(leaf, {}, additional_dirs=[_EXTRA_DIR])
    path_specs = {v.path_spec for v in result}
    assert "$ADDITIONAL_DIR/build/output" in path_specs


def test_absolute_additional_dir_forms_still_present():
    """Adding $ADDITIONAL_DIR named forms is additive; absolute forms are preserved."""
    leaf = _leaf("cp /opt/company/shared/build/output /tmp/out")
    result = to_pattern_form(leaf, {}, additional_dirs=[_EXTRA_DIR])
    path_specs = {v.path_spec for v in result}
    assert _EXTRA_DIR + "/**" in path_specs
    assert _EXTRA_DIR + "/build/output" in path_specs
