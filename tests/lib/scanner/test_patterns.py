"""Tests for the output-scanner pattern loader.

Tests cover:
- Loading the seed YAML and producing CompiledPattern objects
- All ten seed pattern names present
- Each entry has a compiled re.Pattern, not a raw string
- ValueError on malformed entries (missing key, invalid regex)
- Per-pattern positive samples that must match
- JWT negative cases that must NOT match
- JWT full-signature consumption via redactor
"""

from __future__ import annotations

import importlib.resources as pkg_resources
import re

import pytest
import yaml

from nephoscope.lib.scanner.patterns import load_patterns
from nephoscope.lib.scanner.redact import redact


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_yaml_path():
    """Return the on-disk path of the shipped seed YAML via importlib.resources."""
    return pkg_resources.files("nephoscope.lib.scanner").joinpath("output_scanner.yaml")


EXPECTED_NAMES = {
    "anthropic_api_key",
    "stripe_live_key",
    "stripe_live_key_alt",
    "github_personal_token",
    "github_oauth_token",
    "aws_access_key_id",
    "slack_token",
    "sendgrid_key",
    "jwt",
    "private_key_material",
}


POSITIVE_SAMPLES = {
    "anthropic_api_key": "sk-ant-api03-abc123",
    "stripe_live_key": "sk-live-abc123def456",
    "stripe_live_key_alt": "sk_live_abc123def456",
    "github_personal_token": "ghp_abcdefghijklmnopqrstuvwxyz012345",
    "github_oauth_token": "gho_abcdefghijklmnopqrstuvwxyz012345",
    "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "slack_token": "xoxb-1234567890-abcdefghij",
    "sendgrid_key": "SG.abc123.def456",
    "jwt": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.",
    "private_key_material": (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxyz\n"
        "-----END RSA PRIVATE KEY-----"
    ),
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def loaded_patterns():
    """Compiled patterns loaded from the shipped seed YAML."""
    return load_patterns(_seed_yaml_path())


# ---------------------------------------------------------------------------
# Load mechanics
# ---------------------------------------------------------------------------


class TestLoadPatterns:
    """Tests for load_patterns() — load + compile + validation."""

    def test_load_returns_non_empty_list(self, loaded_patterns):
        """Loading the shipped YAML returns a non-empty list."""
        assert isinstance(loaded_patterns, list)
        assert len(loaded_patterns) > 0

    def test_all_ten_pattern_names_present(self, loaded_patterns):
        """Loaded patterns contain exactly the ten plan-defined names."""
        actual_names = {p.name for p in loaded_patterns}
        assert actual_names == EXPECTED_NAMES

    def test_each_entry_has_compiled_regex(self, loaded_patterns):
        """Every CompiledPattern has a real re.Pattern in .regex (not a string)."""
        for p in loaded_patterns:
            assert isinstance(p.regex, re.Pattern), (
                f"pattern {p.name!r} has .regex of type {type(p.regex).__name__}, "
                "expected re.Pattern"
            )

    def test_missing_name_key_raises_valueerror(self, tmp_path):
        """Pattern entry missing the 'name' key triggers ValueError."""
        bad_yaml = tmp_path / "missing_name.yaml"
        bad_yaml.write_text(
            yaml.dump(
                {
                    "patterns": [
                        {"pattern": "abc"},
                    ]
                }
            )
        )

        with pytest.raises(ValueError):
            load_patterns(bad_yaml)

    def test_missing_pattern_key_raises_valueerror(self, tmp_path):
        """Pattern entry missing the 'pattern' key triggers ValueError."""
        bad_yaml = tmp_path / "missing_pattern.yaml"
        bad_yaml.write_text(
            yaml.dump(
                {
                    "patterns": [
                        {"name": "orphan"},
                    ]
                }
            )
        )

        with pytest.raises(ValueError):
            load_patterns(bad_yaml)

    def test_invalid_regex_raises_valueerror(self, tmp_path):
        """An entry whose pattern is an invalid regex triggers ValueError."""
        bad_yaml = tmp_path / "bad_regex.yaml"
        bad_yaml.write_text(
            yaml.dump(
                {
                    "patterns": [
                        {"name": "broken", "pattern": "["},
                    ]
                }
            )
        )

        with pytest.raises(ValueError):
            load_patterns(bad_yaml)


# ---------------------------------------------------------------------------
# Positive matches
# ---------------------------------------------------------------------------


class TestPositiveMatches:
    """Each shipped pattern must match its representative sample string."""

    @pytest.mark.parametrize(
        "pattern_name,sample",
        sorted(POSITIVE_SAMPLES.items()),
    )
    def test_pattern_matches_sample(self, loaded_patterns, pattern_name, sample):
        """`re.search(pattern.regex, sample)` must find a match."""
        by_name = {p.name: p for p in loaded_patterns}
        assert pattern_name in by_name, (
            f"pattern {pattern_name!r} not present in loaded patterns"
        )
        pattern = by_name[pattern_name]

        assert re.search(pattern.regex, sample) is not None, (
            f"pattern {pattern_name!r} did not match sample {sample!r}"
        )

    def test_private_key_material_matches_full_block(self, loaded_patterns):
        """The PEM pattern matches a complete BEGIN/END block and rejects a bare header."""
        by_name = {p.name: p for p in loaded_patterns}
        pattern = by_name['private_key_material']

        full_block = (
            '-----BEGIN PRIVATE KEY-----\n'
            'MIIEowIBAAKCAQEAxyz\n'
            '-----END PRIVATE KEY-----'
        )
        assert re.search(pattern.regex, full_block) is not None, (
            f'private_key_material did not match full PEM block {full_block!r}'
        )

        bare_header = '-----BEGIN PRIVATE KEY-----'
        assert re.search(pattern.regex, bare_header) is None, (
            'private_key_material should not match a bare header without END line'
        )


# ---------------------------------------------------------------------------
# JWT negative cases
# ---------------------------------------------------------------------------


class TestJwtNegatives:
    """The JWT pattern must not match base64-ish strings without 3-part shape."""

    def test_bare_eyj_does_not_match(self, loaded_patterns):
        """A bare `eyJ` string without dots must not match the JWT pattern."""
        by_name = {p.name: p for p in loaded_patterns}
        jwt_pattern = by_name["jwt"]

        assert re.search(jwt_pattern.regex, "eyJ") is None, (
            "JWT pattern should not match bare 'eyJ'"
        )

    def test_two_part_jwt_does_not_match(self, loaded_patterns):
        """A two-segment JWT-shaped string (no trailing dot) must not match."""
        by_name = {p.name: p for p in loaded_patterns}
        jwt_pattern = by_name["jwt"]

        sample = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0"
        assert re.search(jwt_pattern.regex, sample) is None, (
            f"JWT pattern should not match two-part shape {sample!r}"
        )


# ---------------------------------------------------------------------------
# JWT full-signature consumption
# ---------------------------------------------------------------------------


class TestJwtSignatureConsumption:
    """The JWT pattern must consume the full signature, leaving nothing visible."""

    def test_jwt_with_signature_is_fully_consumed(self, loaded_patterns):
        """A 3-part JWT with a real signature is matched and the signature does not survive redaction."""
        full_jwt = 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c'
        result = redact(full_jwt, loaded_patterns)
        assert 'SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c' not in result.text
