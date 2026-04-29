"""Tests for script_runner behaviour in parse_command / to_pattern_form.

The global patch_verb_categories autouse fixture (conftest.py) already
injects the full verb set — including script_runner: {python3, python,
bash, sh, node, deno} — so all tests here call parse_command directly
without any additional monkeypatching.
"""

from __future__ import annotations

from nephoscope.learners.permission.canonicalize import parse_command, to_pattern_form


class TestScriptRunnerCanonicalLeaf:
    def test_python3_script_subcommand_is_none(self) -> None:
        leaves = parse_command("python3 /tmp/script.py")
        assert len(leaves) == 1
        assert leaves[0].subcommand is None

    def test_python3_script_path_in_positional_paths(self) -> None:
        leaves = parse_command("python3 /tmp/script.py")
        assert len(leaves) == 1
        assert "/tmp/script.py" in leaves[0].positional_paths

    def test_python_bare_also_routes_script_path(self) -> None:
        leaves = parse_command("python /home/user/script.py --verbose")
        assert len(leaves) == 1
        assert leaves[0].subcommand is None
        assert "/home/user/script.py" in leaves[0].positional_paths

    def test_bash_script_path_becomes_positional(self) -> None:
        leaves = parse_command("bash /opt/deploy.sh -x")
        assert len(leaves) == 1
        assert leaves[0].subcommand is None
        assert "/opt/deploy.sh" in leaves[0].positional_paths

    def test_sh_script_path_becomes_positional(self) -> None:
        leaves = parse_command("sh /tmp/run.sh")
        assert len(leaves) == 1
        assert leaves[0].subcommand is None

    def test_node_script_path_becomes_positional(self) -> None:
        leaves = parse_command("node /app/server.js")
        assert len(leaves) == 1
        assert leaves[0].subcommand is None
        assert "/app/server.js" in leaves[0].positional_paths

    def test_python3_no_script_path_subcommand_still_none(self) -> None:
        leaves = parse_command("python3")
        assert len(leaves) == 1
        assert leaves[0].subcommand is None
        assert leaves[0].positional_paths == ()

    def test_git_not_a_script_runner_keeps_subcommand(self) -> None:
        leaves = parse_command("git commit -m 'msg'")
        assert len(leaves) == 1
        assert leaves[0].subcommand == "commit"

    def test_script_runner_to_pattern_form_emits_home_path_spec(self) -> None:
        leaves = parse_command("python3 /home/user/.claude/hooks/foo.py")
        assert len(leaves) == 1
        leaf = leaves[0]
        variants = to_pattern_form(leaf, {"home": "/home/user"})
        path_specs = [
            v.path_spec for v in variants if v.path_spec and "$" in v.path_spec
        ]
        assert any("$HOME" in ps for ps in path_specs), (
            f"Expected a $HOME path_spec variant; got path_specs={path_specs!r}"
        )

    def test_script_runner_with_claude_dir_ctx_emits_claude_dir_path_spec(
        self,
    ) -> None:
        leaves = parse_command("python3 /home/user/.claude/hooks/foo.py")
        assert len(leaves) == 1
        leaf = leaves[0]
        variants = to_pattern_form(
            leaf,
            {"home": "/home/user", "claude_dir": "/home/user/.claude"},
        )
        matching = [
            v
            for v in variants
            if v.path_spec is not None
            and "$CLAUDE_DIR" in v.path_spec
            and v.subcommand is None
        ]
        assert matching, (
            f"Expected a $CLAUDE_DIR path_spec variant with subcommand=None; "
            f"got variants={variants!r}"
        )
