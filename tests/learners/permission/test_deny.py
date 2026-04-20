"""Tests for learners.permission.deny."""
from __future__ import annotations

import pytest

from learners.permission.canonicalize import CanonicalLeaf, Redirection
from learners.permission import deny


@pytest.fixture(autouse=True)
def _reset_deny_cache():
    """Ensure each test re-reads the YAML config (cheap and safe)."""
    deny._reset_cache()
    yield
    deny._reset_cache()


def _leaf(
    verb: str,
    subcommand: str | None = None,
    flags: frozenset[str] = frozenset(),
    redirections: tuple[Redirection, ...] = (),
) -> CanonicalLeaf:
    return CanonicalLeaf(
        verb=verb,
        subcommand=subcommand,
        flags=flags,
        redirections=redirections,
        raw_leaf=f"{verb}",
    )


def test_rm_verb_is_denied():
    denied, reason = deny.is_denied(_leaf("rm", flags=frozenset({"-rf"})))
    assert denied
    assert reason is not None
    assert "rm" in reason


def test_git_push_subcommand_is_denied():
    denied, reason = deny.is_denied(_leaf("git", subcommand="push"))
    assert denied
    assert reason is not None
    assert "push" in reason


def test_git_status_is_not_denied():
    denied, reason = deny.is_denied(_leaf("git", subcommand="status"))
    assert not denied
    assert reason is None


def test_uv_pytest_is_not_denied_even_though_uv_publish_is():
    denied, _ = deny.is_denied(
        _leaf("uv", subcommand="pytest", flags=frozenset({"-q"}))
    )
    assert not denied


def test_uv_publish_is_denied():
    denied, _ = deny.is_denied(_leaf("uv", subcommand="publish"))
    assert denied


def test_sudo_is_always_denied():
    denied, reason = deny.is_denied(_leaf("sudo"))
    assert denied
    assert reason is not None
    assert "sudo" in reason


def test_truncate_redirect_into_claude_dir_is_denied():
    leaf = _leaf(
        "echo",
        redirections=(Redirection(op=">", target="/home/steve/.claude/foo"),),
    )
    denied, reason = deny.is_denied(leaf)
    assert denied
    assert reason is not None
    assert "guarded" in reason


def test_append_redirect_into_tmp_is_allowed():
    # /tmp/log does not exist on most test runs; append is OK.
    leaf = _leaf(
        "echo",
        redirections=(Redirection(op=">>", target="/tmp/claude-permission-test-log"),),
    )
    denied, reason = deny.is_denied(leaf)
    assert not denied, reason


def test_truncate_redirect_into_existing_file_is_denied(tmp_path):
    target = tmp_path / "existing.txt"
    target.write_text("hi", encoding="utf-8")
    leaf = _leaf(
        "echo",
        redirections=(Redirection(op=">", target=str(target)),),
    )
    denied, reason = deny.is_denied(leaf)
    assert denied
    assert reason is not None
    assert "overwrite" in reason


def test_truncate_redirect_into_nonexistent_tmp_file_is_allowed(tmp_path):
    # Write-to-nonexistent is safe (it's a create).
    target = tmp_path / "does-not-exist.txt"
    leaf = _leaf(
        "echo",
        redirections=(Redirection(op=">", target=str(target)),),
    )
    denied, _ = deny.is_denied(leaf)
    assert not denied


def test_git_commit_with_force_flag_is_denied():
    leaf = _leaf("git", subcommand="commit", flags=frozenset({"--force"}))
    denied, reason = deny.is_denied(leaf)
    assert denied
    assert reason is not None
    assert "--force" in reason


def test_guarded_prefix_covers_cache_claude():
    leaf = _leaf(
        "echo",
        redirections=(Redirection(op=">", target="/home/steve/.cache/claude/x"),),
    )
    denied, _ = deny.is_denied(leaf)
    assert denied
