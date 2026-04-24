"""Tests for lib/paths.py — path resolution and the canonicalize() helper."""

from __future__ import annotations

import pytest

from nephoscope.lib import paths


class TestCanonicalize:
    """Tests for canonicalize() — the write-site path normalizer."""

    def test_canonicalize_expands_tilde(self, tmp_path, monkeypatch):
        """A tilde-prefixed path expands to the current HOME."""
        monkeypatch.setenv("HOME", str(tmp_path))
        result = paths.canonicalize("~/something")
        assert result == str(tmp_path / "something"), (
            f"expected tilde to expand to {tmp_path}/something, "
            f"got {result!r} — expanduser() was not applied"
        )

    def test_canonicalize_resolves_symlinks(self, tmp_path):
        """A symlink input resolves to the target's real path."""
        target = tmp_path / "real"
        target.mkdir()
        (target / "file.txt").write_text("hello")
        link = tmp_path / "link"
        link.symlink_to(target)

        result = paths.canonicalize(link / "file.txt")
        expected = str(target.resolve() / "file.txt")
        assert result == expected, (
            f"expected symlink to resolve to {expected}, got {result!r} "
            f"— resolve() was not applied"
        )

    def test_canonicalize_accepts_nonexistent(self, tmp_path):
        """A path that does not exist is normalized, not raised on."""
        nonexistent = tmp_path / "does" / "not" / "exist.txt"
        # Must not raise FileNotFoundError.
        result = paths.canonicalize(nonexistent)
        assert result == str(nonexistent), (
            f"expected non-existent path to normalize to {nonexistent}, got {result!r}"
        )

    def test_canonicalize_empty_string_roundtrips(self):
        """Empty string round-trips to empty string (no synthesized path)."""
        assert paths.canonicalize("") == "", (
            "empty string must round-trip to empty string, not to cwd or home"
        )

    def test_canonicalize_none_roundtrips(self):
        """None input round-trips to empty string."""
        assert paths.canonicalize(None) == "", (
            "None input must round-trip to empty string"
        )

    def test_canonicalize_idempotent(self, tmp_path, monkeypatch):
        """canonicalize(canonicalize(p)) == canonicalize(p) for any p."""
        monkeypatch.setenv("HOME", str(tmp_path))
        # Create a symlink chain so there's real work to collapse.
        real = tmp_path / "real-dir"
        real.mkdir()
        link = tmp_path / "link-dir"
        link.symlink_to(real)

        for raw in [
            "~/something",
            str(link / "sub" / "file.txt"),
            "/plain/absolute/path",
            "",
        ]:
            first = paths.canonicalize(raw)
            second = paths.canonicalize(first)
            assert first == second, (
                f"canonicalize not idempotent for {raw!r}: "
                f"first={first!r} second={second!r}"
            )

    def test_canonicalize_accepts_path_object(self, tmp_path):
        """A pathlib.Path input is accepted, not just str."""
        target = tmp_path / "file.txt"
        result = paths.canonicalize(target)
        assert result == str(target), (
            f"Path input should yield {target!s}, got {result!r}"
        )

    def test_canonicalize_tilde_with_subpath(self, tmp_path, monkeypatch):
        """Tilde expansion composes correctly with a nested subpath."""
        monkeypatch.setenv("HOME", str(tmp_path))
        result = paths.canonicalize("~/a/b/c.txt")
        assert result == str(tmp_path / "a" / "b" / "c.txt"), (
            f"expected {tmp_path}/a/b/c.txt, got {result!r}"
        )

    @pytest.mark.parametrize(
        "raw",
        [
            "/",
            ".",
            "..",
            "/tmp/café.txt",
            "/tmp/日本語.txt",
        ],
    )
    def test_canonicalize_edge_inputs_do_not_raise(self, raw):
        """Unicode and relative/root inputs normalize without raising.

        canonicalize delegates to Path.resolve(strict=False), which accepts
        root, '.', '..', and arbitrary unicode without error on POSIX. Pin
        the contract: none of these inputs raise, and the result is
        idempotent under a second canonicalize pass.
        """
        result = paths.canonicalize(raw)
        assert isinstance(result, str)
        assert paths.canonicalize(result) == result, (
            f"canonicalize not idempotent on edge input {raw!r}: "
            f"first={result!r} second={paths.canonicalize(result)!r}"
        )


class TestCanonicalizeDocstringCites:
    """The docstring must list the sanctioned call sites so future
    INSERT-site authors land at canonicalize() naturally. This is a
    documentation contract, not a runtime one, but it's load-bearing
    for the plan's stated intent (see plan §5, 'Write-site audit')."""

    def test_docstring_cites_projects_cwd(self):
        assert "projects.cwd" in (paths.canonicalize.__doc__ or "")

    def test_docstring_cites_projects_root(self):
        # projects.root is written alongside cwd in upsert_project.
        assert "projects.root" in (paths.canonicalize.__doc__ or "") or (
            "root" in (paths.canonicalize.__doc__ or "")
        )

    def test_docstring_cites_file_paths(self):
        assert "file_paths" in (paths.canonicalize.__doc__ or "")

    def test_docstring_cites_transcript_path(self):
        # sessions.transcript_path — future-site reminder.
        assert "transcript_path" in (paths.canonicalize.__doc__ or "")

    def test_docstring_cites_future_settings_json_sites(self):
        # projects.settings_json_path / global_mirror.settings_json_path
        # are future INSERT sites; the plan says to pre-list them so a
        # future author lands at canonicalize() naturally.
        doc = paths.canonicalize.__doc__ or ""
        assert "settings_json_path" in doc, (
            "docstring must cite global_mirror.settings_json_path / "
            "projects.settings_json_path as future canonical write sites "
            "— otherwise a future INSERT-site author has no signpost"
        )
