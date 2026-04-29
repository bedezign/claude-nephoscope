"""Tests for $TRUSTED_DIR placeholder in to_pattern_form."""

from __future__ import annotations

from nephoscope.learners.permission.canonicalize import parse_command, to_pattern_form


def _leaf(cmd: str):
    leaves = parse_command(cmd)
    assert leaves, f"parse_command returned empty for {cmd!r}"
    return leaves[0]


def test_path_under_trusted_dir_emits_trusted_dir_glob():
    """Path under a trusted dir emits both $TRUSTED_DIR/** and $TRUSTED_DIR/<tail>."""
    leaf = _leaf("rm -rf /trusted/root/subdir")
    result = to_pattern_form(leaf, {}, trusted_dirs=["/trusted/root"])
    path_specs = {v.path_spec for v in result}
    assert "$TRUSTED_DIR/**" in path_specs
    assert "$TRUSTED_DIR/subdir" in path_specs


def test_path_equal_to_trusted_dir_emits_glob_only():
    """Path equal to a trusted dir emits $TRUSTED_DIR/** but no specific subpath."""
    leaf = _leaf("rm -rf /trusted/root")
    result = to_pattern_form(leaf, {}, trusted_dirs=["/trusted/root"])
    path_specs = {v.path_spec for v in result}
    assert "$TRUSTED_DIR/**" in path_specs
    assert not any(
        ps and ps.startswith("$TRUSTED_DIR/") and ps != "$TRUSTED_DIR/**"
        for ps in path_specs
    )


def test_path_outside_trusted_dir_emits_no_trusted_dir_spec():
    """Path outside every trusted dir emits no $TRUSTED_DIR spec."""
    leaf = _leaf("rm -rf /other/path")
    result = to_pattern_form(leaf, {}, trusted_dirs=["/trusted/root"])
    assert not any(
        ps and "$TRUSTED_DIR" in ps for ps in {v.path_spec for v in result} if ps
    )


def test_ctx_var_takes_priority_over_trusted_dir():
    """Path matching both $HOME ctx var and a trusted dir emits only $HOME spec."""
    leaf = _leaf("rm -rf /home/user/subdir")
    result = to_pattern_form(leaf, {"home": "/home/user"}, trusted_dirs=["/home/user"])
    path_specs = {v.path_spec for v in result}
    assert "$HOME/**" in path_specs
    assert not any(ps and "$TRUSTED_DIR" in ps for ps in path_specs if ps)


def test_trusted_dir_none_emits_no_trusted_dir_spec():
    """Passing trusted_dirs=None emits no $TRUSTED_DIR spec."""
    leaf = _leaf("rm -rf /some/path")
    result = to_pattern_form(leaf, {}, trusted_dirs=None)
    assert not any(
        ps and "$TRUSTED_DIR" in ps for ps in {v.path_spec for v in result} if ps
    )


def test_trusted_dir_wins_over_additional_dir():
    """Path under both a trusted_dir and an additional_dir emits $TRUSTED_DIR, not inline absolute."""
    leaf = _leaf("rm -rf /shared/work/output")
    result = to_pattern_form(
        leaf,
        {},
        additional_dirs=["/shared/work"],
        trusted_dirs=["/shared/work"],
    )
    path_specs = {v.path_spec for v in result}
    assert "$TRUSTED_DIR/**" in path_specs
    assert not any(
        ps and "$TRUSTED_DIR" not in ps and "/shared/work" in (ps or "")
        for ps in path_specs
    )


def test_duplicate_trusted_dirs_produce_single_path_spec():
    """Duplicate trusted_dirs entries must produce the same path_spec coverage as a single entry."""
    leaf = _leaf("rm -rf /home/user/project/build")
    single = to_pattern_form(leaf, {}, trusted_dirs=["/home/user/project"])
    duped = to_pattern_form(
        leaf, {}, trusted_dirs=["/home/user/project", "/home/user/project"]
    )
    assert {v.path_spec for v in single} == {v.path_spec for v in duped}
