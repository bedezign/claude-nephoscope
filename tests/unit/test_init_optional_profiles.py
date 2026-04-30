"""Tests for nephoscope-init optional profile prompt — Phase 2.

Covers:
- _OPTIONAL_PROFILES ordering and stems
- _prompt_for_profiles parser (all 7 input cases)
- _seed_optional_profiles tolerates failures
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# Project root relative paths
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
FIXTURES_OPTIONAL = (
    SRC_ROOT
    / "nephoscope"
    / "learners"
    / "permission"
    / "config"
    / "fixtures"
    / "optional"
)


# ---------------------------------------------------------------------------
# Step 10 — _OPTIONAL_PROFILES ordering and stems
# ---------------------------------------------------------------------------


class TestOptionalProfilesList:
    """_OPTIONAL_PROFILES is a list[tuple[str,str]] with correct ordering and coverage."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from nephoscope.cli.init_cmd import _OPTIONAL_PROFILES, _FIXTURES_DIR

        self._profiles = _OPTIONAL_PROFILES
        self._fixtures_dir = _FIXTURES_DIR

    def test_is_list_of_tuples(self):
        assert isinstance(self._profiles, list)
        for item in self._profiles:
            assert isinstance(item, tuple) and len(item) == 2, (
                f"Expected tuple[str,str], got {item!r}"
            )

    def test_has_five_entries(self):
        assert len(self._profiles) == 5, (
            f"Expected 5 profiles, got {len(self._profiles)}"
        )

    def test_ordering_matches_plan(self):
        stems = [stem for stem, _ in self._profiles]
        assert stems == [
            "project-dev",
            "dev-tools",
            "python-dev",
            "javascript",
            "devops",
        ], f"Order wrong: {stems}"

    def test_each_stem_has_file(self):
        for stem, _ in self._profiles:
            path = self._fixtures_dir / "optional" / f"{stem}.yaml"
            assert path.exists(), f"Missing file for stem {stem!r}: {path}"

    def test_descriptions_non_empty(self):
        for stem, desc in self._profiles:
            assert desc and desc.strip(), f"Empty description for {stem!r}"


# ---------------------------------------------------------------------------
# Step 11 — _prompt_for_profiles parser
# ---------------------------------------------------------------------------

_ALL_STEMS = ["project-dev", "dev-tools", "python-dev", "javascript", "devops"]


def _call_prompt(input_str: str) -> list[Path]:
    """Call _prompt_for_profiles with a monkeypatched input."""
    from nephoscope.cli.init_cmd import _prompt_for_profiles

    with patch("builtins.input", return_value=input_str):
        return _prompt_for_profiles()


class TestPromptForProfiles:
    """_prompt_for_profiles input parser — all 7 cases."""

    def test_blank_returns_empty(self):
        result = _call_prompt("")
        assert result == [], f"Blank input should return [], got {result!r}"

    def test_all_returns_all_five_paths(self):
        result = _call_prompt("all")
        stems = [p.stem for p in result]
        assert sorted(stems) == sorted(_ALL_STEMS), (
            f'"all" should return all stems, got {stems!r}'
        )

    def test_135_returns_three_paths(self):
        # 1=project-dev, 3=python-dev, 5=devops
        result = _call_prompt("135")
        stems = [p.stem for p in result]
        assert sorted(stems) == sorted(["project-dev", "python-dev", "devops"]), (
            f'"135" should return project-dev, python-dev, devops — got {stems!r}'
        )

    def test_single_digit_four_returns_one_path(self):
        # 4=javascript (was 3 before project-dev was inserted at position 1)
        result = _call_prompt("4")
        stems = [p.stem for p in result]
        assert stems == ["javascript"], f'"4" should return [javascript], got {stems!r}'

    def test_invalid_chars_returns_empty_with_warning(self, capsys):
        result = _call_prompt("abc")
        captured = capsys.readouterr()
        assert result == [], f"Invalid chars should return [], got {result!r}"
        assert captured.err, "Expected a warning on stderr for invalid chars"

    def test_digit_out_of_range_returns_empty_with_warning(self, capsys):
        result = _call_prompt("6")
        captured = capsys.readouterr()
        assert result == [], f"Out-of-range digit should return [], got {result!r}"
        assert captured.err, "Expected a warning on stderr for out-of-range digit"

    def test_mixed_valid_and_invalid_chars_returns_empty_with_warning(self, capsys):
        # "1x2" has valid digits but also "x" — isdigit() rejects the whole string
        # upfront, so the function returns [] without processing any digits.
        # This documents the fail-whole-input contract for mixed inputs.
        result = _call_prompt("1x2")
        captured = capsys.readouterr()
        assert result == [], f"Mixed-char input '1x2' should return [], got {result!r}"
        assert captured.err, "Expected a warning on stderr for mixed valid+invalid input"

    def test_eoferror_returns_empty(self):
        from nephoscope.cli.init_cmd import _prompt_for_profiles

        with patch("builtins.input", side_effect=EOFError):
            result = _prompt_for_profiles()
        assert result == [], f"EOFError should return [], got {result!r}"

    def test_dedupe_in_input_order(self):
        """Repeated digits are deduplicated; order preserved."""
        result = _call_prompt("112")
        stems = [p.stem for p in result]
        # 1=project-dev, 2=dev-tools; 1 is repeated so only 2 unique entries
        assert stems == ["project-dev", "dev-tools"], (
            f'"112" should dedupe and preserve order, got {stems!r}'
        )


# ---------------------------------------------------------------------------
# Step 13 — _seed_optional_profiles tolerates failures
# ---------------------------------------------------------------------------


class TestSeedOptionalProfiles:
    """_seed_optional_profiles: valid path lands entries, malformed warns and continues."""

    def test_valid_path_seeds_entries(self, tmp_db):
        from nephoscope.cli.init_cmd import _seed_optional_profiles

        dev_tools_path = FIXTURES_OPTIONAL / "dev-tools.yaml"
        _seed_optional_profiles(tmp_db, [dev_tools_path])
        tmp_db.commit()

        rows = tmp_db.execute(
            "SELECT COUNT(*) FROM rule_shapes WHERE verb = 'curl';"
        ).fetchone()
        assert rows[0] >= 1, "curl entry from dev-tools.yaml not found after seeding"

    def test_malformed_yaml_warns_and_does_not_raise(self, tmp_path, tmp_db, capsys):
        from nephoscope.cli.init_cmd import _seed_optional_profiles

        bad = tmp_path / "bad.yaml"
        bad.write_text("- verb: broken_entry\n  flags: []\n")  # missing decision

        # Should not raise
        _seed_optional_profiles(tmp_db, [bad])

        captured = capsys.readouterr()
        assert captured.err, "Expected warning on stderr for malformed yaml"

    def test_mix_valid_and_malformed_valid_lands(self, tmp_path, tmp_db, capsys):
        from nephoscope.cli.init_cmd import _seed_optional_profiles

        bad = tmp_path / "bad.yaml"
        bad.write_text("- verb: broken_entry\n  flags: []\n")  # missing decision
        dev_tools_path = FIXTURES_OPTIONAL / "dev-tools.yaml"

        _seed_optional_profiles(tmp_db, [dev_tools_path, bad])
        tmp_db.commit()

        captured = capsys.readouterr()
        assert captured.err, "Expected warning for malformed yaml"

        # Valid entries from dev-tools should still be present
        rows = tmp_db.execute(
            "SELECT COUNT(*) FROM rule_shapes WHERE verb = 'curl';"
        ).fetchone()
        assert rows[0] >= 1, (
            "curl entry should still land when malformed yaml is mixed in"
        )
