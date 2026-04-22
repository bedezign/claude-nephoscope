"""Tests for learners.permission.canonicalize — parse_command + to_pattern_form."""

from __future__ import annotations

import json
from collections.abc import Iterable


from learners.permission.canonicalize import (
    CanonicalLeaf,
    PatternVariant,
    Redirection,
    parse_command,
    to_pattern_form,
)


# ===========================================================================
# parse_command — existing behaviour (preserved from pre-Phase-8 suite)
# ===========================================================================


def test_simple_verb_and_subcommand():
    leaves = parse_command("git status")
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf.verb == "git"
    assert leaf.subcommand == "status"
    assert leaf.flags == frozenset()
    assert leaf.redirections == ()


def test_env_var_assignment_is_stripped_and_task_runner_resolves():
    leaves = parse_command("FOO=bar uv run pytest -q")
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf.verb == "uv"
    # Task-runner exception: ("uv", "run") consumed, pytest is the subcommand.
    assert leaf.subcommand == "pytest"
    assert leaf.flags == frozenset({"-q"})


def test_secret_in_env_var_never_appears_in_output():
    raw = (
        "COLLECTIVE_DATABASE_URL=postgresql://user:s3cret@host/db "
        "uv run pytest -q 2>&1 | tail -10"
    )
    leaves = parse_command(raw)
    # Pipeline yields two leaves.
    assert len(leaves) == 2

    uv_leaf = next(leaf for leaf in leaves if leaf.verb == "uv")
    tail_leaf = next(leaf for leaf in leaves if leaf.verb == "tail")

    # uv leaf canonicalized as task runner.
    assert uv_leaf.subcommand == "pytest"
    assert uv_leaf.flags == frozenset({"-q"})

    # The credentialed URL must not appear anywhere on either leaf.
    for leaf in leaves:
        assert "s3cret" not in leaf.raw_leaf
        assert "postgresql" not in leaf.raw_leaf
        assert not any("s3cret" in f for f in leaf.flags)

    # Tail: -10 is a numeric flag and is collapsed to -<N>.
    assert tail_leaf.subcommand is None
    assert tail_leaf.flags == frozenset({"-<N>"})


def test_command_substitution_yields_inner_and_outer_leaves():
    leaves = parse_command("rm -rf $(pwd)")
    # Two leaves: inner pwd plus outer rm.
    verbs = sorted(leaf.verb for leaf in leaves)
    assert verbs == ["pwd", "rm"]

    rm_leaf = next(leaf for leaf in leaves if leaf.verb == "rm")
    # bashlex tokenizes `-rf` as a single word; we split POSIX clusters so
    # ``rm -rf`` and ``rm -r -f`` produce the same flag set.
    assert rm_leaf.subcommand is None
    assert rm_leaf.flags == frozenset({"-r", "-f"})


def test_list_operators_produce_one_leaf_per_command():
    leaves = parse_command("a; b && c || d")
    verbs = [leaf.verb for leaf in leaves]
    assert verbs == ["a", "b", "c", "d"]
    for leaf in leaves:
        assert leaf.subcommand is None
        assert leaf.flags == frozenset()


def test_redirection_is_captured():
    leaves = parse_command("cmd > /tmp/file")
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf.verb == "cmd"
    assert leaf.redirections == (Redirection(op=">", target="/tmp/file"),)


def test_dev_null_redirection_is_dropped_as_noise():
    leaves = parse_command("cmd > /dev/null")
    assert len(leaves) == 1
    assert leaves[0].redirections == ()


def test_fd_redirection_is_dropped_as_noise():
    # 2>&1 has no file target; should not produce a Redirection.
    leaves = parse_command("cmd 2>&1")
    assert len(leaves) == 1
    assert leaves[0].redirections == ()


def test_append_redirection_is_captured_with_op_marker():
    leaves = parse_command("echo hi >> /tmp/log")
    assert len(leaves) == 1
    redirs = leaves[0].redirections
    assert len(redirs) == 1
    assert redirs[0].op == ">>"
    assert redirs[0].target == "/tmp/log"


def test_malformed_input_returns_empty_list():
    # bashlex raises ParsingError; we swallow and return [].
    assert parse_command("malformed (((") == []


def test_empty_and_whitespace_inputs_return_empty_list():
    assert parse_command("") == []
    assert parse_command("   ") == []


def test_make_runner_without_run_target():
    # ("make",) is in TASK_RUNNERS with length 1: the next positional word
    # becomes the subcommand (e.g. `make test` → subcommand='test').
    leaves = parse_command("make test")
    assert len(leaves) == 1
    assert leaves[0].verb == "make"
    assert leaves[0].subcommand == "test"


def test_flag_with_value_drops_the_value():
    leaves = parse_command("git commit -m 'a secret message'")
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf.verb == "git"
    assert leaf.subcommand == "commit"
    # Only the flag, not its value.
    assert leaf.flags == frozenset({"-m"})
    assert "secret" not in " ".join(leaf.flags)


def test_long_flag_equals_value_keeps_only_flag_name():
    leaves = parse_command("curl --output=/tmp/x https://example.org")
    assert len(leaves) == 1
    leaf = leaves[0]
    # --output=/tmp/x → just --output
    assert "--output" in leaf.flags
    assert not any("=" in f for f in leaf.flags)


# ---------------------------------------------------------------------------
# POSIX short-flag cluster splitting
# ---------------------------------------------------------------------------


def test_short_flag_cluster_rm_rf_splits_into_r_and_f():
    leaves = parse_command("rm -rf /tmp/foo")
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf.verb == "rm"
    assert leaf.flags == frozenset({"-r", "-f"})


def test_short_flag_cluster_ls_la_splits_into_l_and_a():
    leaves = parse_command("ls -la")
    assert len(leaves) == 1
    assert leaves[0].flags == frozenset({"-l", "-a"})


def test_short_flag_cluster_tar_xvf_splits_into_three_flags():
    leaves = parse_command("tar -xvf archive.tgz")
    assert len(leaves) == 1
    assert leaves[0].flags == frozenset({"-x", "-v", "-f"})


def test_numeric_dash_arg_is_not_split_into_per_digit_flags():
    leaves = parse_command("tail -10")
    assert len(leaves) == 1
    flags = leaves[0].flags
    assert "-1" not in flags
    assert "-0" not in flags
    assert flags == frozenset({"-<N>"})


def test_long_flag_is_not_cluster_split():
    leaves = parse_command("git commit --force")
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf.verb == "git"
    assert leaf.subcommand == "commit"
    assert leaf.flags == frozenset({"--force"})


def test_mixed_letter_digit_flag_is_not_cluster_split():
    leaves = parse_command("gcc -O3 main.c")
    assert len(leaves) == 1
    assert leaves[0].flags == frozenset({"-O3"})


# ---------------------------------------------------------------------------
# Process/command substitution filtered from subcommand slot
# ---------------------------------------------------------------------------


def test_process_substitution_input_is_not_outer_subcommand():
    leaves = parse_command("diff <(ls) <(ls -a)")
    diff_leaves = [leaf for leaf in leaves if leaf.verb == "diff"]
    assert len(diff_leaves) == 1
    assert diff_leaves[0].subcommand is None

    ls_leaves = [leaf for leaf in leaves if leaf.verb == "ls"]
    assert len(ls_leaves) == 2
    flag_sets = sorted(frozenset(leaf.flags) for leaf in ls_leaves)
    assert frozenset() in flag_sets
    assert frozenset({"-a"}) in flag_sets


def test_command_substitution_is_not_outer_subcommand():
    leaves = parse_command("cat $(which git)")
    cat_leaves = [leaf for leaf in leaves if leaf.verb == "cat"]
    assert len(cat_leaves) == 1
    assert cat_leaves[0].subcommand is None

    which_leaves = [leaf for leaf in leaves if leaf.verb == "which"]
    assert len(which_leaves) == 1


def test_process_substitution_output_is_not_outer_subcommand():
    leaves = parse_command("cat >(tee log)")
    cat_leaves = [leaf for leaf in leaves if leaf.verb == "cat"]
    assert len(cat_leaves) == 1
    assert cat_leaves[0].subcommand is None

    tee_leaves = [leaf for leaf in leaves if leaf.verb == "tee"]
    assert len(tee_leaves) == 1


def test_dash_prefixed_quoted_content_not_treated_as_flag():
    leaves = parse_command('echo "--- banner ---"')
    assert leaves[0].flags == frozenset()


def test_content_verb_echo_drops_message_as_subcommand():
    a = parse_command('echo "hello world"')
    b = parse_command('echo "goodbye"')
    assert len(a) == 1 and len(b) == 1
    assert a[0].verb == "echo"
    assert a[0].subcommand is None
    assert a[0].flags == frozenset()
    assert (a[0].verb, a[0].subcommand, a[0].flags) == (
        b[0].verb,
        b[0].subcommand,
        b[0].flags,
    )


def test_content_verb_ls_drops_path_as_subcommand():
    a = parse_command("ls /home/steve")
    b = parse_command("ls /tmp")
    assert a[0].subcommand is None
    assert b[0].subcommand is None
    assert a[0].flags == b[0].flags == frozenset()


def test_content_verb_ls_still_captures_flags():
    leaves = parse_command("ls -la /home/steve")
    assert leaves[0].verb == "ls"
    assert leaves[0].subcommand is None
    assert leaves[0].flags == frozenset({"-l", "-a"})


def test_content_verb_cat_collapses_file_argument():
    a = parse_command("cat /etc/hosts")
    b = parse_command("cat /etc/passwd")
    assert a[0].subcommand is None
    assert b[0].subcommand is None


def test_content_verb_grep_collapses_pattern_argument():
    a = parse_command("grep needle")
    b = parse_command("grep haystack")
    assert a[0].subcommand is None
    assert b[0].subcommand is None


def test_content_verb_find_path_dropped_flags_kept():
    leaves = parse_command("find /home -name '*.py'")
    assert leaves[0].verb == "find"
    assert leaves[0].subcommand is None
    assert leaves[0].flags  # non-empty


def test_non_content_verb_retains_subcommand():
    leaves = parse_command("git status")
    assert leaves[0].verb == "git"
    assert leaves[0].subcommand == "status"


def test_task_runner_wins_over_content_verb_lookup():
    leaves = parse_command("uv run pytest -q")
    assert leaves[0].verb == "uv"
    assert leaves[0].subcommand == "pytest"


def test_sed_script_argument_is_content():
    a = parse_command("sed 's/a/b/' file.txt")
    b = parse_command("sed 's/x/y/' file.txt")
    assert a[0].subcommand is None
    assert b[0].subcommand is None
    inplace = parse_command("sed -i 's/a/b/' file.txt")
    assert "-i" in inplace[0].flags


# ---------------------------------------------------------------------------
# Numeric flag collapsing (-<N> sentinel)
# ---------------------------------------------------------------------------


def test_numeric_flag_head_40_and_head_100_produce_identical_shape():
    a = parse_command("head -40")
    b = parse_command("head -100")
    assert len(a) == 1 and len(b) == 1
    assert a[0].flags == frozenset({"-<N>"})
    assert b[0].flags == frozenset({"-<N>"})
    assert a[0].flags == b[0].flags


def test_numeric_flag_tail_5_yields_sentinel():
    leaves = parse_command("tail -5")
    assert len(leaves) == 1
    assert "-<N>" in leaves[0].flags


def test_numeric_flag_boundary_zero_and_leading_zeros_collapse():
    for variant in ("-0", "-00", "-01", "-007"):
        leaves = parse_command(f"head {variant}")
        assert len(leaves) == 1
        assert leaves[0].flags == frozenset({"-<N>"})


def test_numeric_flag_with_long_flag_option_stays_positional():
    leaves = parse_command("head -n 40")
    assert len(leaves) == 1
    assert "-n" in leaves[0].flags
    assert "-<N>" not in leaves[0].flags


def test_letter_digit_mixed_flags_stay_verbatim():
    gcc = parse_command("gcc -O3 foo.c")
    assert len(gcc) == 1
    assert "-O3" in gcc[0].flags

    j_flag = parse_command("make -j4")
    assert len(j_flag) == 1
    assert "-j4" in j_flag[0].flags


def test_long_flag_with_equals_not_affected():
    leaves = parse_command("some_cmd --jobs=4")
    assert len(leaves) == 1
    assert "--jobs" in leaves[0].flags
    assert "-<N>" not in leaves[0].flags


def test_numeric_flag_in_positional_position():
    leaves = parse_command("seq 1 -5")
    assert len(leaves) == 1
    assert "-<N>" in leaves[0].flags


def test_wget_capital_n_flag_literal():
    leaves = parse_command("wget -N https://example.com")
    assert len(leaves) == 1
    assert "-N" in leaves[0].flags
    assert "-<N>" not in leaves[0].flags


def test_ssh_capital_n_flag_literal():
    leaves = parse_command("ssh -N -L 8080:localhost:80 host")
    assert len(leaves) == 1
    assert "-N" in leaves[0].flags
    assert "-<N>" not in leaves[0].flags


def test_ls_capital_n_flag_literal():
    leaves = parse_command("ls -N")
    assert len(leaves) == 1
    assert "-N" in leaves[0].flags
    assert "-<N>" not in leaves[0].flags


# ===========================================================================
# to_pattern_form — new Phase 8 behaviour
# ===========================================================================

HOME = "/home/alice"
PROJECT = "/home/alice/work/myproject"
CWD = "/home/alice/work/myproject"

CTX_FULL = {"home": HOME, "project_root": PROJECT, "cwd": CWD}
CTX_HOME_ONLY = {"home": HOME}
CTX_EMPTY: dict[str, str] = {}


def _leaf(
    verb, *, subcommand=None, flags: Iterable[str] = frozenset(), positional_paths=()
):
    """Convenience constructor for CanonicalLeaf in tests."""
    return CanonicalLeaf(
        verb=verb,
        subcommand=subcommand,
        flags=frozenset(flags),
        redirections=(),
        raw_leaf=verb,
        positional_paths=tuple(positional_paths),
    )


def _flags_json(flags):
    return json.dumps(sorted(flags), separators=(",", ":"))


# ---------------------------------------------------------------------------
# Literal form
# ---------------------------------------------------------------------------


def test_literal_form_plain_verb_no_paths():
    """Simple command with no paths → literal base + flags-wildcard."""
    leaf = _leaf("git", subcommand="status")
    variants = to_pattern_form(leaf, CTX_FULL)

    # Must include a fully-literal variant.
    assert (
        PatternVariant(verb="git", subcommand="status", flags="[]", path_spec="")
        in variants
    )


def test_literal_form_flags_are_minified_json():
    leaf = _leaf("git", subcommand="commit", flags={"-m", "--amend"})
    variants = to_pattern_form(leaf, CTX_FULL)
    flags_strs = {v.flags for v in variants}
    # Literal flags present as minified sorted JSON.
    assert _flags_json({"-m", "--amend"}) in flags_strs


def test_literal_form_no_positional_paths_gives_empty_string_path_spec():
    leaf = _leaf("git", subcommand="status")
    variants = to_pattern_form(leaf, CTX_FULL)
    literal = next(
        v for v in variants if v.flags != "*" and not (v.verb.startswith("$"))
    )
    assert literal.path_spec == ""


def test_literal_form_with_positional_paths_has_none_path_spec():
    leaf = _leaf("rm", positional_paths=(f"{PROJECT}/target",))
    variants = to_pattern_form(leaf, CTX_FULL)
    literal = next(v for v in variants if v.verb == "rm" and v.flags != "*")
    # Literal base for a leaf with paths has path_spec=None (any).
    assert literal.path_spec is None


# ---------------------------------------------------------------------------
# Verb pattern substitution
# ---------------------------------------------------------------------------


def test_verb_under_project_root_gets_project_root_pattern():
    venv_python = f"{PROJECT}/.venv/bin/python"
    leaf = _leaf(venv_python)
    variants = to_pattern_form(leaf, CTX_FULL)
    verb_patterns = {v.verb for v in variants}
    assert "$PROJECT_ROOT/.venv/bin/python" in verb_patterns


def test_verb_under_home_but_not_project_gets_home_pattern():
    tool = f"{HOME}/.local/bin/mytool"
    leaf = _leaf(tool)
    variants = to_pattern_form(leaf, CTX_FULL)
    verb_patterns = {v.verb for v in variants}
    assert "$HOME/.local/bin/mytool" in verb_patterns
    # Should not be under $PROJECT_ROOT (it's not under project).
    assert not any(v.startswith("$PROJECT_ROOT") for v in verb_patterns)


def test_verb_under_project_root_prefers_project_over_home():
    # project is a subdirectory of home; project_root prefix is longer.
    venv_python = f"{PROJECT}/.venv/bin/python"
    leaf = _leaf(venv_python)
    variants = to_pattern_form(leaf, CTX_FULL)
    # Should have $PROJECT_ROOT variant; $HOME variant is absent
    # (longest prefix wins — project_root match is chosen).
    verb_patterns = {v.verb for v in variants}
    assert "$PROJECT_ROOT/.venv/bin/python" in verb_patterns
    assert "$HOME/work/myproject/.venv/bin/python" not in verb_patterns


def test_verb_that_is_not_absolute_path_gets_no_var_substitution():
    leaf = _leaf("pytest", subcommand=None)
    variants = to_pattern_form(leaf, CTX_FULL)
    for v in variants:
        assert not v.verb.startswith("$"), f"Unexpected pattern verb: {v.verb}"


def test_empty_ctx_produces_only_literal_and_wildcard_variants():
    leaf = _leaf("git", subcommand="status")
    variants = to_pattern_form(leaf, CTX_EMPTY)
    # No $VAR substitution possible.
    for v in variants:
        assert "$" not in (v.verb or "")
        assert "$" not in (v.path_spec or "")


def test_verb_exact_match_on_project_root():
    leaf = _leaf(PROJECT)
    variants = to_pattern_form(leaf, CTX_FULL)
    verb_patterns = {v.verb for v in variants}
    assert "$PROJECT_ROOT" in verb_patterns


# ---------------------------------------------------------------------------
# Path spec variants
# ---------------------------------------------------------------------------


def test_path_under_project_emits_glob_and_specific_path_spec():
    path = f"{PROJECT}/src/module.py"
    leaf = _leaf("rm", positional_paths=(path,))
    variants = to_pattern_form(leaf, CTX_FULL)
    path_specs = {v.path_spec for v in variants}
    assert "$PROJECT_ROOT/**" in path_specs
    assert "$PROJECT_ROOT/src/module.py" in path_specs


def test_path_under_home_emits_home_path_spec():
    path = f"{HOME}/docs/notes.txt"
    leaf = _leaf("cat", positional_paths=(path,))
    variants = to_pattern_form(leaf, CTX_HOME_ONLY)
    path_specs = {v.path_spec for v in variants}
    assert "$HOME/**" in path_specs
    assert "$HOME/docs/notes.txt" in path_specs


def test_multiple_positional_paths_emit_multiple_path_specs():
    leaf = _leaf(
        "diff",
        positional_paths=(
            f"{PROJECT}/a.py",
            f"{PROJECT}/b.py",
        ),
    )
    variants = to_pattern_form(leaf, CTX_FULL)
    path_specs = {v.path_spec for v in variants if v.path_spec and "$" in v.path_spec}
    # Glob is shared; specific paths are distinct.
    assert "$PROJECT_ROOT/**" in path_specs
    assert "$PROJECT_ROOT/a.py" in path_specs
    assert "$PROJECT_ROOT/b.py" in path_specs


def test_positional_path_not_under_any_ctx_var_produces_no_path_spec():
    leaf = _leaf("cat", positional_paths=("/etc/hosts",))
    variants = to_pattern_form(leaf, CTX_FULL)
    # /etc/hosts is not under home or project; no $VAR path_spec.
    for v in variants:
        if v.path_spec:
            assert "$" not in v.path_spec, f"Unexpected $VAR path_spec: {v.path_spec}"


def test_relative_positional_path_produces_no_path_spec():
    leaf = _leaf("rm", positional_paths=("relative/path",))
    variants = to_pattern_form(leaf, CTX_FULL)
    for v in variants:
        if v.path_spec:
            assert "$" not in v.path_spec


def test_path_spec_variant_uses_best_verb():
    venv_python = f"{PROJECT}/.venv/bin/python"
    leaf = _leaf(venv_python, positional_paths=(f"{PROJECT}/script.py",))
    variants = to_pattern_form(leaf, CTX_FULL)
    # Path-spec variants should use the patterned verb, not the literal.
    path_spec_variants = [v for v in variants if v.path_spec and "$" in v.path_spec]
    for v in path_spec_variants:
        assert v.verb.startswith("$PROJECT_ROOT"), (
            f"Expected $PROJECT_ROOT verb on path-spec variant, got: {v.verb}"
        )


# ---------------------------------------------------------------------------
# Flags wildcard variant
# ---------------------------------------------------------------------------


def test_flags_wildcard_variant_is_always_present():
    leaf = _leaf("git", subcommand="status")
    variants = to_pattern_form(leaf, CTX_FULL)
    wildcard_variants = [v for v in variants if v.flags == "*"]
    assert len(wildcard_variants) >= 1


def test_flags_wildcard_uses_best_verb():
    venv_pytest = f"{PROJECT}/.venv/bin/pytest"
    leaf = _leaf(venv_pytest)
    variants = to_pattern_form(leaf, CTX_FULL)
    wildcard = next(v for v in variants if v.flags == "*")
    assert wildcard.verb.startswith("$PROJECT_ROOT")


def test_flags_wildcard_subcommand_is_preserved():
    leaf = _leaf("git", subcommand="commit", flags={"--amend"})
    variants = to_pattern_form(leaf, CTX_FULL)
    wildcard = next(v for v in variants if v.flags == "*")
    assert wildcard.subcommand == "commit"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def test_no_duplicate_variants_returned():
    leaf = _leaf("git", subcommand="status")
    variants = to_pattern_form(leaf, CTX_FULL)
    # Every variant must be unique.
    assert len(variants) == len(set(variants))


def test_no_duplicate_variants_with_path():
    leaf = _leaf("rm", positional_paths=(f"{PROJECT}/file.py",))
    variants = to_pattern_form(leaf, CTX_FULL)
    assert len(variants) == len(set(variants))


# ---------------------------------------------------------------------------
# Return-value structure
# ---------------------------------------------------------------------------


def test_all_returned_items_are_pattern_variant_instances():
    leaf = _leaf("git", subcommand="push", flags={"--force"})
    variants = to_pattern_form(leaf, CTX_FULL)
    for v in variants:
        assert isinstance(v, PatternVariant)


def test_literal_is_first_variant():
    leaf = _leaf("git", subcommand="status")
    variants = to_pattern_form(leaf, CTX_FULL)
    assert variants[0].verb == "git"
    assert variants[0].flags == "[]"


def test_subcommand_preserved_on_all_variants():
    leaf = _leaf("git", subcommand="push", flags={"--force"})
    variants = to_pattern_form(leaf, CTX_FULL)
    for v in variants:
        assert v.subcommand == "push"


# ---------------------------------------------------------------------------
# Review-detection markers (detected via field values, no extra fields)
# ---------------------------------------------------------------------------


def test_verb_pattern_detectable_via_dollar_prefix():
    leaf = _leaf(f"{PROJECT}/.venv/bin/python")
    variants = to_pattern_form(leaf, CTX_FULL)
    patterned = [v for v in variants if v.verb.startswith("$")]
    assert patterned, "Expected at least one verb-patterned variant"


def test_flags_wildcard_detectable_via_star_string():
    leaf = _leaf("git", subcommand="status")
    variants = to_pattern_form(leaf, CTX_FULL)
    wildcards = [v for v in variants if v.flags == "*"]
    assert wildcards, "Expected at least one flags-wildcard variant"


def test_path_pattern_detectable_via_dollar_in_path_spec():
    leaf = _leaf("rm", positional_paths=(f"{PROJECT}/target",))
    variants = to_pattern_form(leaf, CTX_FULL)
    path_patterned = [v for v in variants if v.path_spec and "$" in v.path_spec]
    assert path_patterned, "Expected at least one path-patterned variant"


# ---------------------------------------------------------------------------
# Doom-path: edge / zero / empty cases
# ---------------------------------------------------------------------------


def test_empty_positional_paths_tuple_gives_empty_string_path_spec_on_literal():
    leaf = _leaf("pwd")
    variants = to_pattern_form(leaf, CTX_FULL)
    literal = variants[0]
    assert literal.path_spec == ""


def test_empty_flags_literal_is_empty_json_array():
    leaf = _leaf("git", subcommand="status")
    variants = to_pattern_form(leaf, CTX_FULL)
    literal = variants[0]
    assert literal.flags == "[]"


def test_empty_ctx_does_not_crash():
    leaf = _leaf("git", subcommand="status", positional_paths=("/some/path",))
    variants = to_pattern_form(leaf, {})
    assert len(variants) >= 1  # at least literal


def test_none_ctx_values_are_ignored():
    ctx = {"home": "", "project_root": "", "cwd": ""}
    leaf = _leaf(f"{HOME}/.local/bin/tool")
    variants = to_pattern_form(leaf, ctx)
    # Empty strings in ctx should not produce $VAR patterns.
    for v in variants:
        assert not v.verb.startswith("$")


def test_ctx_with_trailing_slash_still_matches():
    ctx = {"home": HOME + "/", "project_root": PROJECT + "/"}
    leaf = _leaf(f"{PROJECT}/.venv/bin/python")
    variants = to_pattern_form(leaf, ctx)
    verb_patterns = {v.verb for v in variants}
    # Trailing slash in ctx path is stripped before comparison.
    assert "$PROJECT_ROOT/.venv/bin/python" in verb_patterns


def test_verb_equal_to_ctx_path_exactly():
    leaf = _leaf(HOME)
    variants = to_pattern_form(leaf, CTX_HOME_ONLY)
    verb_patterns = {v.verb for v in variants}
    assert "$HOME" in verb_patterns


def test_mixed_positionals_in_and_outside_ctx():
    """Paths inside ctx get $VAR path_specs; outside-ctx paths do not."""
    inside = f"{PROJECT}/src/main.py"
    outside = "/usr/lib/libfoo.so"
    leaf = _leaf("objdump", positional_paths=(inside, outside))
    variants = to_pattern_form(leaf, CTX_FULL)
    path_specs = {v.path_spec for v in variants}
    assert "$PROJECT_ROOT/src/main.py" in path_specs
    # /usr/lib path should not produce any $VAR path_spec.
    assert not any(ps and "$" in ps and "lib" in ps for ps in path_specs)
