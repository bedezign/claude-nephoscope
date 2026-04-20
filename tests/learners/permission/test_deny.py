"""Tests for learners.permission.deny (evaluate + is_denied wrapper)."""
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


# --- Hard deny tier ---

def test_sudo_is_always_denied():
    decision, reason = deny.evaluate(_leaf("sudo"))
    assert decision == "deny"
    assert reason is not None
    assert "sudo" in reason


def test_mkfs_verb_is_denied():
    decision, reason = deny.evaluate(_leaf("mkfs"))
    assert decision == "deny"
    assert reason is not None
    assert "mkfs" in reason


def test_dd_verb_is_denied():
    decision, _ = deny.evaluate(_leaf("dd"))
    assert decision == "deny"


def test_shutdown_verb_is_denied():
    decision, _ = deny.evaluate(_leaf("shutdown"))
    assert decision == "deny"


def test_systemctl_reboot_is_denied():
    decision, reason = deny.evaluate(_leaf("systemctl", subcommand="reboot"))
    assert decision == "deny"
    assert reason is not None
    assert "reboot" in reason


def test_truncate_redirect_into_claude_dir_is_denied():
    leaf = _leaf(
        "echo",
        redirections=(Redirection(op=">", target="/home/steve/.claude/foo"),),
    )
    decision, reason = deny.evaluate(leaf)
    assert decision == "deny"
    assert reason is not None
    assert "guarded" in reason


def test_guarded_prefix_covers_cache_claude():
    leaf = _leaf(
        "echo",
        redirections=(Redirection(op=">", target="/home/steve/.cache/claude/x"),),
    )
    decision, _ = deny.evaluate(leaf)
    assert decision == "deny"


# --- Ask tier ---

def test_rm_verb_asks():
    decision, reason = deny.evaluate(_leaf("rm"))
    assert decision == "ask"
    assert reason is not None
    assert "rm" in reason


def test_rm_with_recursive_flag_asks():
    # rm -rf still reaches the user via ask — the flag pattern is in the
    # ask tier so the reason names the flag, but the verb-level ask would
    # fire even without the flag.
    decision, reason = deny.evaluate(
        _leaf("rm", flags=frozenset({"-r", "-f"}))
    )
    assert decision == "ask"
    assert reason is not None


def test_mv_verb_asks():
    decision, _ = deny.evaluate(_leaf("mv"))
    assert decision == "ask"


def test_chmod_asks():
    decision, _ = deny.evaluate(_leaf("chmod"))
    assert decision == "ask"


def test_git_push_asks():
    decision, reason = deny.evaluate(_leaf("git", subcommand="push"))
    assert decision == "ask"
    assert reason is not None
    assert "push" in reason


def test_uv_publish_asks():
    decision, _ = deny.evaluate(_leaf("uv", subcommand="publish"))
    assert decision == "ask"


def test_git_commit_with_force_flag_asks():
    leaf = _leaf("git", subcommand="commit", flags=frozenset({"--force"}))
    decision, reason = deny.evaluate(leaf)
    assert decision == "ask"
    assert reason is not None
    assert "--force" in reason


def test_truncate_redirect_over_existing_file_asks(tmp_path):
    target = tmp_path / "existing.txt"
    target.write_text("hi", encoding="utf-8")
    leaf = _leaf(
        "echo",
        redirections=(Redirection(op=">", target=str(target)),),
    )
    decision, reason = deny.evaluate(leaf)
    assert decision == "ask"
    assert reason is not None
    assert "overwrite" in reason


# --- No opinion (allow through to learner) ---

def test_git_status_has_no_opinion():
    decision, reason = deny.evaluate(_leaf("git", subcommand="status"))
    assert decision is None
    assert reason is None


def test_uv_pytest_has_no_opinion():
    decision, _ = deny.evaluate(
        _leaf("uv", subcommand="pytest", flags=frozenset({"-q"}))
    )
    assert decision is None


def test_append_redirect_into_tmp_has_no_opinion():
    leaf = _leaf(
        "echo",
        redirections=(Redirection(op=">>", target="/tmp/claude-permission-test-log"),),
    )
    decision, reason = deny.evaluate(leaf)
    assert decision is None, reason


def test_truncate_redirect_to_nonexistent_tmp_file_has_no_opinion(tmp_path):
    target = tmp_path / "does-not-exist.txt"
    leaf = _leaf(
        "echo",
        redirections=(Redirection(op=">", target=str(target)),),
    )
    decision, _ = deny.evaluate(leaf)
    assert decision is None


# --- is_denied backward-compat wrapper ---

def test_is_denied_true_for_deny_tier():
    denied, reason = deny.is_denied(_leaf("sudo"))
    assert denied
    assert reason is not None


def test_is_denied_false_for_ask_tier():
    denied, reason = deny.is_denied(_leaf("rm"))
    assert not denied
    assert reason is None


def test_is_denied_false_for_allow():
    denied, reason = deny.is_denied(_leaf("git", subcommand="status"))
    assert not denied
    assert reason is None
