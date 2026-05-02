"""Tests for the meta-profile loader: ``nephoscope.learners.permission.profiles``.

This module wraps ``apply_fixtures`` / ``apply_verb_types`` for YAML files that
group ``permissions`` and ``verb_types`` into a single profile (with a ``_meta``
header for id/description). It also exposes profile discovery (bundled + user
directories) and an interactive ``main(argv)`` CLI with ``list`` and ``load``
subcommands.

The tests cover:
  - ``list_profiles`` — bundled-only / user-only / both / collision / no-meta
    skip / malformed YAML skip / user dir auto-creation.
  - ``apply_profile`` — permissions only / verb_types only / both / missing
    ``_meta`` / missing id / non-dict YAML / DB-level idempotency.
  - ``load_profile_by_id`` — unknown id raises / known id loads / return tuple.
  - ``main`` CLI — ``list`` output / ``load`` confirm flow / unknown id exit /
    'n' at prompt aborts.

Test isolation: ``tmp_db`` from ``tests/conftest.py`` patches
``OBSERVABILITY_DB`` and yields a connection with the schema applied; ``tmp_path``
gives each test its own bundled / user directories so the real installed
profiles are never touched. CLI tests monkeypatch
``nephoscope.learners.permission.profiles._read_line`` for stdin input — same
pattern used in ``tests/commands/test_review_cmd.py``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from nephoscope.learners.permission.profiles import (
    ProfileEntry,
    _parse_ids,
    apply_profile,
    list_profiles,
    load_profile_by_id,
    main,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_profile(
    path: Path,
    profile_id: str,
    description: str,
    permissions: list[dict] | None = None,
    verb_types: list[dict] | None = None,
) -> Path:
    """Write a meta-profile YAML file at ``path`` and return the path."""
    body: dict = {
        "_meta": {"id": profile_id, "description": description},
    }
    if permissions is not None:
        body["permissions"] = permissions
    if verb_types is not None:
        body["verb_types"] = verb_types
    path.write_text(yaml.dump(body), encoding="utf-8")
    return path


def _count_permissions(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM permissions;").fetchone()[0]


def _count_verb_categories(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM verb_categories;").fetchone()[0]


# ===========================================================================
# list_profiles
# ===========================================================================


class TestListProfiles:
    """Discovery of bundled + user profile YAMLs."""

    def test_bundled_only(self, tmp_path: Path) -> None:
        """Profiles in bundled_dir appear with source='bundled'."""
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        bundled.mkdir()
        _write_profile(bundled / "alpha.yaml", "alpha", "Alpha profile")

        entries = list_profiles(bundled_dir=bundled, user_dir=user)

        assert len(entries) == 1
        assert isinstance(entries[0], ProfileEntry)
        assert entries[0].id == "alpha"
        assert entries[0].description == "Alpha profile"
        assert entries[0].source == "bundled"
        assert entries[0].path == bundled / "alpha.yaml"

    def test_user_only(self, tmp_path: Path) -> None:
        """Profiles in user_dir appear with source='user'."""
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        bundled.mkdir()
        user.mkdir()
        _write_profile(user / "mine.yaml", "mine", "My profile")

        entries = list_profiles(bundled_dir=bundled, user_dir=user)

        assert len(entries) == 1
        assert entries[0].id == "mine"
        assert entries[0].source == "user"

    def test_both_dirs_yields_both_with_bundled_first(self, tmp_path: Path) -> None:
        """Listing returns bundled before user when both have unique ids."""
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        bundled.mkdir()
        user.mkdir()
        _write_profile(bundled / "core.yaml", "core", "Core")
        _write_profile(user / "extras.yaml", "extras", "Extras")

        entries = list_profiles(bundled_dir=bundled, user_dir=user)

        sources = [e.source for e in entries]
        assert "bundled" in sources
        assert "user" in sources
        # bundled entries come first
        assert sources.index("bundled") < sources.index("user")

    def test_empty_dirs_returns_empty_list(self, tmp_path: Path) -> None:
        """When both dirs are empty, return an empty list."""
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        bundled.mkdir()
        user.mkdir()

        assert list_profiles(bundled_dir=bundled, user_dir=user) == []

    def test_id_collision_bundled_wins(self, tmp_path: Path) -> None:
        """When bundled and user profiles share an id, bundled wins.

        The user-side entry must not appear in the returned list — it is
        suppressed entirely, not just sorted after the bundled one.
        """
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        bundled.mkdir()
        user.mkdir()
        _write_profile(
            bundled / "shared.yaml", "shared", "From bundled", permissions=[]
        )
        _write_profile(user / "shared.yaml", "shared", "From user", permissions=[])

        entries = list_profiles(bundled_dir=bundled, user_dir=user)

        # bundled wins: it appears
        bundled_match = [
            e for e in entries if e.id == "shared" and e.source == "bundled"
        ]
        assert len(bundled_match) == 1
        assert bundled_match[0].description == "From bundled"

        # user-side colliding entry is skipped (not in list)
        user_match = [e for e in entries if e.id == "shared" and e.source == "user"]
        assert user_match == []

    def test_file_without_meta_skipped(self, tmp_path: Path) -> None:
        """A YAML file without a valid ``_meta`` block is silently skipped."""
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        bundled.mkdir()
        user.mkdir()
        # Valid neighbour proves the loop does not abort on the bad file.
        _write_profile(bundled / "good.yaml", "good", "Good")
        # No _meta block at all.
        (bundled / "no_meta.yaml").write_text(
            yaml.dump({"permissions": []}), encoding="utf-8"
        )

        entries = list_profiles(bundled_dir=bundled, user_dir=user)

        ids = [e.id for e in entries]
        assert "good" in ids
        # The no-meta file is silently skipped — no entry appears for it.
        assert len(entries) == 1

    def test_malformed_yaml_skipped(self, tmp_path: Path) -> None:
        """A file with unparseable YAML is silently skipped, not propagated."""
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        bundled.mkdir()
        user.mkdir()
        _write_profile(bundled / "good.yaml", "good", "Good")
        # YAML syntax error: unbalanced bracket, unfinished mapping.
        (bundled / "broken.yaml").write_text(
            ": this is not [valid yaml ::: {", encoding="utf-8"
        )

        # Must not raise.
        entries = list_profiles(bundled_dir=bundled, user_dir=user)
        ids = [e.id for e in entries]
        assert ids == ["good"]

    def test_user_dir_does_not_exist_does_not_create_it(self, tmp_path: Path) -> None:
        """list_profiles does not create the user_dir; missing dir returns no user entries."""
        bundled = tmp_path / "bundled"
        user = tmp_path / "user_does_not_yet_exist"
        bundled.mkdir()
        _write_profile(bundled / "core.yaml", "core", "Core")

        assert not user.exists()

        entries = list_profiles(bundled_dir=bundled, user_dir=user)

        # list_profiles must NOT create the directory
        assert not user.exists()

        # only the bundled entry shows up
        sources = [e.source for e in entries]
        assert sources == ["bundled"]

    def test_cmd_load_creates_user_dir(self, tmp_path: Path, monkeypatch) -> None:
        """_cmd_load creates the user_dir when it does not exist."""
        from unittest.mock import patch

        from nephoscope.learners.permission.profiles import _cmd_load

        plugin_data = tmp_path / "plugin_data"
        assert not plugin_data.exists()

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))
        empty_bundled = tmp_path / "empty_bundled"
        empty_bundled.mkdir()
        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._bundled_dir",
            lambda: empty_bundled,
        )

        # _cmd_load with no matching ids exits early with an error after mkdir
        with patch("builtins.print"):
            _cmd_load(["nonexistent"])

        assert (plugin_data / "profiles").exists()

    def test_user_dir_does_not_exist_no_bundled_returns_empty(
        self, tmp_path: Path
    ) -> None:
        """Missing user_dir + empty bundled returns empty list; dir is NOT created."""
        bundled = tmp_path / "bundled"
        user = tmp_path / "user_missing"
        bundled.mkdir()

        entries = list_profiles(bundled_dir=bundled, user_dir=user)

        assert entries == []
        assert not user.exists()

    def test_list_profiles_null_order_defaults_to_999(self, tmp_path: Path) -> None:
        """A profile with ``order: null`` in _meta resolves to order 999."""
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        bundled.mkdir()
        user.mkdir()
        profile_path = bundled / "foo.yaml"
        profile_path.write_text(
            yaml.dump({"_meta": {"id": "foo", "order": None, "description": "test"}}),
            encoding="utf-8",
        )

        entries = list_profiles(bundled_dir=bundled, user_dir=user)

        assert len(entries) == 1
        assert entries[0].order == 999

    def test_list_profiles_non_numeric_order_defaults_to_999(
        self, tmp_path: Path
    ) -> None:
        """A profile with ``order: "invalid"`` in _meta resolves to order 999."""
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        bundled.mkdir()
        user.mkdir()
        profile_path = bundled / "foo.yaml"
        profile_path.write_text(
            yaml.dump(
                {"_meta": {"id": "foo", "order": "invalid", "description": "test"}}
            ),
            encoding="utf-8",
        )

        entries = list_profiles(bundled_dir=bundled, user_dir=user)

        assert len(entries) == 1
        assert entries[0].order == 999

    def test_list_profiles_same_order_sorted_by_id(self, tmp_path: Path) -> None:
        """Two profiles with the same order value are sorted alphabetically by id."""
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        bundled.mkdir()
        user.mkdir()
        (bundled / "zebra.yaml").write_text(
            yaml.dump({"_meta": {"id": "zebra", "order": 5, "description": "last"}}),
            encoding="utf-8",
        )
        (bundled / "apple.yaml").write_text(
            yaml.dump({"_meta": {"id": "apple", "order": 5, "description": "first"}}),
            encoding="utf-8",
        )

        entries = list_profiles(bundled_dir=bundled, user_dir=user)

        assert len(entries) == 2
        assert [e.id for e in entries] == ["apple", "zebra"]


# ===========================================================================
# _cmd_load mkdir failure
# ===========================================================================


class TestCmdLoadMkdirFailure:
    """_cmd_load exits 1 and emits stderr when the user profiles directory cannot be created."""

    def test_cmd_load_user_dir_mkdir_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When mkdir raises OSError, _cmd_load returns 1 and emits an error to stderr."""
        from unittest.mock import MagicMock

        from nephoscope.learners.permission.profiles import _cmd_load

        failing_path = MagicMock(spec=Path)
        failing_path.mkdir.side_effect = OSError("Permission denied")

        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._user_dir",
            lambda: failing_path,
        )

        rc = _cmd_load(["any-id"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "error" in captured.err.lower() or "cannot" in captured.err.lower()


# ===========================================================================
# apply_profile
# ===========================================================================


class TestApplyProfile:
    """Apply a meta-profile YAML file to the DB."""

    def test_permissions_only(self, tmp_db: sqlite3.Connection, tmp_path: Path) -> None:
        """A profile with only permissions inserts permission rows."""
        profile = _write_profile(
            tmp_path / "p.yaml",
            "perms_only",
            "perms only",
            permissions=[
                {"verb": "Read", "flags": [], "decision": "approved"},
            ],
        )

        perms_count, verb_types_count = apply_profile(tmp_db, profile)

        assert perms_count == 1
        assert verb_types_count == 0

        # DB-level evidence: permission row exists.
        row = tmp_db.execute(
            "SELECT decision FROM permissions"
            " WHERE rule_shape_id IN (SELECT id FROM rule_shapes WHERE verb='Read');"
        ).fetchone()
        assert row is not None
        assert row[0] == "approved"

    def test_verb_types_only(self, tmp_db: sqlite3.Connection, tmp_path: Path) -> None:
        """A profile with only verb_types inserts verb_categories rows."""
        profile = _write_profile(
            tmp_path / "p.yaml",
            "verbs_only",
            "verbs only",
            verb_types=[
                {"verb": "alpha", "category": "content_verb"},
                {"verb": "beta", "category": "content_verb"},
            ],
        )

        perms_count, verb_types_count = apply_profile(tmp_db, profile)

        assert perms_count == 0
        assert verb_types_count == 2

        rows = tmp_db.execute(
            "SELECT verb FROM verb_categories WHERE category='content_verb';"
        ).fetchall()
        verbs = {r[0] for r in rows}
        assert verbs == {"alpha", "beta"}

    def test_both_sections(self, tmp_db: sqlite3.Connection, tmp_path: Path) -> None:
        """A profile with both sections returns both counts and writes both."""
        profile = _write_profile(
            tmp_path / "p.yaml",
            "both",
            "both sections",
            permissions=[
                {"verb": "Read", "flags": [], "decision": "approved"},
                {"verb": "Write", "flags": [], "decision": "approved"},
            ],
            verb_types=[
                {"verb": "myverb", "category": "content_verb"},
            ],
        )

        perms_count, verb_types_count = apply_profile(tmp_db, profile)

        assert perms_count == 2
        assert verb_types_count == 1

        assert _count_permissions(tmp_db) == 2
        cat_count = tmp_db.execute(
            "SELECT COUNT(*) FROM verb_categories WHERE verb='myverb';"
        ).fetchone()[0]
        assert cat_count == 1

    def test_missing_meta_raises(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """A YAML without the ``_meta`` block raises ValueError."""
        profile = tmp_path / "no_meta.yaml"
        profile.write_text(
            yaml.dump({"permissions": []}),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="_meta"):
            apply_profile(tmp_db, profile)

    def test_missing_meta_id_raises(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """A YAML where ``_meta`` is present but lacks ``id`` raises ValueError."""
        profile = tmp_path / "no_id.yaml"
        profile.write_text(
            yaml.dump({"_meta": {"description": "no id field"}, "permissions": []}),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="id"):
            apply_profile(tmp_db, profile)

    def test_empty_meta_id_raises(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """A YAML where ``_meta.id`` is the empty string raises ValueError."""
        profile = tmp_path / "empty_id.yaml"
        profile.write_text(
            yaml.dump({"_meta": {"id": "", "description": "empty"}}),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="id"):
            apply_profile(tmp_db, profile)

    def test_flat_list_yaml_raises(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """A YAML body that is a flat list (the legacy fixture form) is invalid."""
        profile = tmp_path / "list.yaml"
        profile.write_text(
            yaml.dump([{"verb": "Read", "flags": [], "decision": "approved"}]),
            encoding="utf-8",
        )

        with pytest.raises(ValueError):
            apply_profile(tmp_db, profile)

    def test_idempotent_db_state(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Applying the same profile twice does not double DB rows.

        ``apply_profile`` is a thin wrapper around ``apply_fixtures`` /
        ``apply_verb_types`` — both have UPSERT / IGNORE semantics. Counting at
        the return-tuple level is misleading (returned counts cover entries
        processed, not rows actually inserted). The honest assertion is that
        the row counts in ``permissions`` and ``verb_categories`` do not grow
        on the second apply.
        """
        profile = _write_profile(
            tmp_path / "p.yaml",
            "idem",
            "idempotent",
            permissions=[{"verb": "Read", "flags": [], "decision": "approved"}],
            verb_types=[{"verb": "myverb", "category": "content_verb"}],
        )

        apply_profile(tmp_db, profile)
        perms_after_first = _count_permissions(tmp_db)
        verbs_after_first = _count_verb_categories(tmp_db)

        apply_profile(tmp_db, profile)
        perms_after_second = _count_permissions(tmp_db)
        verbs_after_second = _count_verb_categories(tmp_db)

        assert perms_after_second == perms_after_first
        assert verbs_after_second == verbs_after_first

    def test_invalid_permission_entry_propagates(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Validation failures from ``apply_fixtures`` propagate as ValueError."""
        profile = _write_profile(
            tmp_path / "p.yaml",
            "bad_perm",
            "bad permission",
            permissions=[{"verb": "Read"}],  # missing decision + flags
        )

        with pytest.raises(ValueError):
            apply_profile(tmp_db, profile)

    def test_invalid_verb_type_entry_propagates(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Validation failures from ``apply_verb_types`` propagate as ValueError."""
        profile = _write_profile(
            tmp_path / "p.yaml",
            "bad_vt",
            "bad verb type",
            verb_types=[{"verb": "myverb", "category": "not_a_real_category"}],
        )

        with pytest.raises(ValueError):
            apply_profile(tmp_db, profile)


# ===========================================================================
# load_profile_by_id
# ===========================================================================


class TestLoadProfileById:
    """Resolve a profile id and apply it."""

    def test_unknown_id_raises(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        bundled.mkdir()
        user.mkdir()
        _write_profile(bundled / "alpha.yaml", "alpha", "Alpha")

        with pytest.raises(ValueError, match="bogus"):
            load_profile_by_id("bogus", tmp_db, bundled_dir=bundled, user_dir=user)

    def test_known_id_loads(self, tmp_db: sqlite3.Connection, tmp_path: Path) -> None:
        """A known id resolves and the underlying apply happens."""
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        bundled.mkdir()
        user.mkdir()
        _write_profile(
            bundled / "alpha.yaml",
            "alpha",
            "Alpha",
            permissions=[{"verb": "Read", "flags": [], "decision": "approved"}],
            verb_types=[{"verb": "myverb", "category": "content_verb"}],
        )

        perms, verbs = load_profile_by_id(
            "alpha", tmp_db, bundled_dir=bundled, user_dir=user
        )

        assert perms == 1
        assert verbs == 1
        assert _count_permissions(tmp_db) == 1
        cat_count = tmp_db.execute(
            "SELECT COUNT(*) FROM verb_categories WHERE verb='myverb';"
        ).fetchone()[0]
        assert cat_count == 1

    def test_returns_counts_tuple(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Return value is exactly ``(permissions_count, verb_types_count)``."""
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        bundled.mkdir()
        user.mkdir()
        _write_profile(
            bundled / "p.yaml",
            "p",
            "p",
            permissions=[
                {"verb": "Read", "flags": [], "decision": "approved"},
                {"verb": "Write", "flags": [], "decision": "approved"},
            ],
            verb_types=[
                {"verb": "v1", "category": "content_verb"},
                {"verb": "v2", "category": "content_verb"},
                {"verb": "v3", "category": "script_runner"},
            ],
        )

        result = load_profile_by_id("p", tmp_db, bundled_dir=bundled, user_dir=user)

        assert isinstance(result, tuple)
        assert len(result) == 2
        assert result == (2, 3)

    def test_user_profile_loads_when_no_collision(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """A user-only profile id resolves and applies."""
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        bundled.mkdir()
        user.mkdir()
        _write_profile(
            user / "mine.yaml",
            "mine",
            "Mine",
            verb_types=[{"verb": "uservb", "category": "content_verb"}],
        )

        perms, verbs = load_profile_by_id(
            "mine", tmp_db, bundled_dir=bundled, user_dir=user
        )

        assert perms == 0
        assert verbs == 1


# ===========================================================================
# CLI — main(argv)
# ===========================================================================


class TestCli:
    """``main()`` argparse entry point with ``list`` and ``load`` subcommands."""

    def test_list_prints_id_source_description(
        self,
        tmp_db: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``main(['list'])`` prints a row per profile with id, source, and description."""
        bundled = tmp_path / "bundled"
        plugin_data = tmp_path / "plugin_data"
        user_profiles = plugin_data / "profiles"
        bundled.mkdir()
        user_profiles.mkdir(parents=True)
        _write_profile(bundled / "core.yaml", "core", "Core profile")
        _write_profile(user_profiles / "extras.yaml", "extras", "Extra goodies")

        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._bundled_dir", lambda: bundled
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))

        rc = main(["list"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "core" in out
        assert "Core profile" in out
        assert "bundled" in out
        assert "extras" in out
        assert "Extra goodies" in out
        assert "user" in out

    def test_load_unknown_id_exits_one(
        self,
        tmp_db: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``main(['load', '<unknown>'])`` exits 1 and prints an error message."""
        bundled = tmp_path / "bundled"
        plugin_data = tmp_path / "plugin_data"
        bundled.mkdir()
        (plugin_data / "profiles").mkdir(parents=True)
        _write_profile(bundled / "core.yaml", "core", "Core")

        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._bundled_dir", lambda: bundled
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))

        rc = main(["load", "does-not-exist"])

        assert rc == 1
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        # Some error wording naming the unknown id should surface.
        assert (
            "does-not-exist" in combined
            or "unknown" in combined.lower()
            or ("not found" in combined.lower())
        )

    def test_load_confirms_then_applies(
        self,
        tmp_db: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``main(['load', 'core'])`` with 'y' (or empty/default) at prompt loads."""
        bundled = tmp_path / "bundled"
        plugin_data = tmp_path / "plugin_data"
        bundled.mkdir()
        (plugin_data / "profiles").mkdir(parents=True)
        _write_profile(
            bundled / "core.yaml",
            "core",
            "Core",
            permissions=[{"verb": "Read", "flags": [], "decision": "approved"}],
            verb_types=[{"verb": "myverb", "category": "content_verb"}],
        )

        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._bundled_dir", lambda: bundled
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))

        # 'y' confirms.
        responses = iter(["y"])
        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._read_line",
            lambda: next(responses),
        )

        rc = main(["load", "core"])

        assert rc == 0
        # DB shows the data was loaded.
        assert _count_permissions(tmp_db) == 1
        cat_count = tmp_db.execute(
            "SELECT COUNT(*) FROM verb_categories WHERE verb='myverb';"
        ).fetchone()[0]
        assert cat_count == 1
        out = capsys.readouterr().out
        assert "loaded" in out.lower()
        assert "1" in out  # at least one count appears

    def test_load_n_at_prompt_does_not_load(
        self,
        tmp_db: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When user types 'n' at the confirm prompt, no apply happens."""
        bundled = tmp_path / "bundled"
        plugin_data = tmp_path / "plugin_data"
        bundled.mkdir()
        (plugin_data / "profiles").mkdir(parents=True)
        _write_profile(
            bundled / "core.yaml",
            "core",
            "Core",
            permissions=[{"verb": "Read", "flags": [], "decision": "approved"}],
            verb_types=[{"verb": "myverb", "category": "content_verb"}],
        )

        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._bundled_dir", lambda: bundled
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))

        # 'n' aborts.
        responses = iter(["n"])
        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._read_line",
            lambda: next(responses),
        )

        # Spy on load_profile_by_id to confirm it is NOT invoked after 'n'.
        called: list[tuple] = []
        original = load_profile_by_id

        def _spy(*args, **kwargs):
            called.append((args, kwargs))
            return original(*args, **kwargs)

        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles.load_profile_by_id", _spy
        )

        rc = main(["load", "core"])

        # Exit code is 0 (graceful abort, not error).
        assert rc == 0
        # The loader was NOT invoked.
        assert called == []
        # And the DB is unchanged.
        assert _count_permissions(tmp_db) == 0
        assert _count_verb_categories(tmp_db) == 0

    def test_load_shows_counts_before_prompt(
        self,
        tmp_db: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The pre-prompt summary names the perm count + verb_type count of the file."""
        bundled = tmp_path / "bundled"
        plugin_data = tmp_path / "plugin_data"
        bundled.mkdir()
        (plugin_data / "profiles").mkdir(parents=True)
        _write_profile(
            bundled / "p.yaml",
            "p",
            "P profile",
            permissions=[
                {"verb": "Read", "flags": [], "decision": "approved"},
                {"verb": "Write", "flags": [], "decision": "approved"},
            ],
            verb_types=[
                {"verb": "v1", "category": "content_verb"},
                {"verb": "v2", "category": "content_verb"},
                {"verb": "v3", "category": "content_verb"},
            ],
        )

        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._bundled_dir", lambda: bundled
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))

        # 'n' so we test the displayed pre-prompt content without applying.
        responses = iter(["n"])
        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._read_line",
            lambda: next(responses),
        )

        rc = main(["load", "p"])

        assert rc == 0
        out = capsys.readouterr().out
        # Counts appear in the pre-prompt summary (parsed from the YAML, not the DB).
        assert "2" in out
        assert "3" in out


# ===========================================================================
# _parse_ids — comma/space normalisation
# ===========================================================================


class TestParseIds:
    """``_parse_ids`` normalises space- and comma-separated id tokens."""

    def test_single_id(self) -> None:
        assert _parse_ids(["git"]) == ["git"]

    def test_space_separated_via_multiple_args(self) -> None:
        assert _parse_ids(["git", "python-dev"]) == ["git", "python-dev"]

    def test_comma_separated_single_arg(self) -> None:
        assert _parse_ids(["git,python-dev"]) == ["git", "python-dev"]

    def test_comma_space_separated(self) -> None:
        assert _parse_ids(["git, python-dev, devops"]) == [
            "git",
            "python-dev",
            "devops",
        ]

    def test_mixed_space_and_comma(self) -> None:
        assert _parse_ids(["git,python-dev", "devops"]) == [
            "git",
            "python-dev",
            "devops",
        ]

    def test_deduplication(self) -> None:
        assert _parse_ids(["git", "git", "python-dev"]) == ["git", "python-dev"]

    def test_empty_tokens_ignored(self) -> None:
        assert _parse_ids(["git,,python-dev"]) == ["git", "python-dev"]


# ===========================================================================
# CLI — multi-id load
# ===========================================================================


class TestCliMultiLoad:
    """``main(['load', ...])`` with multiple profile ids."""

    def test_multi_load_space_separated(
        self,
        tmp_db: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Two space-separated ids are both loaded on confirm."""
        bundled = tmp_path / "bundled"
        plugin_data = tmp_path / "plugin_data"
        bundled.mkdir()
        (plugin_data / "profiles").mkdir(parents=True)
        _write_profile(
            bundled / "alpha.yaml",
            "alpha",
            "Alpha",
            permissions=[{"verb": "Read", "flags": [], "decision": "approved"}],
        )
        _write_profile(
            bundled / "beta.yaml",
            "beta",
            "Beta",
            permissions=[{"verb": "Write", "flags": [], "decision": "approved"}],
        )
        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._bundled_dir", lambda: bundled
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))
        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._read_line", lambda: "y"
        )

        rc = main(["load", "alpha", "beta"])

        assert rc == 0
        assert _count_permissions(tmp_db) == 2

    def test_multi_load_comma_separated(
        self,
        tmp_db: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Comma-separated ids in a single arg are both loaded on confirm."""
        bundled = tmp_path / "bundled"
        plugin_data = tmp_path / "plugin_data"
        bundled.mkdir()
        (plugin_data / "profiles").mkdir(parents=True)
        _write_profile(
            bundled / "alpha.yaml",
            "alpha",
            "Alpha",
            permissions=[{"verb": "Read", "flags": [], "decision": "approved"}],
        )
        _write_profile(
            bundled / "beta.yaml",
            "beta",
            "Beta",
            permissions=[{"verb": "Write", "flags": [], "decision": "approved"}],
        )
        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._bundled_dir", lambda: bundled
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))
        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._read_line", lambda: "y"
        )

        rc = main(["load", "alpha,beta"])

        assert rc == 0
        assert _count_permissions(tmp_db) == 2

    def test_multi_load_one_unknown_exits_one(
        self,
        tmp_db: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """If any id is unknown, exits 1 before prompting."""
        bundled = tmp_path / "bundled"
        plugin_data = tmp_path / "plugin_data"
        bundled.mkdir()
        (plugin_data / "profiles").mkdir(parents=True)
        _write_profile(bundled / "alpha.yaml", "alpha", "Alpha")
        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._bundled_dir", lambda: bundled
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))
        called = []
        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._read_line",
            lambda: called.append(1) or "y",
        )

        rc = main(["load", "alpha", "does-not-exist"])

        assert rc == 1
        assert called == []  # prompt was never reached

    def test_multi_load_n_aborts_all(
        self,
        tmp_db: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """'n' at the single combined prompt aborts all profiles."""
        bundled = tmp_path / "bundled"
        plugin_data = tmp_path / "plugin_data"
        bundled.mkdir()
        (plugin_data / "profiles").mkdir(parents=True)
        _write_profile(
            bundled / "alpha.yaml",
            "alpha",
            "Alpha",
            permissions=[{"verb": "Read", "flags": [], "decision": "approved"}],
        )
        _write_profile(
            bundled / "beta.yaml",
            "beta",
            "Beta",
            permissions=[{"verb": "Write", "flags": [], "decision": "approved"}],
        )
        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._bundled_dir", lambda: bundled
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))
        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._read_line", lambda: "n"
        )

        rc = main(["load", "alpha", "beta"])

        assert rc == 0
        assert _count_permissions(tmp_db) == 0

    def test_multi_load_prompt_says_these_N_profiles(
        self,
        tmp_db: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Prompt wording mentions the count when multiple profiles are selected."""
        bundled = tmp_path / "bundled"
        plugin_data = tmp_path / "plugin_data"
        bundled.mkdir()
        (plugin_data / "profiles").mkdir(parents=True)
        _write_profile(
            bundled / "alpha.yaml",
            "alpha",
            "Alpha",
            permissions=[{"verb": "Read", "flags": [], "decision": "approved"}],
        )
        _write_profile(
            bundled / "beta.yaml",
            "beta",
            "Beta",
            permissions=[{"verb": "Write", "flags": [], "decision": "approved"}],
        )
        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._bundled_dir", lambda: bundled
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))
        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._read_line", lambda: "n"
        )

        main(["load", "alpha", "beta"])

        out = capsys.readouterr().out
        assert "2" in out


# ===========================================================================
# Doom-path gap tests
# ===========================================================================


class TestApplyProfileDoomPath:
    """Doom-path gap coverage for apply_profile."""

    def test_meta_null_raises_value_error(
        self, tmp_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """``_meta: null`` passes the ``'_meta' not in data`` check but must raise ValueError.

        YAML ``_meta: null`` yields ``{"_meta": None}``; calling ``.get("id")`` on None
        raises AttributeError without the isinstance guard added in MEDIUM-1.
        """
        profile = tmp_path / "null_meta.yaml"
        profile.write_text("_meta: null\npermissions: []\n", encoding="utf-8")

        with pytest.raises(ValueError, match="_meta"):
            apply_profile(tmp_db, profile)

    def test_list_profiles_idempotent(self, tmp_path: Path) -> None:
        """Two consecutive calls to list_profiles return identical results."""
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        bundled.mkdir()
        user.mkdir()
        _write_profile(bundled / "alpha.yaml", "alpha", "Alpha profile")
        _write_profile(bundled / "beta.yaml", "beta", "Beta profile")

        first = list_profiles(bundled_dir=bundled, user_dir=user)
        second = list_profiles(bundled_dir=bundled, user_dir=user)

        assert first == second

    def test_list_profiles_non_ascii_filename_skipped_gracefully(
        self, tmp_path: Path
    ) -> None:
        """A YAML file whose name contains non-ASCII characters does not raise.

        The file either loads (if valid YAML with a proper _meta) or is skipped
        due to missing _meta — in neither case should list_profiles raise.
        """
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        bundled.mkdir()
        user.mkdir()
        _write_profile(bundled / "normal.yaml", "normal", "Normal profile")
        # Non-ASCII filename with no _meta — should be silently skipped.
        unicode_path = bundled / "écart.yaml"
        unicode_path.write_text(yaml.dump({"permissions": []}), encoding="utf-8")

        # Must not raise; the non-ASCII file is skipped, normal one is present.
        entries = list_profiles(bundled_dir=bundled, user_dir=user)
        ids = [e.id for e in entries]
        assert "normal" in ids

    def test_cmd_load_out_of_bounds_index_exits_one(
        self,
        tmp_db: sqlite3.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``_cmd_load`` with an out-of-bounds id string (not a valid profile id) exits 1.

        The ``load`` subcommand accepts profile ids (strings), not 1-indexed integers.
        Passing an id that matches no profile must exit 1 with an error message.
        Indexes 0 and beyond-max are out of range in the list sense; for the CLI,
        this maps to an unknown id.
        """
        bundled = tmp_path / "bundled"
        plugin_data = tmp_path / "plugin_data"
        bundled.mkdir()
        (plugin_data / "profiles").mkdir(parents=True)
        _write_profile(bundled / "only.yaml", "only", "Only profile")

        monkeypatch.setattr(
            "nephoscope.learners.permission.profiles._bundled_dir", lambda: bundled
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))

        # "0" and "99" are not valid profile ids — both should exit 1.
        for bad_id in ("0", "99"):
            rc = main(["load", bad_id])
            assert rc == 1, f"Expected exit 1 for unknown id {bad_id!r}, got {rc}"
            captured = capsys.readouterr()
            combined = captured.out + captured.err
            assert bad_id in combined or "not found" in combined.lower(), (
                f"Expected error message containing {bad_id!r}"
            )


# ===========================================================================
# Phase B4 — credential-file-tools meta-profile
# ===========================================================================


class TestCredentialFileToolsProfile:
    """Integration tests for the credential-file-tools meta-profile fixture.

    The profile must load via apply_profile and produce deny entries for
    Read, Write, and Edit tools on credential file paths.  .env.example must
    NOT appear.
    """

    def test_profile_file_exists(self):
        """credential-file-tools.yaml must exist in the bundled meta-profiles dir."""
        from nephoscope.learners.permission.profiles import _bundled_dir

        profile_path = _bundled_dir() / "credential-file-tools.yaml"
        assert profile_path.exists(), (
            f"credential-file-tools.yaml not found at {profile_path}"
        )

    def test_apply_profile_loads_read_env_deny(self, tmp_db, tmp_path):
        """apply_profile → DB contains a rejected rule for Read/**/.env."""
        from nephoscope.learners.permission.profiles import _bundled_dir, apply_profile

        profile_path = _bundled_dir() / "credential-file-tools.yaml"
        apply_profile(tmp_db, profile_path)
        tmp_db.commit()

        row = tmp_db.execute(
            "SELECT rs.tool, rs.path_spec, p.decision"
            " FROM rule_shapes rs JOIN permissions p ON p.rule_shape_id = rs.id"
            " WHERE rs.verb = 'Read' AND rs.path_spec = '**/.env' AND rs.tool = 'Read';"
        ).fetchone()
        assert row is not None, "No Read/**/.env rule found after loading profile"
        assert row[0] == "Read"
        assert row[1] == "**/.env"
        assert row[2] == "rejected"

    def test_apply_profile_loads_write_env_deny(self, tmp_db, tmp_path):
        """apply_profile → DB contains a rejected rule for Write/**/.env."""
        from nephoscope.learners.permission.profiles import _bundled_dir, apply_profile

        profile_path = _bundled_dir() / "credential-file-tools.yaml"
        apply_profile(tmp_db, profile_path)
        tmp_db.commit()

        row = tmp_db.execute(
            "SELECT rs.tool, p.decision"
            " FROM rule_shapes rs JOIN permissions p ON p.rule_shape_id = rs.id"
            " WHERE rs.verb = 'Write' AND rs.path_spec = '**/.env' AND rs.tool = 'Write';"
        ).fetchone()
        assert row is not None, "No Write/**/.env rule found after loading profile"
        assert row[0] == "Write"
        assert row[1] == "rejected"

    def test_apply_profile_loads_edit_env_deny(self, tmp_db, tmp_path):
        """apply_profile → DB contains a rejected rule for Edit/**/.env."""
        from nephoscope.learners.permission.profiles import _bundled_dir, apply_profile

        profile_path = _bundled_dir() / "credential-file-tools.yaml"
        apply_profile(tmp_db, profile_path)
        tmp_db.commit()

        row = tmp_db.execute(
            "SELECT rs.tool, p.decision"
            " FROM rule_shapes rs JOIN permissions p ON p.rule_shape_id = rs.id"
            " WHERE rs.verb = 'Edit' AND rs.path_spec = '**/.env' AND rs.tool = 'Edit';"
        ).fetchone()
        assert row is not None, "No Edit/**/.env rule found after loading profile"
        assert row[0] == "Edit"
        assert row[1] == "rejected"

    def test_env_example_not_in_deny_rules(self, tmp_db, tmp_path):
        """After loading the profile, no rule matches .env.example path_specs."""
        from nephoscope.learners.permission.profiles import _bundled_dir, apply_profile

        profile_path = _bundled_dir() / "credential-file-tools.yaml"
        apply_profile(tmp_db, profile_path)
        tmp_db.commit()

        # Check no path_spec contains .env.example or .env.template
        rows = tmp_db.execute(
            "SELECT path_spec FROM rule_shapes"
            " WHERE path_spec LIKE '%.env.example%' OR path_spec LIKE '%.env.template%';"
        ).fetchall()
        assert rows == [], (
            f".env.example or .env.template path_specs found: {[r[0] for r in rows]}"
        )

    def test_mirror_deny_list_contains_read_env(self, tmp_db, tmp_path):
        """After loading and syncing, settings.json deny list contains Read(**/.env)."""
        import json
        from unittest.mock import patch

        from nephoscope.learners.permission.profiles import _bundled_dir, apply_profile
        from nephoscope.lib.mirror.writer import sync_global

        profile_path = _bundled_dir() / "credential-file-tools.yaml"
        apply_profile(tmp_db, profile_path)

        # Seed the global_mirror singleton pointing at a temp settings.json
        fake_settings = tmp_path / "settings.json"
        tmp_db.execute(
            "INSERT OR REPLACE INTO global_mirror"
            " (id, settings_json_path, settings_json_sha256, settings_json_last_synced)"
            " VALUES (1, ?, NULL, NULL);",
            (str(fake_settings),),
        )
        tmp_db.commit()

        with patch("nephoscope.config.get_config") as mock_cfg:
            mock_cfg.return_value.trusted_dirs = []
            sync_global(tmp_db)

        data = json.loads(fake_settings.read_text())
        deny = data["permissions"]["deny"]
        assert "Read(**/.env)" in deny, (
            f"Expected Read(**/.env) in deny list, got: {deny}"
        )
        assert "Write(**/.env)" in deny, (
            f"Expected Write(**/.env) in deny list, got: {deny}"
        )
        assert "Edit(**/.env)" in deny, (
            f"Expected Edit(**/.env) in deny list, got: {deny}"
        )


# ===========================================================================
# dev-tools meta-profile — new rules: stat, chmod +x, rm -rf
# ===========================================================================


class TestDevToolsNewRules:
    """Integration tests for dev-tools.yaml rules: stat, find, tail, head, chmod +x, and rm -rf."""

    @staticmethod
    def _load_dev_tools_permissions() -> list[dict]:
        """Load the permissions list from the bundled dev-tools.yaml."""
        from nephoscope.learners.permission.profiles import _bundled_dir

        profile_path = _bundled_dir() / "dev-tools.yaml"
        import yaml

        data = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        return data.get("permissions", [])

    # ------------------------------------------------------------------
    # find
    # ------------------------------------------------------------------

    def test_find_rule_present_with_wildcard_flags(self) -> None:
        """dev-tools profile contains a find rule with flags='*' and no path_spec."""
        perms = self._load_dev_tools_permissions()
        find_rules = [p for p in perms if p.get("verb") == "find"]
        assert find_rules, "No find rule found in dev-tools.yaml"
        rule = find_rules[0]
        assert rule.get("flags") == "*"
        assert rule.get("path_spec") is None
        assert rule.get("decision") == "approved"

    # ------------------------------------------------------------------
    # stat
    # ------------------------------------------------------------------

    def test_stat_rule_present_with_wildcard_flags(self) -> None:
        """dev-tools profile contains a stat rule with flags='*' and no path_spec."""
        perms = self._load_dev_tools_permissions()
        stat_rules = [p for p in perms if p.get("verb") == "stat"]
        assert stat_rules, "No stat rule found in dev-tools.yaml"
        rule = stat_rules[0]
        assert rule.get("flags") == "*", (
            f"Expected flags='*' for stat rule, got {rule.get('flags')!r}"
        )
        assert rule.get("path_spec") is None, (
            f"Expected no path_spec for stat rule, got {rule.get('path_spec')!r}"
        )
        assert rule.get("decision") == "approved"

    # ------------------------------------------------------------------
    # chmod +x
    # ------------------------------------------------------------------

    def test_chmod_plus_x_rules_cover_three_path_specs(self) -> None:
        """dev-tools profile has chmod +x rules for all three expected path_specs."""
        perms = self._load_dev_tools_permissions()
        chmod_rules = [
            p for p in perms if p.get("verb") == "chmod" and p.get("subcommand") == "+x"
        ]
        actual_specs = {r.get("path_spec") for r in chmod_rules}
        expected_specs = {
            "$JUNK_DIR/**",
            "$TRUSTED_DIR/**",
            "$PROJECT_ROOT/**",
        }
        assert expected_specs == actual_specs, (
            f"chmod +x path_specs mismatch: expected {expected_specs}, got {actual_specs}"
        )

    def test_chmod_plus_x_rules_all_approved(self) -> None:
        """All chmod +x rules in dev-tools are approved."""
        perms = self._load_dev_tools_permissions()
        chmod_rules = [
            p for p in perms if p.get("verb") == "chmod" and p.get("subcommand") == "+x"
        ]
        for rule in chmod_rules:
            assert rule.get("decision") == "approved", (
                f"chmod +x rule for {rule.get('path_spec')!r} is not approved"
            )
            assert rule.get("flags") == [], (
                f"Expected flags=[] for chmod +x rule, got {rule.get('flags')!r}"
            )

    # ------------------------------------------------------------------
    # rm -rf
    # ------------------------------------------------------------------

    def test_rm_rf_rules_cover_three_path_specs(self) -> None:
        """dev-tools profile has rm -rf rules for all three expected path_specs."""
        perms = self._load_dev_tools_permissions()
        rm_rules = [p for p in perms if p.get("verb") == "rm"]
        actual_specs = {r.get("path_spec") for r in rm_rules}
        expected_specs = {
            "$JUNK_DIR/**",
            "$TRUSTED_DIR/**",
            "$PROJECT_ROOT/**",
        }
        assert expected_specs == actual_specs, (
            f"rm rules path_specs mismatch: expected {expected_specs}, got {actual_specs}"
        )

    def test_rm_rf_rules_all_approved_with_correct_flags(self) -> None:
        """All rm rules in dev-tools are approved and carry [-f, -r] flags."""
        perms = self._load_dev_tools_permissions()
        rm_rules = [p for p in perms if p.get("verb") == "rm"]
        for rule in rm_rules:
            assert rule.get("decision") == "approved", (
                f"rm rule for {rule.get('path_spec')!r} is not approved"
            )
            flags = rule.get("flags", [])
            assert set(flags) == {"-f", "-r"}, (
                f"rm rule for {rule.get('path_spec')!r} has unexpected flags: {flags!r}"
            )

    # ------------------------------------------------------------------
    # DB round-trip: apply_profile inserts selected new rules
    # ------------------------------------------------------------------

    def test_apply_dev_tools_inserts_stat_rule(self, tmp_db) -> None:
        """apply_profile on dev-tools.yaml writes a stat/wildcard rule to the DB."""
        from nephoscope.learners.permission.profiles import _bundled_dir, apply_profile

        profile_path = _bundled_dir() / "dev-tools.yaml"
        apply_profile(tmp_db, profile_path)
        tmp_db.commit()

        row = tmp_db.execute(
            "SELECT rs.flags, p.decision"
            " FROM rule_shapes rs JOIN permissions p ON p.rule_shape_id = rs.id"
            " WHERE rs.verb = 'stat';"
        ).fetchone()
        assert row is not None, (
            "No stat rule found in DB after loading dev-tools profile"
        )
        assert row[0] == "*", f"Expected flags='*' for stat rule in DB, got {row[0]!r}"
        assert row[1] == "approved"

    def test_apply_dev_tools_inserts_chmod_rules(self, tmp_db) -> None:
        """apply_profile on dev-tools.yaml writes all three chmod +x rules to the DB."""
        from nephoscope.learners.permission.profiles import _bundled_dir, apply_profile

        profile_path = _bundled_dir() / "dev-tools.yaml"
        apply_profile(tmp_db, profile_path)
        tmp_db.commit()

        rows = tmp_db.execute(
            "SELECT rs.subcommand, rs.path_spec, p.decision"
            " FROM rule_shapes rs JOIN permissions p ON p.rule_shape_id = rs.id"
            " WHERE rs.verb = 'chmod';"
        ).fetchall()
        path_specs = {r[1] for r in rows}
        expected = {
            "$JUNK_DIR/**",
            "$TRUSTED_DIR/**",
            "$PROJECT_ROOT/**",
        }
        assert expected == path_specs, (
            f"chmod path_specs in DB: expected {expected}, got {path_specs}"
        )
        for row in rows:
            assert row[0] == "+x", f"Unexpected chmod subcommand in DB: {row[0]!r}"
            assert row[2] == "approved"

    def test_apply_dev_tools_inserts_rm_rules(self, tmp_db) -> None:
        """apply_profile on dev-tools.yaml writes all three rm rules to the DB."""
        from nephoscope.learners.permission.profiles import _bundled_dir, apply_profile

        profile_path = _bundled_dir() / "dev-tools.yaml"
        apply_profile(tmp_db, profile_path)
        tmp_db.commit()

        rows = tmp_db.execute(
            "SELECT rs.path_spec, rs.flags, p.decision"
            " FROM rule_shapes rs JOIN permissions p ON p.rule_shape_id = rs.id"
            " WHERE rs.verb = 'rm';"
        ).fetchall()
        path_specs = {r[0] for r in rows}
        expected = {
            "$JUNK_DIR/**",
            "$TRUSTED_DIR/**",
            "$PROJECT_ROOT/**",
        }
        assert expected == path_specs, (
            f"rm path_specs in DB: expected {expected}, got {path_specs}"
        )
        for row in rows:
            assert row[2] == "approved"

    def test_apply_dev_tools_inserts_find_rule(self, tmp_db) -> None:
        """apply_profile on dev-tools.yaml writes a find/wildcard rule to the DB."""
        from nephoscope.learners.permission.profiles import _bundled_dir, apply_profile

        profile_path = _bundled_dir() / "dev-tools.yaml"
        apply_profile(tmp_db, profile_path)
        tmp_db.commit()

        row = tmp_db.execute(
            "SELECT rs.flags, p.decision, rs.path_spec, p.danger_accepted"
            " FROM rule_shapes rs JOIN permissions p ON p.rule_shape_id = rs.id"
            " WHERE rs.verb = 'find';"
        ).fetchone()
        assert row is not None, (
            "No find rule found in DB after loading dev-tools profile"
        )
        assert row[0] == "*", f"Expected flags='*' for find rule in DB, got {row[0]!r}"
        assert row[1] == "approved"
        assert row[2] is None, f"Expected no path_spec for find rule in DB, got {row[2]!r}"
        assert row[3] == "wildcard_hides_dangerous_flag"

    def test_apply_dev_tools_inserts_tail_rule(self, tmp_db) -> None:
        """apply_profile on dev-tools.yaml writes a tail/wildcard rule to the DB."""
        from nephoscope.learners.permission.profiles import _bundled_dir, apply_profile

        profile_path = _bundled_dir() / "dev-tools.yaml"
        apply_profile(tmp_db, profile_path)
        tmp_db.commit()

        row = tmp_db.execute(
            "SELECT rs.flags, p.decision, rs.path_spec"
            " FROM rule_shapes rs JOIN permissions p ON p.rule_shape_id = rs.id"
            " WHERE rs.verb = 'tail';"
        ).fetchone()
        assert row is not None, (
            "No tail rule found in DB after loading dev-tools profile"
        )
        assert row[0] == "*", f"Expected flags='*' for tail rule in DB, got {row[0]!r}"
        assert row[1] == "approved"
        assert row[2] is None, f"Expected no path_spec for tail rule in DB, got {row[2]!r}"

    def test_apply_dev_tools_inserts_head_rule(self, tmp_db) -> None:
        """apply_profile on dev-tools.yaml writes a head/wildcard rule to the DB."""
        from nephoscope.learners.permission.profiles import _bundled_dir, apply_profile

        profile_path = _bundled_dir() / "dev-tools.yaml"
        apply_profile(tmp_db, profile_path)
        tmp_db.commit()

        row = tmp_db.execute(
            "SELECT rs.flags, p.decision, rs.path_spec"
            " FROM rule_shapes rs JOIN permissions p ON p.rule_shape_id = rs.id"
            " WHERE rs.verb = 'head';"
        ).fetchone()
        assert row is not None, (
            "No head rule found in DB after loading dev-tools profile"
        )
        assert row[0] == "*", f"Expected flags='*' for head rule in DB, got {row[0]!r}"
        assert row[1] == "approved"
        assert row[2] is None, f"Expected no path_spec for head rule in DB, got {row[2]!r}"

    # ------------------------------------------------------------------
    # tail / head
    # ------------------------------------------------------------------

    def test_tail_rule_present_with_wildcard_flags(self) -> None:
        """dev-tools profile contains a tail rule with flags='*' and no path_spec."""
        perms = self._load_dev_tools_permissions()
        tail_rules = [p for p in perms if p.get("verb") == "tail"]
        assert tail_rules, "No tail rule found in dev-tools.yaml"
        rule = tail_rules[0]
        assert rule.get("flags") == "*", (
            f"Expected flags='*' for tail rule, got {rule.get('flags')!r}"
        )
        assert rule.get("path_spec") is None
        assert rule.get("decision") == "approved"

    def test_head_rule_present_with_wildcard_flags(self) -> None:
        """dev-tools profile contains a head rule with flags='*' and no path_spec."""
        perms = self._load_dev_tools_permissions()
        head_rules = [p for p in perms if p.get("verb") == "head"]
        assert head_rules, "No head rule found in dev-tools.yaml"
        rule = head_rules[0]
        assert rule.get("flags") == "*", (
            f"Expected flags='*' for head rule, got {rule.get('flags')!r}"
        )
        assert rule.get("path_spec") is None
        assert rule.get("decision") == "approved"


# ===========================================================================
# python-dev meta-profile — uv run uses flags="*"
# ===========================================================================


class TestPythonDevUvRunRule:
    """The uv run rule in python-dev.yaml must use flags='*' so that flags
    passed to the wrapped command (e.g. pytest -q) do not cause a mismatch."""

    @staticmethod
    def _load_python_dev_permissions() -> list[dict]:
        from nephoscope.learners.permission.profiles import _bundled_dir

        profile_path = _bundled_dir() / "python-dev.yaml"
        import yaml

        data = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        return data.get("permissions", [])

    def test_uv_run_rule_uses_wildcard_flags(self) -> None:
        """uv run rule has flags='*' so pytest -q / mypy / etc. are covered."""
        perms = self._load_python_dev_permissions()
        uv_run_rules = [
            p for p in perms if p.get("verb") == "uv" and p.get("subcommand") == "run"
        ]
        assert uv_run_rules, "No uv run rule found in python-dev.yaml"
        rule = uv_run_rules[0]
        assert rule.get("flags") == "*", (
            f"uv run rule must have flags='*' (wrapped-tool flags leak into the shape); "
            f"got {rule.get('flags')!r}"
        )
