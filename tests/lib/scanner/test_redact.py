"""Tests for the output-scanner redactor.

Tests cover:
- Redact mode (default): single match, multi-pattern, multi-occurrence,
  no-match passthrough, surrounding context preservation
- Warn mode: text unchanged, matches still recorded
- Match record shape: pattern name accessible
- Doom-path cases: empty string, empty patterns, invalid mode
"""

from __future__ import annotations

import importlib.resources as pkg_resources

import pytest

from nephoscope.lib.scanner.patterns import load_patterns
from nephoscope.lib.scanner.redact import redact


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_yaml_path():
    """Return the on-disk path of the shipped seed YAML via importlib.resources."""
    return pkg_resources.files("nephoscope.lib.scanner").joinpath("output_scanner.yaml")


@pytest.fixture
def loaded_patterns():
    """Compiled patterns loaded from the shipped seed YAML."""
    return load_patterns(_seed_yaml_path())


# ---------------------------------------------------------------------------
# Redact mode (default)
# ---------------------------------------------------------------------------


class TestRedactMode:
    """Default redact mode: replace matches with [REDACTED:<name>] markers."""

    def test_single_match_replaced(self, loaded_patterns):
        """A single anthropic key is replaced with the redaction marker."""
        text = "Bearer sk-ant-api03-abc"
        result = redact(text, loaded_patterns)

        assert "[REDACTED:anthropic_api_key]" in result.text
        assert "sk-ant-api03-abc" not in result.text

    def test_multiple_different_patterns_in_one_string(self, loaded_patterns):
        """A string with two different secrets has both redacted."""
        text = "first sk-ant-foo and second ghp_abcdefghijklmnopqrstuvwxyz012345"
        result = redact(text, loaded_patterns)

        assert "[REDACTED:anthropic_api_key]" in result.text
        assert "[REDACTED:github_personal_token]" in result.text
        assert "sk-ant-foo" not in result.text
        assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in result.text

    def test_multiple_occurrences_of_same_pattern(self, loaded_patterns):
        """Two `ghp_` tokens in one string both get redacted."""
        text = "token1 ghp_abcdefghijklmnopqrstuvwxyz012345 then ghp_zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
        result = redact(text, loaded_patterns)

        assert result.text.count("[REDACTED:github_personal_token]") == 2
        assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in result.text
        assert "ghp_zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz" not in result.text

    def test_no_match_text_unchanged(self, loaded_patterns):
        """A clean string passes through unchanged with no recorded matches."""
        text = "this is a perfectly clean string with no secrets"
        result = redact(text, loaded_patterns)

        assert result.text == text
        assert result.matches == []

    def test_surrounding_context_preserved(self, loaded_patterns):
        """Non-secret content around a redaction is preserved verbatim."""
        text = "key: sk-ant-abc123\nother: clean"
        result = redact(text, loaded_patterns)

        assert "other: clean" in result.text
        assert "sk-ant-abc123" not in result.text


# ---------------------------------------------------------------------------
# Warn mode
# ---------------------------------------------------------------------------


class TestWarnMode:
    """Warn mode returns the original text but still records matches."""

    def test_warn_mode_text_unchanged(self, loaded_patterns):
        """Warn mode returns the input text byte-for-byte."""
        text = "Bearer sk-ant-api03-abc"
        result = redact(text, loaded_patterns, mode="warn")

        assert result.text == text

    def test_warn_mode_match_still_recorded(self, loaded_patterns):
        """Even in warn mode, the matched pattern is in result.matches."""
        text = "Bearer sk-ant-api03-abc"
        result = redact(text, loaded_patterns, mode="warn")

        assert len(result.matches) >= 1
        assert any(m.name == "anthropic_api_key" for m in result.matches)


# ---------------------------------------------------------------------------
# Match record shape
# ---------------------------------------------------------------------------


class TestMatchRecord:
    """The MatchRecord exposes the pattern name."""

    def test_match_record_names_the_pattern(self, loaded_patterns):
        """result.matches[0].name carries the matched pattern's name."""
        text = "Bearer sk-ant-api03-abc"
        result = redact(text, loaded_patterns)

        assert len(result.matches) >= 1
        assert result.matches[0].name == "anthropic_api_key"


# ---------------------------------------------------------------------------
# Doom-path cases
# ---------------------------------------------------------------------------


class TestDoomPath:
    """Empty inputs and invalid args."""

    def test_empty_string(self, loaded_patterns):
        """Empty input returns empty text and no matches."""
        result = redact("", loaded_patterns)

        assert result.text == ""
        assert result.matches == []

    def test_empty_patterns_list(self):
        """No patterns means nothing to redact; text unchanged."""
        text = "sk-ant-abc"
        result = redact(text, [])

        assert result.text == text
        assert result.matches == []

    def test_invalid_mode_raises_valueerror(self, loaded_patterns):
        """An unknown mode value raises ValueError."""
        with pytest.raises(ValueError):
            redact("x", loaded_patterns, mode="invalid")
