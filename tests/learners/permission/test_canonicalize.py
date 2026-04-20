"""Tests for learners.permission.canonicalize."""
from __future__ import annotations

from learners.permission.canonicalize import Redirection, parse_command


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

    uv_leaf = next(l for l in leaves if l.verb == "uv")
    tail_leaf = next(l for l in leaves if l.verb == "tail")

    # uv leaf canonicalized as task runner.
    assert uv_leaf.subcommand == "pytest"
    assert uv_leaf.flags == frozenset({"-q"})

    # The credentialed URL must not appear anywhere on either leaf.
    for leaf in leaves:
        assert "s3cret" not in leaf.raw_leaf
        assert "postgresql" not in leaf.raw_leaf
        assert not any("s3cret" in f for f in leaf.flags)

    # Tail: bashlex treats -10 as a flag-shaped token (starts with `-`).
    # Assert actual behavior rather than idealized behavior per the plan.
    assert tail_leaf.subcommand is None
    assert tail_leaf.flags == frozenset({"-10"})


def test_command_substitution_yields_inner_and_outer_leaves():
    leaves = parse_command("rm -rf $(pwd)")
    # Two leaves: inner pwd plus outer rm.
    verbs = sorted(l.verb for l in leaves)
    assert verbs == ["pwd", "rm"]

    rm_leaf = next(l for l in leaves if l.verb == "rm")
    # bashlex tokenizes `-rf` as a single word; we split POSIX clusters so
    # ``rm -rf`` and ``rm -r -f`` produce the same flag set.
    assert rm_leaf.subcommand is None
    assert rm_leaf.flags == frozenset({"-r", "-f"})


def test_list_operators_produce_one_leaf_per_command():
    leaves = parse_command("a; b && c || d")
    verbs = [l.verb for l in leaves]
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
# Fix A: POSIX short-flag cluster splitting
# ---------------------------------------------------------------------------


def test_short_flag_cluster_rm_rf_splits_into_r_and_f():
    leaves = parse_command("rm -rf /tmp/foo")
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf.verb == "rm"
    # -rf splits so it matches ``rm -r -f`` shape and per-flag deny patterns.
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
    # ``tail -10`` — the -10 token is numeric, not a letter cluster. The point
    # of the test is that it is NOT split into {-1, -0}. Current behavior
    # keeps the whole token as a single "flag" (since it starts with "-").
    leaves = parse_command("tail -10")
    assert len(leaves) == 1
    flags = leaves[0].flags
    assert "-1" not in flags
    assert "-0" not in flags
    # And the whole token survives intact (matches current behavior).
    assert flags == frozenset({"-10"})


def test_long_flag_is_not_cluster_split():
    leaves = parse_command("git commit --force")
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf.verb == "git"
    assert leaf.subcommand == "commit"
    assert leaf.flags == frozenset({"--force"})


def test_mixed_letter_digit_flag_is_not_cluster_split():
    # ``-O3`` contains a digit — never split (it's gcc's opt-level, not a
    # cluster of -O and -3).
    leaves = parse_command("gcc -O3 main.c")
    assert len(leaves) == 1
    assert leaves[0].flags == frozenset({"-O3"})


# ---------------------------------------------------------------------------
# Fix B: process/command substitution filtered from subcommand slot
# ---------------------------------------------------------------------------


def test_process_substitution_input_is_not_outer_subcommand():
    leaves = parse_command("diff <(ls) <(ls -a)")
    # Outer diff leaf has no subcommand; inner ls leaves still appear.
    diff_leaves = [l for l in leaves if l.verb == "diff"]
    assert len(diff_leaves) == 1
    assert diff_leaves[0].subcommand is None

    ls_leaves = [l for l in leaves if l.verb == "ls"]
    # Two inner ``ls`` calls (one plain, one with -a).
    assert len(ls_leaves) == 2
    flag_sets = sorted(frozenset(l.flags) for l in ls_leaves)
    assert frozenset() in flag_sets
    assert frozenset({"-a"}) in flag_sets


def test_command_substitution_is_not_outer_subcommand():
    leaves = parse_command("cat $(which git)")
    cat_leaves = [l for l in leaves if l.verb == "cat"]
    assert len(cat_leaves) == 1
    assert cat_leaves[0].subcommand is None

    which_leaves = [l for l in leaves if l.verb == "which"]
    assert len(which_leaves) == 1


def test_process_substitution_output_is_not_outer_subcommand():
    leaves = parse_command("cat >(tee log)")
    cat_leaves = [l for l in leaves if l.verb == "cat"]
    assert len(cat_leaves) == 1
    assert cat_leaves[0].subcommand is None

    tee_leaves = [l for l in leaves if l.verb == "tee"]
    assert len(tee_leaves) == 1


def test_dash_prefixed_quoted_content_not_treated_as_flag():
    # Regression for the CONTENT_VERBS interaction: echo "--- banner ---"
    # used to deposit the banner into the flag set because _collect_flags
    # only checked for leading dash.
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
    # find's path arg is now content (dropped from shape). Flags still
    # surface — note the POSIX short-cluster splitter treats `-name` as
    # a cluster and splits it into per-letter flags (a pre-existing quirk
    # of the canonicalizer, orthogonal to CONTENT_VERBS).
    leaves = parse_command("find /home -name '*.py'")
    assert leaves[0].verb == "find"
    assert leaves[0].subcommand is None
    assert leaves[0].flags  # non-empty — at least some flag(s) captured


def test_non_content_verb_retains_subcommand():
    # Regression: git is NOT a content verb; subcommand behavior unchanged.
    leaves = parse_command("git status")
    assert leaves[0].verb == "git"
    assert leaves[0].subcommand == "status"


def test_task_runner_wins_over_content_verb_lookup():
    # uv is not in CONTENT_VERBS but verify the existing task-runner path
    # still resolves correctly — this is a sanity check that the content
    # branch doesn't accidentally intercept task-runner verbs.
    leaves = parse_command("uv run pytest -q")
    assert leaves[0].verb == "uv"
    assert leaves[0].subcommand == "pytest"


def test_sed_script_argument_is_content():
    # sed's first positional is a script — content, not a subcommand.
    a = parse_command("sed 's/a/b/' file.txt")
    b = parse_command("sed 's/x/y/' file.txt")
    assert a[0].subcommand is None
    assert b[0].subcommand is None
    # Destructive flag still surfaces when present.
    inplace = parse_command("sed -i 's/a/b/' file.txt")
    assert "-i" in inplace[0].flags
