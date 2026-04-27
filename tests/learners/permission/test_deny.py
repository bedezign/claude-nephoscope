"""Unit tests for learners.permission.deny — uncovered branches.

Focuses on the helper functions and paths not exercised by the integration-
level hook tests: config loading edge cases, non-dict config shapes,
subcommand/flag deny and ask matches, redirection guards, and the
``is_denied`` backward-compat wrapper.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from nephoscope.learners.permission.canonicalize import CanonicalLeaf, Redirection
from nephoscope.learners.permission.deny import (
    _check_ask_flags,
    _check_ask_redirections,
    _check_ask_subcommand,
    _check_ask_verb,
    _check_deny_flags,
    _check_deny_redirections,
    _check_deny_subcommand,
    _check_deny_verb,
    _load_config,
    _reset_cache,
    evaluate,
    is_denied,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _leaf(
    verb: str = 'ls',
    subcommand: str | None = None,
    flags: frozenset[str] | None = None,
    redirections: tuple[Redirection, ...] = (),
) -> CanonicalLeaf:
    return CanonicalLeaf(
        verb=verb,
        subcommand=subcommand,
        flags=flags if flags is not None else frozenset(),
        redirections=redirections,
        raw_leaf=verb,
    )


def _redir(op: str, target: str) -> Redirection:
    return Redirection(op=op, target=target)


# ---------------------------------------------------------------------------
# _load_config — missing file path
# ---------------------------------------------------------------------------


class TestLoadConfigMissingFile:
    def test_returns_empty_dict_when_config_file_absent(self, tmp_path, monkeypatch):
        _reset_cache()
        monkeypatch.setattr(
            'nephoscope.learners.permission.deny._CONFIG_PATH',
            tmp_path / 'nonexistent.yaml',
        )
        result = _load_config()
        assert result == {}
        _reset_cache()

    def test_non_dict_yaml_falls_back_to_empty_dict(self, tmp_path, monkeypatch):
        config_file = tmp_path / 'deny.yaml'
        config_file.write_text('- just_a_list\n- of_items\n', encoding='utf-8')
        _reset_cache()
        monkeypatch.setattr(
            'nephoscope.learners.permission.deny._CONFIG_PATH',
            config_file,
        )
        result = _load_config()
        assert result == {}
        _reset_cache()


# ---------------------------------------------------------------------------
# _reset_cache
# ---------------------------------------------------------------------------


class TestResetCache:
    def test_reset_forces_reload(self, tmp_path, monkeypatch):
        config_file = tmp_path / 'deny.yaml'
        config_file.write_text('denied_verbs: [badcmd]\n', encoding='utf-8')
        _reset_cache()
        monkeypatch.setattr(
            'nephoscope.learners.permission.deny._CONFIG_PATH',
            config_file,
        )
        result1 = _load_config()
        assert 'denied_verbs' in result1
        _reset_cache()
        # After reset the module-level cache is None; next call re-reads.
        result2 = _load_config()
        assert result2 == result1
        _reset_cache()


# ---------------------------------------------------------------------------
# _check_deny_verb
# ---------------------------------------------------------------------------


class TestCheckDenyVerb:
    def test_sudo_always_denied_regardless_of_config(self):
        leaf = _leaf(verb='sudo')
        result = _check_deny_verb(leaf, {})
        assert result == ('deny', "verb 'sudo' is never auto-allowed")

    def test_verb_in_denied_verbs_list_is_denied(self):
        leaf = _leaf(verb='badcmd')
        result = _check_deny_verb(leaf, {'denied_verbs': ['badcmd', 'othercmd']})
        assert result == ('deny', "verb 'badcmd' is in deny list")

    def test_verb_not_in_list_returns_none(self):
        leaf = _leaf(verb='ls')
        assert _check_deny_verb(leaf, {'denied_verbs': ['badcmd']}) is None

    def test_non_list_denied_verbs_does_not_match(self):
        # Malformed config — denied_verbs is a string, not a list.
        leaf = _leaf(verb='ls')
        assert _check_deny_verb(leaf, {'denied_verbs': 'ls'}) is None


# ---------------------------------------------------------------------------
# _check_deny_subcommand
# ---------------------------------------------------------------------------


class TestCheckDenySubcommand:
    def test_matching_subcommand_is_denied(self):
        leaf = _leaf(verb='git', subcommand='push')
        config = {'denied_subcommands': {'git': ['push', 'force-push']}}
        result = _check_deny_subcommand(leaf, config)
        assert result == ('deny', "subcommand 'git push' is in deny list")

    def test_non_matching_subcommand_returns_none(self):
        leaf = _leaf(verb='git', subcommand='status')
        config = {'denied_subcommands': {'git': ['push']}}
        assert _check_deny_subcommand(leaf, config) is None

    def test_none_subcommand_skips_check(self):
        leaf = _leaf(verb='git', subcommand=None)
        config = {'denied_subcommands': {'git': ['push']}}
        assert _check_deny_subcommand(leaf, config) is None

    def test_non_dict_denied_subcommands_returns_none(self):
        leaf = _leaf(verb='git', subcommand='push')
        assert _check_deny_subcommand(leaf, {'denied_subcommands': 'not_a_dict'}) is None


# ---------------------------------------------------------------------------
# _check_deny_flags
# ---------------------------------------------------------------------------


class TestCheckDenyFlags:
    def test_matching_flag_is_denied(self):
        leaf = _leaf(verb='rm', flags=frozenset(['-f', '-r']))
        config = {'denied_flag_patterns': {'rm': ['-f']}}
        result = _check_deny_flags(leaf, config)
        assert result == ('deny', "flag '-f' on 'rm' is in deny list")

    def test_non_matching_flag_returns_none(self):
        leaf = _leaf(verb='rm', flags=frozenset(['-r']))
        config = {'denied_flag_patterns': {'rm': ['-f']}}
        assert _check_deny_flags(leaf, config) is None

    def test_non_dict_denied_flag_patterns_returns_none(self):
        leaf = _leaf(verb='rm', flags=frozenset(['-f']))
        assert _check_deny_flags(leaf, {'denied_flag_patterns': 'not_a_dict'}) is None

    def test_non_list_patterns_for_verb_returns_none(self):
        # Verb key present but patterns is not a list.
        leaf = _leaf(verb='rm', flags=frozenset(['-f']))
        config = {'denied_flag_patterns': {'rm': '-f'}}
        assert _check_deny_flags(leaf, config) is None


# ---------------------------------------------------------------------------
# _check_deny_redirections
# ---------------------------------------------------------------------------


class TestCheckDenyRedirections:
    def test_non_write_redirection_is_skipped(self):
        # Input redirection (<) should not trigger a deny.
        leaf = _leaf(redirections=(_redir('<', '/etc/passwd'),))
        assert _check_deny_redirections(leaf) is None

    def test_write_to_guarded_system_path_is_denied(self, monkeypatch):
        monkeypatch.setattr(
            'nephoscope.learners.permission.deny._guarded_write_prefixes',
            lambda: ('/etc/', '/var/'),
        )
        leaf = _leaf(redirections=(_redir('>', '/etc/passwd'),))
        result = _check_deny_redirections(leaf)
        assert result is not None
        assert result[0] == 'deny'
        assert '/etc/passwd' in result[1]

    def test_append_to_guarded_path_is_denied(self, monkeypatch):
        monkeypatch.setattr(
            'nephoscope.learners.permission.deny._guarded_write_prefixes',
            lambda: ('/etc/',),
        )
        leaf = _leaf(redirections=(_redir('>>', '/etc/some/file'),))
        result = _check_deny_redirections(leaf)
        assert result is not None
        assert result[0] == 'deny'

    def test_write_to_safe_path_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            'nephoscope.learners.permission.deny._guarded_write_prefixes',
            lambda: ('/etc/', '/var/'),
        )
        leaf = _leaf(redirections=(_redir('>', '/home/user/output.txt'),))
        assert _check_deny_redirections(leaf) is None


# ---------------------------------------------------------------------------
# _check_ask_subcommand
# ---------------------------------------------------------------------------


class TestCheckAskSubcommand:
    def test_matching_subcommand_triggers_ask(self):
        leaf = _leaf(verb='git', subcommand='rebase')
        config = {'ask_subcommands': {'git': ['rebase', 'reset']}}
        result = _check_ask_subcommand(leaf, config)
        assert result == ('ask', "subcommand 'git rebase' needs confirmation")

    def test_non_matching_subcommand_returns_none(self):
        leaf = _leaf(verb='git', subcommand='status')
        config = {'ask_subcommands': {'git': ['rebase']}}
        assert _check_ask_subcommand(leaf, config) is None

    def test_none_subcommand_skips_check(self):
        leaf = _leaf(verb='git', subcommand=None)
        config = {'ask_subcommands': {'git': ['rebase']}}
        assert _check_ask_subcommand(leaf, config) is None

    def test_non_dict_ask_subcommands_returns_none(self):
        leaf = _leaf(verb='git', subcommand='rebase')
        assert _check_ask_subcommand(leaf, {'ask_subcommands': 'not_a_dict'}) is None


# ---------------------------------------------------------------------------
# _check_ask_flags
# ---------------------------------------------------------------------------


class TestCheckAskFlags:
    def test_matching_flag_triggers_ask(self):
        leaf = _leaf(verb='git', flags=frozenset(['--force']))
        config = {'ask_flag_patterns': {'git': ['--force']}}
        result = _check_ask_flags(leaf, config)
        assert result == ('ask', "flag '--force' on 'git' needs confirmation")

    def test_non_matching_flag_returns_none(self):
        leaf = _leaf(verb='git', flags=frozenset(['--verbose']))
        config = {'ask_flag_patterns': {'git': ['--force']}}
        assert _check_ask_flags(leaf, config) is None

    def test_non_dict_ask_flag_patterns_returns_none(self):
        leaf = _leaf(verb='git', flags=frozenset(['--force']))
        assert _check_ask_flags(leaf, {'ask_flag_patterns': 'not_a_dict'}) is None

    def test_non_list_ask_patterns_for_verb_returns_none(self):
        leaf = _leaf(verb='git', flags=frozenset(['--force']))
        config = {'ask_flag_patterns': {'git': '--force'}}
        assert _check_ask_flags(leaf, config) is None


# ---------------------------------------------------------------------------
# _check_ask_redirections
# ---------------------------------------------------------------------------


class TestCheckAskRedirections:
    def test_truncate_over_existing_file_triggers_ask(self, tmp_path):
        existing = tmp_path / 'existing.txt'
        existing.write_text('content', encoding='utf-8')
        leaf = _leaf(redirections=(_redir('>', str(existing)),))
        result = _check_ask_redirections(leaf)
        assert result is not None
        assert result[0] == 'ask'
        assert str(existing) in result[1]

    def test_truncate_to_nonexistent_path_returns_none(self, tmp_path):
        absent = tmp_path / 'new_output.txt'
        leaf = _leaf(redirections=(_redir('>', str(absent)),))
        assert _check_ask_redirections(leaf) is None

    def test_oserror_on_stat_returns_ask(self):
        # Simulate an OSError from os.path.isfile.
        leaf = _leaf(redirections=(_redir('>', '/some/path'),))
        with mock.patch('os.path.isfile', side_effect=OSError('perm denied')):
            result = _check_ask_redirections(leaf)
        assert result is not None
        assert result[0] == 'ask'
        assert 'could not be stat' in result[1]

    def test_append_redirection_is_not_checked(self, tmp_path):
        existing = tmp_path / 'log.txt'
        existing.write_text('content', encoding='utf-8')
        # ">>" does not trigger the ask check.
        leaf = _leaf(redirections=(_redir('>>', str(existing)),))
        assert _check_ask_redirections(leaf) is None


# ---------------------------------------------------------------------------
# is_denied — backward-compat wrapper
# ---------------------------------------------------------------------------


class TestIsDenied:
    def test_deny_decision_returns_true_with_reason(self):
        leaf = _leaf(verb='sudo')
        denied, reason = is_denied(leaf)
        assert denied is True
        assert reason is not None

    def test_ask_decision_returns_false_none(self, tmp_path, monkeypatch):
        # Build a config that puts ls in ask_verbs so evaluate() returns "ask".
        config_file = tmp_path / 'deny.yaml'
        config_file.write_text('ask_verbs: [ls]\n', encoding='utf-8')
        _reset_cache()
        monkeypatch.setattr(
            'nephoscope.learners.permission.deny._CONFIG_PATH',
            config_file,
        )
        leaf = _leaf(verb='ls')
        denied, reason = is_denied(leaf)
        assert denied is False
        assert reason is None
        _reset_cache()

    def test_no_opinion_returns_false_none(self):
        leaf = _leaf(verb='ls')
        _reset_cache()
        denied, reason = is_denied(leaf)
        assert denied is False
        assert reason is None
