"""Unit tests for canonicalize.normalize_flags."""

from __future__ import annotations

from nephoscope.learners.permission.canonicalize import normalize_flags


class TestShortClusters:
    def test_two_letter_cluster_expands(self) -> None:
        assert normalize_flags(["-rf"]) == ["-f", "-r"]

    def test_multi_letter_cluster_not_split(self) -> None:
        assert normalize_flags(["-plant"]) == ["-plant"]

    def test_tar_style_cluster_not_split(self) -> None:
        assert normalize_flags(["-xvf"]) == ["-xvf"]

    def test_single_flag_unchanged(self) -> None:
        assert normalize_flags(["-r"]) == ["-r"]

    def test_already_expanded_flags_unchanged(self) -> None:
        assert normalize_flags(["-r", "-f"]) == ["-f", "-r"]


class TestLongFlags:
    def test_long_flag_unchanged(self) -> None:
        assert normalize_flags(["--force"]) == ["--force"]

    def test_long_flag_with_value_stripped(self) -> None:
        assert normalize_flags(["--file=foo"]) == ["--file"]

    def test_multiple_long_flags(self) -> None:
        assert normalize_flags(["--verbose", "--dry-run"]) == ["--dry-run", "--verbose"]


class TestMixedTokens:
    def test_letter_digit_not_split(self) -> None:
        assert normalize_flags(["-O3"]) == ["-O3"]

    def test_uppercase_single_not_split(self) -> None:
        assert normalize_flags(["-N"]) == ["-N"]

    def test_mixed_cluster_and_long(self) -> None:
        result = normalize_flags(["-rf", "--force"])
        assert result == ["--force", "-f", "-r"]


class TestNumericFlags:
    def test_numeric_flag_collapsed(self) -> None:
        assert normalize_flags(["-10"]) == ["-<N>"]

    def test_different_numeric_flags_same_sentinel(self) -> None:
        assert normalize_flags(["-40"]) == ["-<N>"]


class TestEdgeCases:
    def test_empty_list(self) -> None:
        assert normalize_flags([]) == []

    def test_non_flag_token_dropped(self) -> None:
        assert normalize_flags(["foo", "-r"]) == ["-r"]

    def test_whitespace_in_token_dropped(self) -> None:
        assert normalize_flags(["-r f"]) == []

    def test_output_is_sorted(self) -> None:
        result = normalize_flags(["-z", "-a", "-m"])
        assert result == sorted(result)

    def test_deduplication(self) -> None:
        assert normalize_flags(["-r", "-r", "-rf"]) == ["-f", "-r"]
