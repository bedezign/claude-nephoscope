"""Tests for learners.permission.canonicalize — parse_command + to_pattern_form."""

from __future__ import annotations

import json
from collections.abc import Iterable


from nephoscope.learners.permission.canonicalize import (
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


def test_task_runner_pair_without_target_falls_through_to_default_subcommand():
    # "npm run" — the pair ("npm", "run") is in TASK_RUNNERS but there is no
    # third word.  The previous HEAD fell through to the default branch which
    # returns subcommand="run"; the SonarQube refactor early-returned (None, 2)
    # instead, producing subcommand=None.  Regression guard.
    leaves = parse_command("npm run")
    assert len(leaves) == 1
    assert leaves[0].verb == "npm"
    assert leaves[0].subcommand == "run"


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


def test_relative_positional_path_without_cwd_produces_no_path_spec():
    """When ctx has no cwd, relative paths yield no $VAR path_spec (original behaviour)."""
    leaf = _leaf("rm", positional_paths=("relative/path",))
    variants = to_pattern_form(leaf, CTX_HOME_ONLY)  # home only — no cwd
    for v in variants:
        if v.path_spec:
            assert "$" not in v.path_spec


def test_path_spec_variant_uses_best_verb():
    venv_python = f"{PROJECT}/.venv/bin/python"
    leaf = _leaf(venv_python, positional_paths=(f"{PROJECT}/script.py",))
    variants = to_pattern_form(leaf, CTX_FULL)
    # Per-verb path-spec variants (verb != "*") should use the patterned verb.
    path_spec_variants = [
        v for v in variants if v.path_spec and "$" in v.path_spec and v.verb != "*"
    ]
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


# ===========================================================================
# to_pattern_form — additional_dirs (Batch 4)
# ===========================================================================

EXTRA_DIR = "/opt/company/shared"
EXTRA_DIR_2 = "/mnt/datasets"

# Full ctx identical to CTX_FULL defined above (repeated here for locality).
_CTX_B4 = {"home": HOME, "project_root": PROJECT, "cwd": CWD}


def test_path_under_additional_dir_emits_inline_glob_and_specific():
    """A positional under an additional_dir → <dir>/** + <dir>/<tail>, no $VAR."""
    path = EXTRA_DIR + "/build/output.bin"
    leaf = _leaf("rm", positional_paths=(path,))
    variants = to_pattern_form(leaf, _CTX_B4, [EXTRA_DIR])
    path_specs = {v.path_spec for v in variants}
    assert EXTRA_DIR + "/**" in path_specs
    assert EXTRA_DIR + "/build/output.bin" in path_specs
    # No $VAR placeholder in any of the extra-dir specs.
    extra_specs = {ps for ps in path_specs if ps and EXTRA_DIR in ps}
    assert not any("$" in ps for ps in extra_specs)


def test_path_not_under_any_additional_dir_falls_back_to_current_behaviour():
    """A path outside both ctx and additional_dirs → no extra path_spec emitted."""
    path = "/usr/local/lib/libfoo.so"
    leaf = _leaf("cat", positional_paths=(path,))
    variants = to_pattern_form(leaf, _CTX_B4, [EXTRA_DIR])
    path_specs = {v.path_spec for v in variants if v.path_spec}
    # No entry containing a path_spec for the /usr/local path.
    assert not any("lib" in (ps or "") for ps in path_specs)


def test_ctx_var_prefix_beats_additional_dir_when_path_under_both():
    """$PROJECT_ROOT takes priority over an additional_dir that overlaps it."""
    # Make extra dir a parent of project, so the path is under both.
    shared_parent = "/home/alice"  # same as HOME — overlaps with ctx home key
    path = PROJECT + "/src/main.py"
    leaf = _leaf("cat", positional_paths=(path,))
    # additional_dirs includes HOME prefix, but ctx-var match must win.
    variants = to_pattern_form(leaf, _CTX_B4, [shared_parent])
    path_specs = {v.path_spec for v in variants}
    # $PROJECT_ROOT variant must be present.
    assert "$PROJECT_ROOT/**" in path_specs
    assert "$PROJECT_ROOT/src/main.py" in path_specs
    # No inline absolute path_spec for the shared_parent — ctx wins.
    assert not any(
        ps and ps.startswith(shared_parent) and "$" not in ps for ps in path_specs
    )


def test_additional_dirs_empty_list_no_change_from_pre_batch4_behaviour():
    """Passing an empty additional_dirs list behaves identically to omitting it."""
    path = f"{PROJECT}/target"
    leaf = _leaf("rm", positional_paths=(path,))
    without = to_pattern_form(leaf, _CTX_B4)
    with_empty = to_pattern_form(leaf, _CTX_B4, [])
    assert without == with_empty


def test_additional_dirs_none_no_change_from_pre_batch4_behaviour():
    """Passing additional_dirs=None behaves identically to the default."""
    leaf = _leaf("git", subcommand="status")
    without = to_pattern_form(leaf, _CTX_B4)
    with_none = to_pattern_form(leaf, _CTX_B4, None)
    assert without == with_none


def test_additional_dir_with_trailing_slash_still_matches():
    """Trailing slashes on additional_dir entries are stripped before matching."""
    path = EXTRA_DIR + "/data/x.csv"
    leaf = _leaf("cat", positional_paths=(path,))
    variants = to_pattern_form(leaf, _CTX_B4, [EXTRA_DIR + "/"])
    path_specs = {v.path_spec for v in variants}
    assert EXTRA_DIR + "/**" in path_specs
    assert EXTRA_DIR + "/data/x.csv" in path_specs


def test_additional_dir_without_trailing_slash_still_matches():
    """Entries without trailing slashes also match correctly (no double-slash)."""
    path = EXTRA_DIR + "/data/x.csv"
    leaf = _leaf("cat", positional_paths=(path,))
    variants = to_pattern_form(leaf, _CTX_B4, [EXTRA_DIR])
    path_specs = {v.path_spec for v in variants}
    # Must not produce double-slash artifacts.
    assert not any("//" in (ps or "") for ps in path_specs)
    assert EXTRA_DIR + "/data/x.csv" in path_specs


def test_additional_dir_that_does_not_exist_on_disk_is_tolerated():
    """Non-existent additional_dirs are accepted — matching is purely lexicographic."""
    nonexistent = "/nonexistent/vanished/dir"
    path = nonexistent + "/file.txt"
    leaf = _leaf("rm", positional_paths=(path,))
    # Must not raise; matching works normally.
    variants = to_pattern_form(leaf, _CTX_B4, [nonexistent])
    path_specs = {v.path_spec for v in variants}
    assert nonexistent + "/**" in path_specs
    assert nonexistent + "/file.txt" in path_specs


def test_multiple_additional_dirs_first_match_wins():
    """When multiple additional_dirs match, only the first matching one emits specs."""
    path = EXTRA_DIR + "/subdir/file.bin"
    leaf = _leaf("cat", positional_paths=(path,))
    # EXTRA_DIR matches; EXTRA_DIR_2 does not.
    variants = to_pattern_form(leaf, _CTX_B4, [EXTRA_DIR, EXTRA_DIR_2])
    path_specs = {v.path_spec for v in variants}
    assert EXTRA_DIR + "/**" in path_specs
    # EXTRA_DIR_2 specs must not appear.
    assert not any((ps or "").startswith(EXTRA_DIR_2) for ps in path_specs)


def test_path_exactly_equal_to_additional_dir_emits_glob_only():
    """A path that equals an additional_dir exactly → only <dir>/** (no specific)."""
    path = EXTRA_DIR  # exact match
    leaf = _leaf("ls", positional_paths=(path,))
    variants = to_pattern_form(leaf, _CTX_B4, [EXTRA_DIR])
    path_specs = {v.path_spec for v in variants}
    assert EXTRA_DIR + "/**" in path_specs
    # The specific form is just the dir itself, not <dir>/<tail> — not emitted.
    assert EXTRA_DIR not in path_specs  # exact dir is not a "tail" entry


def test_no_duplicate_variants_with_additional_dirs():
    """Deduplication holds when additional_dirs are in play."""
    path = EXTRA_DIR + "/file.py"
    leaf = _leaf("rm", positional_paths=(path, path))  # same path twice
    variants = to_pattern_form(leaf, _CTX_B4, [EXTRA_DIR])
    assert len(variants) == len(set(variants))


def test_additional_dirs_produces_no_dollar_in_path_spec():
    """Inline absolute path-specs never contain a $ prefix."""
    path = EXTRA_DIR + "/build/out"
    leaf = _leaf("ls", positional_paths=(path,))
    variants = to_pattern_form(leaf, _CTX_B4, [EXTRA_DIR])
    extra_specs = [
        v.path_spec
        for v in variants
        if v.path_spec and EXTRA_DIR in (v.path_spec or "")
    ]
    assert extra_specs, "Expected at least one additional-dir path_spec"
    for ps in extra_specs:
        assert "$" not in ps, f"Unexpected $ in inline absolute path_spec: {ps}"


# ===========================================================================
# to_pattern_form — wildcard-verb variant (Phase B14)
# ===========================================================================


def test_wildcard_verb_variant_emitted_for_path_under_ctx_var():
    """to_pattern_form emits a verb="*" variant for each distinct path_spec
    when the leaf has positional paths under a ctx variable."""
    path = f"{PROJECT}/src/secrets.py"
    leaf = _leaf("cat", positional_paths=(path,))
    variants = to_pattern_form(leaf, CTX_FULL)

    wildcard_verb_variants = [
        v for v in variants if v.verb == "*" and v.path_spec and "$" in v.path_spec
    ]
    assert wildcard_verb_variants, 'Expected at least one verb="*" path-spec variant'

    # Must have a glob form and a specific form.
    path_specs_of_wildcards = {v.path_spec for v in wildcard_verb_variants}
    assert "$PROJECT_ROOT/**" in path_specs_of_wildcards
    assert "$PROJECT_ROOT/src/secrets.py" in path_specs_of_wildcards


def test_wildcard_verb_variant_shape_fields():
    """verb="*" variant has subcommand=None and flags="*"."""
    path = f"{HOME}/.aws/credentials"
    leaf = _leaf("cat", positional_paths=(path,))
    variants = to_pattern_form(leaf, CTX_HOME_ONLY)

    wildcard_verb_variants = [v for v in variants if v.verb == "*"]
    assert wildcard_verb_variants, 'Expected at least one verb="*" variant'
    for v in wildcard_verb_variants:
        assert v.subcommand is None, (
            f"Expected subcommand=None on wildcard variant, got {v.subcommand!r}"
        )
        assert v.flags == "*", (
            f'Expected flags="*" on wildcard variant, got {v.flags!r}'
        )


def test_wildcard_verb_variant_not_emitted_when_no_positional_paths():
    """No wildcard-verb variant when the leaf has no positional paths."""
    leaf = _leaf("git", subcommand="status")
    variants = to_pattern_form(leaf, CTX_FULL)
    wildcard_verb_variants = [v for v in variants if v.verb == "*"]
    assert not wildcard_verb_variants, (
        f'Expected no verb="*" variants for leaf with no paths, got {wildcard_verb_variants!r}'
    )


def test_wildcard_verb_variant_not_emitted_when_paths_outside_ctx():
    """No wildcard-verb variant for positional paths not under any ctx var."""
    leaf = _leaf("cat", positional_paths=("/etc/hosts",))
    variants = to_pattern_form(leaf, CTX_FULL)
    wildcard_verb_variants = [
        v for v in variants if v.verb == "*" and v.path_spec and "$" in v.path_spec
    ]
    assert not wildcard_verb_variants, (
        f"Expected no $VAR wildcard-verb variants for outside-ctx path, got {wildcard_verb_variants!r}"
    )


def test_wildcard_verb_variant_ordering_after_per_verb_path_spec_variants():
    """verb="*" variants appear AFTER the corresponding per-verb path-spec variants."""
    path = f"{HOME}/.aws/credentials"
    leaf = _leaf("cat", positional_paths=(path,))
    variants = to_pattern_form(leaf, CTX_HOME_ONLY)

    # Find indices of per-verb path-spec variants and wildcard-verb variants.
    per_verb_path_indices = [
        i
        for i, v in enumerate(variants)
        if v.verb != "*" and v.path_spec and "$HOME" in (v.path_spec or "")
    ]
    wildcard_verb_indices = [i for i, v in enumerate(variants) if v.verb == "*"]
    assert per_verb_path_indices, "Expected per-verb path-spec variants"
    assert wildcard_verb_indices, "Expected wildcard-verb variants"
    assert max(per_verb_path_indices) < min(wildcard_verb_indices), (
        f"Wildcard-verb variants must come after per-verb path-spec variants. "
        f"Per-verb indices: {per_verb_path_indices}, wildcard indices: {wildcard_verb_indices}"
    )


def test_wildcard_verb_multiple_path_specs_each_emits_wildcard():
    """Each distinct path_spec from positionals gets its own verb="*" variant."""
    leaf = _leaf(
        "diff",
        positional_paths=(
            f"{HOME}/.aws/credentials",
            f"{HOME}/.kube/config",
        ),
    )
    variants = to_pattern_form(leaf, CTX_HOME_ONLY)

    wildcard_verb_path_specs = {
        v.path_spec for v in variants if v.verb == "*" and v.path_spec
    }
    # Should cover glob + both specific paths.
    assert "$HOME/**" in wildcard_verb_path_specs
    assert "$HOME/.aws/credentials" in wildcard_verb_path_specs
    assert "$HOME/.kube/config" in wildcard_verb_path_specs


def test_no_duplicate_variants_with_wildcard_verb():
    """Deduplication holds when wildcard-verb variants are present."""
    path = f"{PROJECT}/src/app.py"
    leaf = _leaf("cat", positional_paths=(path,))
    variants = to_pattern_form(leaf, CTX_FULL)
    assert len(variants) == len(set(variants))


def test_wildcard_verb_additional_dir_emits_inline_wildcard():
    """A positional under an additional_dir also gets a verb="*" variant."""
    extra_dir = "/opt/shared/tools"
    path = extra_dir + "/setup.sh"
    leaf = _leaf("cat", positional_paths=(path,))
    variants = to_pattern_form(leaf, CTX_EMPTY, [extra_dir])

    wildcard_verb_path_specs = {v.path_spec for v in variants if v.verb == "*"}
    assert extra_dir + "/**" in wildcard_verb_path_specs, (
        f"Expected wildcard-verb variant for additional_dir path, got {wildcard_verb_path_specs!r}"
    )


# ===========================================================================
# Integration boundary: additional_dirs flows from parse_command → to_pattern_form
# → rule_shape lookup → Verdict
#
# This exercises the full path:
#   parse_command produces a leaf with positional_paths under an additional_dir
#   to_pattern_form with additional_dirs produces an inline absolute path_spec
#   a rule_shape keyed on that path_spec produces an approved verdict via bash_match
# ===========================================================================


def test_tilde_in_additional_dirs_expands_to_absolute_and_matches():
    """A ``~/Downloads``-style entry is expanded to an absolute path before matching.

    Without ``expanduser``, the stored string is ``~/Downloads`` (literal tilde),
    and ``path.startswith("~/Downloads/")`` always fails because the canonicalized
    positional path is absolute (``/home/<user>/Downloads/...``).
    """
    import os

    downloads_abs = os.path.expanduser("~/Downloads")
    positional = os.path.join(downloads_abs, "foo.tar.gz")

    leaf = _leaf("cat", positional_paths=(positional,))
    # Pass the tilde form as the additional_dir — must still match.
    variants = to_pattern_form(leaf, {}, ["~/Downloads"])
    path_specs = {v.path_spec for v in variants}
    assert downloads_abs + "/**" in path_specs, (
        f"Expected {downloads_abs + '/**'!r} in {path_specs!r}"
    )
    assert downloads_abs + "/foo.tar.gz" in path_specs


def test_additional_dirs_inline_path_spec_round_trips_through_bash_match():
    """Full integration: additional_dir path → inline path_spec → bash_match Allow.

    1. ``parse_command`` extracts the positional path.
    2. ``to_pattern_form(leaf, ctx, [extra_dir])`` produces the inline glob.
    3. The inline glob is stored as a rule_shape path_spec.
    4. ``bash_match`` resolves the leaf against the DB and returns Allow.
    """
    import sqlite3 as _sqlite3

    from nephoscope.learners.permission.match.bash import match as _bash_match

    extra_dir = "/opt/shared/tools"
    cmd = f"ls {extra_dir}/scripts"

    # Parse the command.
    leaves = parse_command(cmd)
    assert leaves, "parse_command must return at least one leaf"
    ls_leaf = next(lf for lf in leaves if lf.verb == "ls")

    # Build variants with the additional_dir.
    ctx: dict[str, str] = {}  # no $HOME/$PROJECT_ROOT/$CWD on purpose
    variants = to_pattern_form(ls_leaf, ctx, [extra_dir])
    path_specs = {v.path_spec for v in variants}
    expected_glob = extra_dir + "/**"
    assert expected_glob in path_specs, (
        f"Expected {expected_glob!r} in path_specs, got: {path_specs}"
    )

    # Build an in-memory DB with the minimal schema needed.
    conn = _sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE rule_shapes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            verb TEXT NOT NULL,
            subcommand TEXT,
            flags TEXT NOT NULL DEFAULT '[]',
            path_spec TEXT,
            context TEXT NOT NULL DEFAULT 'any',
            created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_shape_id INTEGER NOT NULL,
            session_id INTEGER,
            project_id INTEGER,
            decision TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'test',
            created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE call_statuses (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE tools (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE permission_modes (id INTEGER PRIMARY KEY, name TEXT);
    """)

    # Insert a rule_shape for (ls, None, [], <extra_dir>/**) → approved.
    cur = conn.execute(
        "INSERT INTO rule_shapes (verb, subcommand, flags, path_spec, created_at)"
        " VALUES ('ls', NULL, '[]', ?, '2025-01-01Z');",
        (expected_glob,),
    )
    shape_id = cur.lastrowid
    conn.execute(
        "INSERT INTO permissions (rule_shape_id, session_id, project_id, decision)"
        " VALUES (?, NULL, NULL, 'approved');",
        (shape_id,),
    )
    conn.commit()

    # Patch lookup_permissions so it works without the full schema.
    import nephoscope.lib.db as _db

    _orig = _db.lookup_permissions

    def _lookup(c, sid, sess, proj):
        rows = c.execute(
            "SELECT decision FROM permissions"
            " WHERE rule_shape_id = ?"
            "   AND session_id IS ?"
            "   AND project_id IS ?;",
            (sid, sess, proj),
        ).fetchall()
        return [{"decision": r[0]} for r in rows]

    _db.lookup_permissions = _lookup
    try:
        verdict = _bash_match(
            "Bash",
            {"command": cmd},
            conn,
            None,
            None,
            ctx,
            [extra_dir],
        )
    finally:
        _db.lookup_permissions = _orig
        conn.close()

    from nephoscope.learners.permission.match._types import Verdict

    assert verdict == Verdict.Allow, f"Expected Allow, got {verdict}"


# ===========================================================================
# Phase 2 — is_substitution_child marker on CanonicalLeaf
# ===========================================================================


def test_standalone_op_read_is_not_substitution_child():
    """parse_command("op read 'op://x'") → one leaf, is_substitution_child=False."""
    leaves = parse_command("op read 'op://x'")
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf.verb == "op"
    assert leaf.is_substitution_child is False


def test_op_read_inside_command_substitution_is_substitution_child():
    """The inner `op read` inside $(...) gets is_substitution_child=True.

    parse_command('curl -H "Bearer $(op read \\'op://x\\')"') →
    outer curl with False, inner op read with True.
    """
    leaves = parse_command("curl -H \"Bearer $(op read 'op://x')\"")
    assert len(leaves) == 2

    curl_leaf = next(lf for lf in leaves if lf.verb == "curl")
    op_leaf = next(lf for lf in leaves if lf.verb == "op")

    assert curl_leaf.is_substitution_child is False
    assert op_leaf.is_substitution_child is True


def test_list_operator_commands_both_toplevel():
    """parse_command('op read ... && op read ...') → two leaves, both False."""
    leaves = parse_command("op read 'op://x' && op read 'op://y'")
    assert len(leaves) == 2
    for lf in leaves:
        assert lf.is_substitution_child is False


def test_nested_substitution_innermost_is_substitution_child():
    """Nested command substitution: innermost op read has is_substitution_child=True.

    parse_command('$(echo $(op read \\'op://x\\'))') →
    outer echo with True, inner op read with True.
    Both are reached via substitution recursion.
    """
    leaves = parse_command("$(echo $(op read 'op://x'))")
    # We expect at least the innermost op read.
    op_leaves = [lf for lf in leaves if lf.verb == "op"]
    assert op_leaves, "Expected at least one op leaf in nested substitution"
    for lf in op_leaves:
        assert lf.is_substitution_child is True, (
            f"Expected is_substitution_child=True on nested op leaf, got {lf.is_substitution_child!r}"
        )


def test_process_substitution_inner_command_is_substitution_child():
    """parse_command('diff <(op read ...)')  → inner op read has is_substitution_child=True."""
    leaves = parse_command("diff <(op read 'op://x')")
    op_leaves = [lf for lf in leaves if lf.verb == "op"]
    assert op_leaves, "Expected op leaf from process substitution"
    for lf in op_leaves:
        assert lf.is_substitution_child is True


def test_simple_commands_have_false_by_default():
    """Any parse_command result without substitution has is_substitution_child=False."""
    for cmd in ["git status", "ls -la", "echo hello", "uv run pytest"]:
        for lf in parse_command(cmd):
            assert lf.is_substitution_child is False, (
                f"Expected False for {cmd!r}, verb={lf.verb!r}"
            )


# ===========================================================================
# Phase 2 — PatternVariant.context field
# ===========================================================================


def test_pattern_variant_context_toplevel_for_nontsubstitution_leaf():
    """Variants for a top-level (non-substitution) leaf all carry context='toplevel'."""
    leaves = parse_command("op read 'op://x'")
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf.is_substitution_child is False

    variants = to_pattern_form(leaf, CTX_EMPTY)
    for v in variants:
        assert v.context == "toplevel", (
            f"Expected context='toplevel' on all variants for top-level leaf, got {v.context!r}"
        )


def test_pattern_variant_context_substitution_for_substitution_child():
    """Variants for a substitution-child leaf all carry context='substitution'."""
    leaves = parse_command("curl -H \"Bearer $(op read 'op://x')\"")
    op_leaf = next(lf for lf in leaves if lf.verb == "op")
    assert op_leaf.is_substitution_child is True

    variants = to_pattern_form(op_leaf, CTX_EMPTY)
    for v in variants:
        assert v.context == "substitution", (
            f"Expected context='substitution' on all variants for substitution-child leaf, "
            f"got {v.context!r}"
        )


# ===========================================================================
# Phase 2 — multi-word (two-word) subcommand resolution
#
# Some CLIs (vault, doppler) namespace commands as ``<verb> <group> <action>``
# where the second token is a subgroup, not a final subcommand. The
# canonicalizer must produce a single ``"<group> <action>"`` subcommand for
# these, so seed rules can match without the third token leaking into
# positional_paths.
# ===========================================================================


def test_two_word_subcommand_vault_kv_get():
    """vault kv get foo → subcommand='kv get', positional=('foo',)."""
    leaves = parse_command("vault kv get secret/foo")
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf.verb == "vault"
    assert leaf.subcommand == "kv get"
    assert leaf.positional_paths == ("secret/foo",)


def test_two_word_subcommand_vault_kv_put():
    """vault kv put foo=bar → subcommand='kv put'."""
    leaves = parse_command("vault kv put foo=bar")
    assert len(leaves) == 1
    assert leaves[0].verb == "vault"
    assert leaves[0].subcommand == "kv put"


def test_two_word_subcommand_vault_kv_no_third_token_falls_through():
    """vault kv -h → subcommand='kv' (third token is a flag, not a positional)."""
    leaves = parse_command("vault kv -h")
    assert len(leaves) == 1
    assert leaves[0].verb == "vault"
    # Third token is a flag, not a non-flag positional — fall through.
    assert leaves[0].subcommand == "kv"
    assert "-h" in leaves[0].flags


def test_two_word_subcommand_vault_kv_alone_falls_through():
    """vault kv → subcommand='kv' (only two tokens; default branch)."""
    leaves = parse_command("vault kv")
    assert len(leaves) == 1
    assert leaves[0].verb == "vault"
    assert leaves[0].subcommand == "kv"


def test_two_word_subcommand_vault_auth_list():
    """vault auth list → subcommand='auth list'."""
    leaves = parse_command("vault auth list")
    assert len(leaves) == 1
    assert leaves[0].verb == "vault"
    assert leaves[0].subcommand == "auth list"


def test_two_word_subcommand_doppler_secrets_get():
    """doppler secrets get KEY → subcommand='secrets get', positional=('KEY',)."""
    leaves = parse_command("doppler secrets get KEY")
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf.verb == "doppler"
    assert leaf.subcommand == "secrets get"
    assert leaf.positional_paths == ("KEY",)


def test_two_word_subcommand_does_not_break_single_word_vault():
    """vault read secret/foo (not in two-word allowlist) → subcommand='read'."""
    leaves = parse_command("vault read secret/foo")
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf.verb == "vault"
    # ('vault', 'read') is NOT in TWO_WORD_SUBCOMMAND_VERBS → default branch.
    assert leaf.subcommand == "read"
    assert leaf.positional_paths == ("secret/foo",)


def test_two_word_subcommand_third_token_is_flag_falls_through():
    """vault kv --help → subcommand='kv' (third token is a flag, not a positional)."""
    leaves = parse_command("vault kv --help")
    assert len(leaves) == 1
    assert leaves[0].verb == "vault"
    assert leaves[0].subcommand == "kv"
    assert "--help" in leaves[0].flags


def test_two_word_subcommand_third_token_is_substitution_falls_through():
    """vault kv $(echo get) → subcommand='kv' (third token is substitution)."""
    leaves = parse_command("vault kv $(echo get)")
    vault_leaves = [lf for lf in leaves if lf.verb == "vault"]
    assert len(vault_leaves) == 1
    # Third token starts with $( — _is_positional_subcommand returns False.
    assert vault_leaves[0].subcommand == "kv"


# ===========================================================================
# B14 — relative-path resolution against $CWD
# ===========================================================================

# Context constants for B14 tests.
# _B14_CWD is a subdirectory of _B14_PROJECT so that $CWD wins (longer path).
_B14_HOME = "/home/alice"
_B14_PROJECT = "/home/alice/work"
_B14_CWD = "/home/alice/work/myproject"
_CTX_B14_FULL = {"home": _B14_HOME, "project_root": _B14_PROJECT, "cwd": _B14_CWD}
# CWD-only context — no project_root, so $CWD is the only prefix.
_CTX_B14_CWD_ONLY = {"cwd": _B14_CWD}
_CTX_B14_EMPTY: dict[str, str] = {}


def test_relative_env_file_with_cwd_emits_cwd_path_specs():
    """parse_command("cat .env") with cwd in ctx → $CWD/.env and $CWD/**/.env variants.

    Uses a CWD-only context so that $CWD is the longest (only) prefix and wins.
    """
    leaf = _leaf("cat", positional_paths=(".env",))
    variants = to_pattern_form(leaf, _CTX_B14_CWD_ONLY)
    path_specs = {v.path_spec for v in variants}

    # Must emit the specific path and the basename-glob.
    assert "$CWD/.env" in path_specs, f"Expected $CWD/.env in {path_specs!r}"
    assert "$CWD/**/.env" in path_specs, f"Expected $CWD/**/.env in {path_specs!r}"


def test_relative_nested_file_with_cwd_emits_expected_path_specs():
    """parse_command("cat src/foo.txt") with cwd → $CWD/src/foo.txt and $CWD/**/foo.txt.

    Uses a CWD-only context so that $CWD is the only prefix.
    """
    leaf = _leaf("cat", positional_paths=("src/foo.txt",))
    variants = to_pattern_form(leaf, _CTX_B14_CWD_ONLY)
    path_specs = {v.path_spec for v in variants}

    assert "$CWD/src/foo.txt" in path_specs, (
        f"Expected $CWD/src/foo.txt in {path_specs!r}"
    )
    assert "$CWD/**" in path_specs, f"Expected $CWD/** in {path_specs!r}"
    assert "$CWD/**/foo.txt" in path_specs, (
        f"Expected $CWD/**/foo.txt in {path_specs!r}"
    )


def test_relative_env_file_without_cwd_produces_no_dollar_path_spec():
    """cat .env with EMPTY ctx (no cwd) → no $VAR path_spec variants (old behaviour preserved)."""
    leaf = _leaf("cat", positional_paths=(".env",))
    variants = to_pattern_form(leaf, _CTX_B14_EMPTY)
    for v in variants:
        assert "$" not in (v.path_spec or ""), (
            f"Expected no $VAR path_spec with empty ctx, got {v.path_spec!r}"
        )


# ===========================================================================
# B14 — basename-glob variant ($VAR/**/<basename>)
# ===========================================================================


def test_absolute_path_under_project_emits_basename_glob():
    """Absolute path /work/proj/apps/web/.env → $PROJECT_ROOT/**/.env emitted."""
    path = f"{_B14_PROJECT}/apps/web/.env"
    leaf = _leaf("cat", positional_paths=(path,))
    variants = to_pattern_form(leaf, _CTX_B14_FULL)
    path_specs = {v.path_spec for v in variants}

    # Existing: glob + specific.
    assert "$PROJECT_ROOT/**" in path_specs
    assert "$PROJECT_ROOT/apps/web/.env" in path_specs
    # New: basename-glob.
    assert "$PROJECT_ROOT/**/.env" in path_specs, (
        f"Expected $PROJECT_ROOT/**/.env in {path_specs!r}"
    )


def test_path_exactly_equal_to_ctx_var_emits_no_basename_glob():
    """cat <ctx_var_base> → only $VAR/**, no basename-glob (no tail) for that var.

    Uses a single-ctx-var context so other vars don't introduce basename-globs
    via a longer-than-base subtail match.
    """
    path = _B14_PROJECT  # exact match: path == base
    leaf = _leaf("ls", positional_paths=(path,))
    variants = to_pattern_form(leaf, {"project_root": _B14_PROJECT})
    path_specs = {v.path_spec for v in variants}

    # Glob-only for exact match against the only ctx-var.
    assert "$PROJECT_ROOT/**" in path_specs
    # No basename-glob because there is no tail under $PROJECT_ROOT.
    basename_globs = [
        ps for ps in path_specs if ps and ps.startswith("$") and "/**/" in ps
    ]
    assert not basename_globs, (
        f"Expected no basename-glob for exact match, got {basename_globs!r}"
    )


def test_existing_glob_and_specific_still_emitted_alongside_basename_glob():
    """Both $VAR/** and $VAR/tail still present when basename-glob is added."""
    path = f"{_B14_PROJECT}/src/module.py"
    leaf = _leaf("rm", positional_paths=(path,))
    variants = to_pattern_form(leaf, _CTX_B14_FULL)
    path_specs = {v.path_spec for v in variants}

    assert "$PROJECT_ROOT/**" in path_specs
    assert "$PROJECT_ROOT/src/module.py" in path_specs
    assert "$PROJECT_ROOT/**/module.py" in path_specs
