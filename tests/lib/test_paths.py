"""Tests for lib/paths.py — path resolution and the canonicalize() helper."""

from __future__ import annotations

import pathlib

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

    @pytest.mark.parametrize(
        "exc",
        [
            # OSError subtypes the fallback must absorb. FileNotFoundError is
            # not in this list on purpose — resolve(strict=False) is designed
            # not to raise it for missing paths, so its appearance here would
            # mislead readers about what actually triggers the fallback.
            PermissionError(13, "Permission denied"),
            NotADirectoryError(20, "Not a directory"),
            OSError(5, "Input/output error"),
        ],
    )
    def test_canonicalize_falls_back_on_os_error(self, monkeypatch, tmp_path, exc):
        """resolve() raising any OSError subtype falls back to expanduser-only.

        Hot DB-write paths must not crash on a single unreadable symlink
        segment. The fallback is still deterministic — tildes still
        expand — it just doesn't chase symlinks through the inaccessible
        bit. Parametrized across OSError subtypes so a regression to one
        specific errno still flags.
        """
        monkeypatch.setenv("HOME", str(tmp_path))

        def fail_resolve(self, strict=False):
            raise exc

        monkeypatch.setattr(pathlib.Path, "resolve", fail_resolve)

        result = paths.canonicalize("~/some/deep/path")
        assert result == str(tmp_path / "some" / "deep" / "path"), (
            f"expected expanduser-only fallback for {type(exc).__name__}, "
            f"got {result!r}"
        )

    def test_canonicalize_very_long_path_does_not_raise(self):
        """A near-PATH_MAX length path normalizes without raising.

        Linux PATH_MAX is 4096; the OS won't resolve a syscall-sized path,
        but canonicalize must not crash given one — a malformed payload
        field or a truncated log line should not take the recorder down.
        """
        deep = "/tmp/" + "/".join("x" * 40 for _ in range(90))
        assert len(deep) > 3500
        result = paths.canonicalize(deep)
        assert isinstance(result, str)
        assert paths.canonicalize(result) == result


class TestExtractAddDirArgs:
    """Tests for extract_add_dir_args() — parses parent process argv for --add-dir.

    The cmdline file format mirrors Linux's /proc/<pid>/cmdline: NUL-separated
    argv entries with a trailing NUL. Tests pass an explicit path to a fixture
    file rather than touching /proc.
    """

    @staticmethod
    def _write_cmdline(target: pathlib.Path, argv: list[str]) -> pathlib.Path:
        """Write argv as a NUL-separated cmdline payload."""
        target.write_bytes(b"\x00".join(a.encode("utf-8") for a in argv) + b"\x00")
        return target

    def test_empty_cmdline_returns_empty(self, tmp_path):
        """An empty file (or file with no flags) yields []."""
        cmdline = tmp_path / "cmdline"
        cmdline.write_bytes(b"")
        assert paths.extract_add_dir_args(cmdline) == []

    def test_no_add_dir_flag_returns_empty(self, tmp_path):
        """A normal claude invocation without --add-dir yields []."""
        cmdline = self._write_cmdline(
            tmp_path / "cmdline", ["claude", "--print", "hello"]
        )
        assert paths.extract_add_dir_args(cmdline) == []

    def test_single_separated_form(self, tmp_path):
        """`claude --add-dir /foo` returns ['/foo'] (canonicalized)."""
        cmdline = self._write_cmdline(
            tmp_path / "cmdline", ["claude", "--add-dir", "/tmp"]
        )
        result = paths.extract_add_dir_args(cmdline)
        assert result == [paths.canonicalize("/tmp")], (
            f"expected canonicalized /tmp, got {result!r}"
        )

    def test_single_joined_form(self, tmp_path):
        """`claude --add-dir=/foo` returns ['/foo']."""
        cmdline = self._write_cmdline(
            tmp_path / "cmdline", ["claude", "--add-dir=/tmp"]
        )
        result = paths.extract_add_dir_args(cmdline)
        assert result == [paths.canonicalize("/tmp")]

    def test_multiple_separate_flags(self, tmp_path):
        """Multiple `--add-dir` flags accumulate in argv order."""
        cmdline = self._write_cmdline(
            tmp_path / "cmdline",
            ["claude", "--add-dir", "/tmp", "--add-dir", "/var/tmp"],
        )
        result = paths.extract_add_dir_args(cmdline)
        assert result == [paths.canonicalize("/tmp"), paths.canonicalize("/var/tmp")]

    def test_variadic_consumes_consecutive_values(self, tmp_path):
        """`--add-dir <directories...>` is variadic; consecutive non-flag args belong to it.

        Mirrors Claude Code's own argv semantics: this is exactly the parsing
        rule that bit us during the empirical test (the prompt token got
        consumed as a directory).
        """
        cmdline = self._write_cmdline(
            tmp_path / "cmdline",
            ["claude", "--add-dir", "/tmp", "/var/tmp", "/usr/local"],
        )
        result = paths.extract_add_dir_args(cmdline)
        assert result == [
            paths.canonicalize("/tmp"),
            paths.canonicalize("/var/tmp"),
            paths.canonicalize("/usr/local"),
        ]

    def test_variadic_stops_at_next_flag(self, tmp_path):
        """Variadic consumption stops when the next `-` or `--` flag appears."""
        cmdline = self._write_cmdline(
            tmp_path / "cmdline",
            ["claude", "--add-dir", "/tmp", "--print", "hello"],
        )
        result = paths.extract_add_dir_args(cmdline)
        assert result == [paths.canonicalize("/tmp")]

    def test_dashdash_terminates_parsing(self, tmp_path):
        """`--` terminates flag parsing; later `--add-dir` is positional."""
        cmdline = self._write_cmdline(
            tmp_path / "cmdline",
            ["claude", "--add-dir", "/tmp", "--", "--add-dir", "/never"],
        )
        result = paths.extract_add_dir_args(cmdline)
        assert result == [paths.canonicalize("/tmp")], (
            f"expected only /tmp before --, got {result!r}"
        )

    def test_missing_value_does_not_crash(self, tmp_path):
        """Trailing `--add-dir` with no value yields [] and does not raise."""
        cmdline = self._write_cmdline(tmp_path / "cmdline", ["claude", "--add-dir"])
        result = paths.extract_add_dir_args(cmdline)
        assert result == []

    def test_missing_proc_file_returns_empty(self, tmp_path):
        """Non-existent cmdline path returns [] (non-Linux fallback shape)."""
        nonexistent = tmp_path / "does-not-exist"
        assert paths.extract_add_dir_args(nonexistent) == []

    def test_default_reads_parent_cmdline(self, tmp_path, monkeypatch):
        """Without an explicit path, reads /proc/<ppid>/cmdline.

        Verified by monkeypatching ``os.getppid`` to point to a fake pid whose
        cmdline file we control via a fake /proc layout.
        """
        fake_proc = tmp_path / "proc" / "9999"
        fake_proc.mkdir(parents=True)
        self._write_cmdline(fake_proc / "cmdline", ["claude", "--add-dir", "/tmp"])

        # Monkeypatch the function that resolves the default cmdline path.
        # This avoids touching os.getppid directly — the helper exposes the
        # default-path resolver as overridable for exactly this reason.
        monkeypatch.setattr(
            paths, "_default_cmdline_path", lambda: fake_proc / "cmdline"
        )
        result = paths.extract_add_dir_args()
        assert result == [paths.canonicalize("/tmp")]

    def test_canonicalizes_tilde(self, tmp_path, monkeypatch):
        """Captured values are canonicalized — tilde expansion in particular."""
        monkeypatch.setenv("HOME", str(tmp_path))
        cmdline = self._write_cmdline(
            tmp_path / "cmdline", ["claude", "--add-dir", "~/proj"]
        )
        result = paths.extract_add_dir_args(cmdline)
        assert result == [str(tmp_path / "proj")], (
            f"expected tilde to expand, got {result!r}"
        )

    def test_empty_string_value_is_dropped(self, tmp_path):
        """`--add-dir=` (joined form, empty value) yields [] for that flag."""
        cmdline = self._write_cmdline(tmp_path / "cmdline", ["claude", "--add-dir="])
        result = paths.extract_add_dir_args(cmdline)
        assert result == []

    def test_extract_add_dir_args_duplicates_preserved(self, tmp_path):
        """Duplicate --add-dir flags are returned verbatim; dedup is the merger's job."""
        cmdline = self._write_cmdline(
            tmp_path / "cmdline",
            ["claude", "--add-dir", str(tmp_path), "--add-dir", str(tmp_path)],
        )
        result = paths.extract_add_dir_args(cmdline)
        assert result == [
            paths.canonicalize(str(tmp_path)),
            paths.canonicalize(str(tmp_path)),
        ], f"expected duplicate entries preserved, got {result!r}"

    def test_permission_denied_returns_empty_and_emits_stderr(
        self, tmp_path, monkeypatch, capsys
    ):
        """PermissionError on /proc read returns [] and emits a stderr signal.

        The observability rule requires at least one signal on catch-and-swallow.
        Verified here so a regression that silences the handler is caught before
        it ships.
        """
        cmdline = tmp_path / "cmdline"
        cmdline.write_bytes(b"irrelevant")

        original_read_bytes = pathlib.Path.read_bytes

        def _raise_permission_error(self):
            if str(self) == str(cmdline):
                raise PermissionError(13, "Permission denied")
            return original_read_bytes(self)

        monkeypatch.setattr(pathlib.Path, "read_bytes", _raise_permission_error)

        result = paths.extract_add_dir_args(cmdline)

        assert result == [], f"expected [] on PermissionError, got {result!r}"
        captured = capsys.readouterr()
        assert "[nephoscope] extract_add_dir_args:" in captured.err, (
            "expected a stderr observability signal on PermissionError, got none; "
            f"stderr was: {captured.err!r}"
        )


class TestObservationsDbPath:
    def test_env_var_tier_overrides_everything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OBSERVABILITY_DB", "/tmp/from-env.db")
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        result = paths.observations_db_path()
        assert result == pathlib.Path("/tmp/from-env.db"), (
            f"OBSERVABILITY_DB env var must win; got {result!r}"
        )

    def test_config_tier_used_when_env_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        from nephoscope.config import get_config

        config_file = tmp_path / "nephoscope.toml"
        config_file.write_text('database = "/data/cfg.db"\n')

        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(config_file))
        monkeypatch.delenv("OBSERVABILITY_DB", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)

        get_config.cache_clear()
        try:
            result = paths.observations_db_path()
        finally:
            get_config.cache_clear()

        assert result == pathlib.Path("/data/cfg.db"), (
            f"config database field must be used when OBSERVABILITY_DB is absent; got {result!r}"
        )

    def test_plugin_data_tier_used_when_config_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        from nephoscope.config import get_config

        nonexistent_config = tmp_path / "no-config.toml"
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(nonexistent_config))
        monkeypatch.delenv("OBSERVABILITY_DB", raising=False)
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", "/tmp/plugin")

        get_config.cache_clear()
        try:
            result = paths.observations_db_path()
        finally:
            get_config.cache_clear()

        assert result == pathlib.Path("/tmp/plugin/observations.db"), (
            f"CLAUDE_PLUGIN_DATA tier must be used when config has no database; got {result!r}"
        )

    def test_hard_fail_when_nothing_configured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        from nephoscope.config import get_config

        nonexistent_config = tmp_path / "no-config.toml"
        monkeypatch.setenv("NEPHOSCOPE_CONFIG", str(nonexistent_config))
        monkeypatch.delenv("OBSERVABILITY_DB", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)

        get_config.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="nephoscope init"):
                paths.observations_db_path()
        finally:
            get_config.cache_clear()
