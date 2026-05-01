"""Tests for nephoscope-init optional profile prompt.

Covers:
- _prompt_for_profiles: space-separated token parser (all cases)
- _seed_optional_profiles: uses apply_profile, tolerates failures
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


# Project root relative paths
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
FIXTURES_META = (
    SRC_ROOT
    / "nephoscope"
    / "learners"
    / "permission"
    / "config"
    / "fixtures"
    / "meta-profiles"
)


# ---------------------------------------------------------------------------
# _prompt_for_profiles — space-separated token parser
# ---------------------------------------------------------------------------


def _call_prompt(input_str: str) -> list[Path]:
    """Call _prompt_for_profiles with a monkeypatched input."""
    from nephoscope.cli.init_cmd import _prompt_for_profiles

    with patch("builtins.input", return_value=input_str):
        return _prompt_for_profiles()


class TestPromptForProfiles:
    """_prompt_for_profiles input parser — all input cases."""

    def test_blank_returns_empty(self):
        result = _call_prompt("")
        assert result == [], f"Blank input should return [], got {result!r}"

    def test_eoferror_returns_empty(self):
        from nephoscope.cli.init_cmd import _prompt_for_profiles

        with patch("builtins.input", side_effect=EOFError):
            result = _prompt_for_profiles()
        assert result == [], f"EOFError should return [], got {result!r}"

    def test_single_valid_token_returns_one_path(self):
        result = _call_prompt("1")
        assert len(result) == 1, (
            f"Single valid token should return 1 path, got {result!r}"
        )
        assert isinstance(result[0], Path)

    def test_space_separated_tokens_returns_multiple_paths(self):
        result = _call_prompt("1 2")
        assert len(result) == 2, (
            f"Two valid tokens should return 2 paths, got {result!r}"
        )

    def test_non_digit_token_warns_and_returns_empty(self, capsys):
        # Non-digit tokens (e.g. "abc") are skipped with a warning on stderr.
        result = _call_prompt("abc")
        assert result == [], f"Non-digit token should return [], got {result!r}"
        captured = capsys.readouterr()
        assert captured.err, "Expected a warning on stderr for non-digit token 'abc'"
        assert "abc" in captured.err

    def test_out_of_range_digit_warns_and_returns_empty(self, capsys):
        # A digit beyond the number of profiles emits a warning on stderr.
        result = _call_prompt("999")
        assert result == [], f"Out-of-range digit should return [], got {result!r}"
        captured = capsys.readouterr()
        assert captured.err, "Expected a warning on stderr for out-of-range digit '999'"
        assert "999" in captured.err

    def test_mixed_valid_and_invalid_tokens_returns_valid_only(self):
        # "1 abc 2" — valid digits 1 and 2 return paths; "abc" is skipped.
        result = _call_prompt("1 abc 2")
        assert len(result) == 2, (
            f"Mixed input '1 abc 2' should yield 2 paths, got {result!r}"
        )

    def test_paths_point_to_existing_yaml_files(self):
        result = _call_prompt("1 2")
        for p in result:
            assert p.exists(), f"Returned path does not exist: {p}"
            assert p.suffix == ".yaml", f"Returned path is not a YAML file: {p}"

    def test_paths_are_in_meta_profiles_directory(self):
        result = _call_prompt("1")
        assert len(result) == 1
        assert result[0].parent == FIXTURES_META, (
            f"Expected path in meta-profiles dir, got {result[0].parent}"
        )

    def test_all_returns_all_paths(self):
        from nephoscope.learners.permission.profiles import list_profiles

        profiles = list_profiles()
        result = _call_prompt("all")
        assert len(result) == len(profiles), (
            f"'all' should return all {len(profiles)} profiles, got {len(result)}"
        )
        assert result == [p.path for p in profiles]

    def test_all_case_insensitive(self):
        from nephoscope.learners.permission.profiles import list_profiles

        profiles = list_profiles()
        for variant in ("ALL", "All", "aLl"):
            result = _call_prompt(variant)
            assert len(result) == len(profiles), (
                f"'{variant}' should return all profiles, got {len(result)}"
            )

    def test_duplicate_tokens_deduped(self):
        result = _call_prompt("1 1")
        assert len(result) == 1, (
            f"Duplicate token '1 1' should return 1 path, not {len(result)}"
        )

    def test_unicode_digit_token_warns(self, capsys):
        # U+00B2 SUPERSCRIPT TWO: isdigit()=True, isdecimal()=False
        result = _call_prompt("²")
        assert result == [], f"Unicode superscript digit should return [], got {result!r}"
        captured = capsys.readouterr()
        assert captured.err, "Expected a warning on stderr for unicode superscript digit"
        assert "²" in captured.err

    def test_compact_format_no_longer_supported(self, capsys):
        # "135" is a single token with value 135, which exceeds len(profiles).
        # It is treated as one out-of-range number, not three separate selections.
        result = _call_prompt("135")
        assert result == [], (
            f"'135' should return [] (out-of-range warning), got {result!r}"
        )
        captured = capsys.readouterr()
        assert captured.err, "Expected out-of-range warning for token '135'"
        assert "135" in captured.err


# ---------------------------------------------------------------------------
# _seed_optional_profiles — uses apply_profile, tolerates failures
# ---------------------------------------------------------------------------


class TestSeedOptionalProfiles:
    """_seed_optional_profiles: valid path lands entries, malformed warns and continues."""

    def test_valid_path_seeds_entries(self, tmp_db):
        from nephoscope.cli.init_cmd import _seed_optional_profiles

        dev_tools_path = FIXTURES_META / "dev-tools.yaml"
        _seed_optional_profiles(tmp_db, [dev_tools_path])
        tmp_db.commit()

        rows = tmp_db.execute(
            "SELECT COUNT(*) FROM rule_shapes WHERE verb = 'curl';"
        ).fetchone()
        assert rows[0] >= 1, "curl entry from dev-tools.yaml not found after seeding"

    def test_malformed_yaml_warns_and_does_not_raise(self, tmp_path, tmp_db, capsys):
        from nephoscope.cli.init_cmd import _seed_optional_profiles

        bad = tmp_path / "bad.yaml"
        # Missing _meta — apply_profile will raise ValueError, which is caught.
        bad.write_text("permissions:\n  - verb: broken_entry\n    flags: []\n")

        # Should not raise
        _seed_optional_profiles(tmp_db, [bad])

        captured = capsys.readouterr()
        assert captured.err, "Expected warning on stderr for malformed yaml"

    def test_mix_valid_and_malformed_valid_lands(self, tmp_path, tmp_db, capsys):
        from nephoscope.cli.init_cmd import _seed_optional_profiles

        bad = tmp_path / "bad.yaml"
        bad.write_text("permissions:\n  - verb: broken_entry\n    flags: []\n")
        dev_tools_path = FIXTURES_META / "dev-tools.yaml"

        _seed_optional_profiles(tmp_db, [dev_tools_path, bad])
        tmp_db.commit()

        captured = capsys.readouterr()
        assert captured.err, "Expected warning on stderr for malformed yaml"

        # Valid entries from dev-tools should still be present
        rows = tmp_db.execute(
            "SELECT COUNT(*) FROM rule_shapes WHERE verb = 'curl';"
        ).fetchone()
        assert rows[0] >= 1, (
            "curl entry should still land when malformed yaml is mixed in"
        )

    def test_empty_paths_is_noop(self, tmp_db):
        from nephoscope.cli.init_cmd import _seed_optional_profiles

        before = tmp_db.execute("SELECT COUNT(*) FROM rule_shapes;").fetchone()[0]
        _seed_optional_profiles(tmp_db, [])
        after = tmp_db.execute("SELECT COUNT(*) FROM rule_shapes;").fetchone()[0]
        assert before == after, "Empty paths should not insert any rows"
