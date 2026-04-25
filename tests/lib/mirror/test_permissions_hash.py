"""Tests for lib.mirror.permissions_hash — canonical permissions-slice SHA-256.

All tests exercise `settings_permissions_hash(content: bytes) -> str`.
The hash is computed over the sorted {allow, deny, ask} slice only;
everything else in the JSON blob is invisible to it.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from nephoscope.lib.mirror.permissions_hash import settings_permissions_hash


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _blob(data: dict) -> bytes:
    """Serialise *data* to bytes as settings_permissions_hash would receive."""
    return json.dumps(data).encode("utf-8")


def _empty_canonical() -> str:
    """Expected hash for an empty permissions slice (all three arrays empty)."""
    canonical = {"allow": [], "deny": [], "ask": []}
    rendered = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Empty / missing permissions key — all three forms must agree
# ---------------------------------------------------------------------------


class TestEmptyEquivalence:
    def test_missing_permissions_key(self):
        """A blob with no 'permissions' key hashes the same as empty arrays."""
        result = settings_permissions_hash(_blob({}))
        assert result == _empty_canonical()

    def test_empty_permissions_dict(self):
        """permissions: {} (key present, value empty) hashes identically to missing."""
        result = settings_permissions_hash(_blob({"permissions": {}}))
        assert result == _empty_canonical()

    def test_empty_blob_braces(self):
        """The minimal valid JSON object {} hashes identically to missing permissions."""
        result = settings_permissions_hash(b"{}")
        assert result == _empty_canonical()

    def test_explicit_empty_arrays_match(self):
        """Explicit empty allow/deny/ask also produce the canonical empty hash."""
        blob = _blob({"permissions": {"allow": [], "deny": [], "ask": []}})
        result = settings_permissions_hash(blob)
        assert result == _empty_canonical()

    def test_all_three_forms_are_identical(self):
        """Missing key, empty dict, and empty arrays all return the same digest."""
        h_missing = settings_permissions_hash(_blob({}))
        h_empty_dict = settings_permissions_hash(_blob({"permissions": {}}))
        h_empty_arrays = settings_permissions_hash(
            _blob({"permissions": {"allow": [], "deny": [], "ask": []}})
        )
        assert h_missing == h_empty_dict == h_empty_arrays


# ---------------------------------------------------------------------------
# Sort invariance — order of entries must not affect the hash
# ---------------------------------------------------------------------------


class TestSortInvariance:
    def test_allow_order_does_not_matter(self):
        """Two blobs with allow entries in different orders produce the same hash."""
        a = _blob({"permissions": {"allow": ["Bash(*)", "Read(*)"]}})
        b = _blob({"permissions": {"allow": ["Read(*)", "Bash(*)"]}})
        assert settings_permissions_hash(a) == settings_permissions_hash(b)

    def test_deny_order_does_not_matter(self):
        """Two blobs with deny entries in different orders produce the same hash."""
        a = _blob({"permissions": {"deny": ["Bash(rm *)", "Write(*)"]}})
        b = _blob({"permissions": {"deny": ["Write(*)", "Bash(rm *)"]}})
        assert settings_permissions_hash(a) == settings_permissions_hash(b)

    def test_ask_order_does_not_matter(self):
        """Two blobs with ask entries in different orders produce the same hash."""
        a = _blob({"permissions": {"ask": ["mcp__tool_a", "mcp__tool_b"]}})
        b = _blob({"permissions": {"ask": ["mcp__tool_b", "mcp__tool_a"]}})
        assert settings_permissions_hash(a) == settings_permissions_hash(b)

    def test_multi_array_sort_invariance(self):
        """Shuffled entries across all three arrays still hash identically."""
        a = _blob(
            {
                "permissions": {
                    "allow": ["Z", "A", "M"],
                    "deny": ["beta", "alpha"],
                    "ask": ["two", "one"],
                }
            }
        )
        b = _blob(
            {
                "permissions": {
                    "allow": ["A", "M", "Z"],
                    "deny": ["alpha", "beta"],
                    "ask": ["one", "two"],
                }
            }
        )
        assert settings_permissions_hash(a) == settings_permissions_hash(b)


# ---------------------------------------------------------------------------
# Isolation — fields outside permissions must not affect the hash
# ---------------------------------------------------------------------------


class TestIsolation:
    def test_hooks_block_does_not_affect_hash(self):
        """Adding a hooks block to the file leaves the permissions hash unchanged."""
        base = _blob({"permissions": {"allow": ["Bash(git *)"], "deny": [], "ask": []}})
        with_hooks = _blob(
            {
                "permissions": {"allow": ["Bash(git *)"], "deny": [], "ask": []},
                "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]},
            }
        )
        assert settings_permissions_hash(base) == settings_permissions_hash(with_hooks)

    def test_env_block_does_not_affect_hash(self):
        """Adding an env block to the file leaves the permissions hash unchanged."""
        base = _blob({"permissions": {"allow": ["Read(*)"], "deny": [], "ask": []}})
        with_env = _blob(
            {
                "permissions": {"allow": ["Read(*)"], "deny": [], "ask": []},
                "env": {"FOO": "bar"},
            }
        )
        assert settings_permissions_hash(base) == settings_permissions_hash(with_env)

    def test_model_field_does_not_affect_hash(self):
        """Changing the model field does not flip the permissions hash."""
        base = _blob({"permissions": {"allow": [], "deny": [], "ask": []}})
        with_model = _blob(
            {
                "permissions": {"allow": [], "deny": [], "ask": []},
                "model": "claude-opus-4-6",
            }
        )
        assert settings_permissions_hash(base) == settings_permissions_hash(with_model)

    def test_permissions_default_mode_does_not_affect_hash(self):
        """permissions.defaultMode (outside the three arrays) does not affect hash."""
        base = _blob({"permissions": {"allow": [], "deny": [], "ask": []}})
        with_mode = _blob(
            {
                "permissions": {
                    "allow": [],
                    "deny": [],
                    "ask": [],
                    "defaultMode": "auto",
                }
            }
        )
        assert settings_permissions_hash(base) == settings_permissions_hash(with_mode)

    def test_permissions_additional_directories_does_not_affect_hash(self):
        """permissions.additionalDirectories does not affect hash."""
        base = _blob({"permissions": {"allow": [], "deny": [], "ask": []}})
        with_dirs = _blob(
            {
                "permissions": {
                    "allow": [],
                    "deny": [],
                    "ask": [],
                    "additionalDirectories": ["/tmp"],
                }
            }
        )
        assert settings_permissions_hash(base) == settings_permissions_hash(with_dirs)

    def test_multiple_unrelated_fields_combined(self):
        """Adding hooks + env + model together still leaves the hash unchanged."""
        perms = {"allow": ["Bash(git *)"], "deny": ["Bash(rm *)"], "ask": []}
        base = _blob({"permissions": perms})
        with_extras = _blob(
            {
                "permissions": perms,
                "hooks": {},
                "env": {"X": "1"},
                "model": "claude-haiku-4-5",
                "additionalDirectories": ["/home"],
            }
        )
        assert settings_permissions_hash(base) == settings_permissions_hash(with_extras)


# ---------------------------------------------------------------------------
# Sensitivity — meaningful permission changes must change the hash
# ---------------------------------------------------------------------------


class TestSensitivity:
    def test_adding_entry_to_allow_changes_hash(self):
        """Adding a rule to allow must produce a different hash."""
        before = _blob({"permissions": {"allow": [], "deny": [], "ask": []}})
        after = _blob(
            {"permissions": {"allow": ["Bash(git *)"], "deny": [], "ask": []}}
        )
        assert settings_permissions_hash(before) != settings_permissions_hash(after)

    def test_moving_entry_from_allow_to_deny_changes_hash(self):
        """Moving an entry from allow to deny must produce a different hash."""
        allow_side = _blob(
            {"permissions": {"allow": ["Bash(rm *)"], "deny": [], "ask": []}}
        )
        deny_side = _blob(
            {"permissions": {"allow": [], "deny": ["Bash(rm *)"], "ask": []}}
        )
        assert settings_permissions_hash(allow_side) != settings_permissions_hash(
            deny_side
        )

    def test_adding_entry_to_deny_changes_hash(self):
        """Adding a rule to deny must produce a different hash."""
        before = _blob({"permissions": {"allow": [], "deny": [], "ask": []}})
        after = _blob({"permissions": {"allow": [], "deny": ["Write(*)"], "ask": []}})
        assert settings_permissions_hash(before) != settings_permissions_hash(after)

    def test_adding_entry_to_ask_changes_hash(self):
        """Adding a rule to ask must produce a different hash."""
        before = _blob({"permissions": {"allow": [], "deny": [], "ask": []}})
        after = _blob({"permissions": {"allow": [], "deny": [], "ask": ["mcp__foo"]}})
        assert settings_permissions_hash(before) != settings_permissions_hash(after)

    def test_removing_entry_changes_hash(self):
        """Removing a rule from allow must produce a different hash."""
        with_rule = _blob(
            {"permissions": {"allow": ["Bash(git *)"], "deny": [], "ask": []}}
        )
        without_rule = _blob({"permissions": {"allow": [], "deny": [], "ask": []}})
        assert settings_permissions_hash(with_rule) != settings_permissions_hash(
            without_rule
        )


# ---------------------------------------------------------------------------
# Duplicate entries — preserved, not deduplicated
# ---------------------------------------------------------------------------


class TestDuplicates:
    def test_duplicates_are_preserved_not_deduped(self):
        """Duplicate entries must not be silently removed — two copies != one copy."""
        one_copy = _blob(
            {"permissions": {"allow": ["Bash(git *)"], "deny": [], "ask": []}}
        )
        two_copies = _blob(
            {
                "permissions": {
                    "allow": ["Bash(git *)", "Bash(git *)"],
                    "deny": [],
                    "ask": [],
                }
            }
        )
        assert settings_permissions_hash(one_copy) != settings_permissions_hash(
            two_copies
        )

    def test_duplicates_sorted_consistently(self):
        """Duplicate entries in different positions hash identically (sort only, no dedup)."""
        a = _blob({"permissions": {"allow": ["B", "A", "B"], "deny": [], "ask": []}})
        b = _blob({"permissions": {"allow": ["B", "B", "A"], "deny": [], "ask": []}})
        assert settings_permissions_hash(a) == settings_permissions_hash(b)


# ---------------------------------------------------------------------------
# Non-ASCII / unicode entries — deterministic across code points
# ---------------------------------------------------------------------------


class TestUnicode:
    def test_unicode_entry_produces_deterministic_hash(self):
        """A unicode permission entry produces a stable, repeatable hash."""
        blob = _blob(
            {"permissions": {"allow": ["Bash(読む *)"], "deny": [], "ask": []}}
        )
        h1 = settings_permissions_hash(blob)
        h2 = settings_permissions_hash(blob)
        assert h1 == h2

    def test_unicode_sort_order(self):
        """Unicode entries are sorted by codepoint order, giving a stable result."""
        a = _blob({"permissions": {"allow": ["ñoño", "alpha"], "deny": [], "ask": []}})
        b = _blob({"permissions": {"allow": ["alpha", "ñoño"], "deny": [], "ask": []}})
        assert settings_permissions_hash(a) == settings_permissions_hash(b)

    def test_unicode_vs_ascii_differs(self):
        """An entry with a unicode char differs from its ASCII-only counterpart."""
        ascii_blob = _blob(
            {"permissions": {"allow": ["Bash(*)"], "deny": [], "ask": []}}
        )
        unicode_blob = _blob(
            {"permissions": {"allow": ["Bash(★)"], "deny": [], "ask": []}}
        )
        assert settings_permissions_hash(ascii_blob) != settings_permissions_hash(
            unicode_blob
        )


# ---------------------------------------------------------------------------
# Hash format — output is a 64-char lowercase hex SHA-256 digest
# ---------------------------------------------------------------------------


class TestHashFormat:
    def test_returns_string(self):
        """Return type is str."""
        result = settings_permissions_hash(b"{}")
        assert isinstance(result, str)

    def test_64_hex_chars(self):
        """Output is a 64-character lowercase hex string."""
        result = settings_permissions_hash(b"{}")
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_deterministic_across_calls(self):
        """Same input always returns the same digest."""
        blob = _blob({"permissions": {"allow": ["Read(*)"], "deny": [], "ask": []}})
        assert settings_permissions_hash(blob) == settings_permissions_hash(blob)


# ---------------------------------------------------------------------------
# Nested permissions dict shapes — robustness against real-world variation
# ---------------------------------------------------------------------------


class TestPermissionsShapes:
    def test_only_allow_present(self):
        """Only allow key present — missing deny and ask treated as empty."""
        a = _blob({"permissions": {"allow": ["Read(*)"]}})
        b = _blob({"permissions": {"allow": ["Read(*)"], "deny": [], "ask": []}})
        assert settings_permissions_hash(a) == settings_permissions_hash(b)

    def test_only_deny_present(self):
        """Only deny key present — missing allow and ask treated as empty."""
        a = _blob({"permissions": {"deny": ["Write(*)"]}})
        b = _blob({"permissions": {"allow": [], "deny": ["Write(*)"], "ask": []}})
        assert settings_permissions_hash(a) == settings_permissions_hash(b)

    def test_permissions_value_is_none(self):
        """permissions: null in JSON — treated same as missing (empty canonical)."""
        blob = json.dumps({"permissions": None}).encode("utf-8")
        assert settings_permissions_hash(blob) == _empty_canonical()

    def test_allow_is_none(self):
        """permissions.allow: null treated as empty list."""
        a = _blob({"permissions": {"allow": None, "deny": [], "ask": []}})
        b = _blob({"permissions": {"allow": [], "deny": [], "ask": []}})
        assert settings_permissions_hash(a) == settings_permissions_hash(b)

    def test_deny_is_none(self):
        """permissions.deny: null treated as empty list."""
        a = _blob({"permissions": {"allow": [], "deny": None, "ask": []}})
        b = _blob({"permissions": {"allow": [], "deny": [], "ask": []}})
        assert settings_permissions_hash(a) == settings_permissions_hash(b)

    def test_ask_is_none(self):
        """permissions.ask: null treated as empty list."""
        a = _blob({"permissions": {"allow": [], "deny": [], "ask": None}})
        b = _blob({"permissions": {"allow": [], "deny": [], "ask": []}})
        assert settings_permissions_hash(a) == settings_permissions_hash(b)


# ---------------------------------------------------------------------------
# Error handling — malformed JSON must propagate
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_malformed_json_raises_json_decode_error(self):
        """Malformed JSON bytes raise json.JSONDecodeError — not silently swallowed."""
        with pytest.raises(json.JSONDecodeError):
            settings_permissions_hash(b"not valid json")

    def test_truncated_json_raises_json_decode_error(self):
        """Truncated JSON raises json.JSONDecodeError."""
        with pytest.raises(json.JSONDecodeError):
            settings_permissions_hash(b'{"permissions": {')

    def test_empty_bytes_raises_json_decode_error(self):
        """Empty input bytes raise json.JSONDecodeError."""
        with pytest.raises(json.JSONDecodeError):
            settings_permissions_hash(b"")

    def test_non_utf8_bytes_raises(self):
        """Non-UTF-8 bytes raise — not silently swallowed."""
        with pytest.raises((json.JSONDecodeError, UnicodeDecodeError)):
            settings_permissions_hash(b"\x80{}")
