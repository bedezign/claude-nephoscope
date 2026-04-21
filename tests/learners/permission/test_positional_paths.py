"""Tests for CanonicalLeaf.positional_paths extraction."""

from __future__ import annotations

from learners.permission.canonicalize import parse_command


def _leaf(cmd: str):
    leaves = parse_command(cmd)
    assert len(leaves) == 1, f"expected 1 leaf, got {len(leaves)}: {leaves}"
    return leaves[0]


def test_rm_with_single_path_captured():
    leaf = _leaf("rm /tmp/foo")
    assert leaf.positional_paths == ("/tmp/foo",)


def test_rm_with_flags_and_path_only_keeps_path():
    leaf = _leaf("rm -rf /tmp/foo")
    assert leaf.positional_paths == ("/tmp/foo",)


def test_mv_with_two_paths():
    leaf = _leaf("mv /a /b")
    assert leaf.positional_paths == ("/a", "/b")


def test_content_verb_drops_first_positional_but_keeps_rest():
    # cat is a CONTENT_VERB: the first positional is subcommand-slot
    # content, which _resolve_subcommand discards. Remaining positionals
    # start from index 1 onwards.
    leaf = _leaf("cat /etc/hosts /etc/passwd")
    # With CONTENT_VERBS, positional_start=1, so /etc/hosts (words[1]) and
    # /etc/passwd (words[2]) both contribute. First positional is "dropped"
    # as subcommand only, not from the positional slice.
    assert leaf.positional_paths == ("/etc/hosts", "/etc/passwd")


def test_no_positional_paths_when_only_flags():
    leaf = _leaf("ls -la")
    assert leaf.positional_paths == ()


def test_substitution_not_captured_as_path():
    # `rm $(pwd) /tmp/foo` yields two leaves: the inner pwd, and the outer
    # rm. On the outer rm, $(pwd) starts with `$(` so it's skipped; the
    # literal /tmp/foo is kept.
    leaves = parse_command("rm $(pwd) /tmp/foo")
    outer = next(leaf for leaf in leaves if leaf.verb == "rm")
    assert "/tmp/foo" in outer.positional_paths
    assert not any(p.startswith("$(") for p in outer.positional_paths)


def test_subcommand_not_captured_as_path():
    # git has no TASK_RUNNERS/CONTENT_VERBS entry, so the first non-flag
    # positional is subcommand. positional_paths starts after.
    leaf = _leaf("git push origin main")
    assert leaf.subcommand == "push"
    assert leaf.positional_paths == ("origin", "main")


def test_task_runner_target_not_captured_as_path():
    # uv run pytest -v: "pytest" becomes subcommand, -v is a flag; no paths.
    leaf = _leaf("uv run pytest -v")
    assert leaf.subcommand == "pytest"
    assert leaf.positional_paths == ()


def test_multiple_leaves_each_track_own_paths():
    leaves = parse_command("rm /a; mv /b /c")
    assert len(leaves) == 2
    rm_leaf = next(leaf for leaf in leaves if leaf.verb == "rm")
    mv_leaf = next(leaf for leaf in leaves if leaf.verb == "mv")
    assert rm_leaf.positional_paths == ("/a",)
    assert mv_leaf.positional_paths == ("/b", "/c")
