"""Tests for permission seed and fixture management.

Tests cover:
- Loading fixtures from YAML
- Exporting permissions to YAML
- Round-trip idempotency
- Invalid fixture handling
- Fixture schema validation
"""

from __future__ import annotations

import json

import pytest
import yaml

from nephoscope.learners.permission.seed import apply_fixtures, export_permissions


class TestApplyFixtures:
    """Tests for apply_fixtures()."""

    def test_empty_fixture(self, tmp_db, tmp_path):
        """Loading an empty fixture list succeeds with no rows created."""
        fixture_file = tmp_path / "empty.yaml"
        fixture_file.write_text("[]")

        shapes_created, perms_created = apply_fixtures(tmp_db, fixture_file)
        assert shapes_created == 0
        assert perms_created == 0

    def test_simple_approved_fixture(self, tmp_db, tmp_path):
        """Loading a simple approved fixture creates rule_shape + permission."""
        fixture_file = tmp_path / "simple.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "Read",
                        "flags": [],
                        "decision": "approved",
                        "reason": "test",
                    }
                ]
            )
        )

        shapes_created, perms_created = apply_fixtures(tmp_db, fixture_file)
        assert perms_created == 1

        # Verify the rule_shape was created
        shape_row = tmp_db.execute(
            "SELECT id, verb, flags FROM rule_shapes WHERE verb = 'Read';"
        ).fetchone()
        assert shape_row is not None
        shape_id = shape_row[0]
        assert shape_row[1] == "Read"
        assert shape_row[2] == "[]"  # minified JSON

        # Verify the permission row was created (global tier)
        perm_row = tmp_db.execute(
            "SELECT decision, source, reason, session_id, project_id "
            "FROM permissions WHERE rule_shape_id = ?;",
            (shape_id,),
        ).fetchone()
        assert perm_row is not None
        assert perm_row[0] == "approved"
        assert perm_row[1] == "seed"
        assert perm_row[2] == "test"
        assert perm_row[3] is None  # session_id
        assert perm_row[4] is None  # project_id

    def test_fixture_with_subcommand(self, tmp_db, tmp_path):
        """Fixture with subcommand is stored correctly."""
        fixture_file = tmp_path / "sub.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "git",
                        "subcommand": "commit",
                        "flags": ["-m"],
                        "decision": "approved",
                    }
                ]
            )
        )

        apply_fixtures(tmp_db, fixture_file)

        shape_row = tmp_db.execute(
            "SELECT subcommand, flags FROM rule_shapes WHERE verb = 'git';"
        ).fetchone()
        assert shape_row is not None
        assert shape_row[0] == "commit"
        assert json.loads(shape_row[1]) == ["-m"]

    def test_fixture_with_path_spec(self, tmp_db, tmp_path):
        """Fixture with path_spec is stored correctly."""
        fixture_file = tmp_path / "path.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "Read",
                        "flags": [],
                        "path_spec": "$PROJECT_ROOT/**",
                        "decision": "approved",
                    }
                ]
            )
        )

        apply_fixtures(tmp_db, fixture_file)

        shape_row = tmp_db.execute(
            "SELECT path_spec FROM rule_shapes WHERE verb = 'Read';"
        ).fetchone()
        assert shape_row is not None
        assert shape_row[0] == "$PROJECT_ROOT/**"

    def test_fixture_with_flags_wildcard(self, tmp_db, tmp_path):
        """Fixture with flags='*' is stored as literal '*'."""
        fixture_file = tmp_path / "wildcard.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "Bash",
                        "flags": "*",
                        "decision": "approved",
                    }
                ]
            )
        )

        apply_fixtures(tmp_db, fixture_file)

        shape_row = tmp_db.execute(
            "SELECT flags FROM rule_shapes WHERE verb = 'Bash';"
        ).fetchone()
        assert shape_row is not None
        assert shape_row[0] == "*"

    def test_fixture_with_rejected_decision(self, tmp_db, tmp_path):
        """Fixture with decision='rejected' is applied correctly."""
        fixture_file = tmp_path / "rejected.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "rm",
                        "flags": ["-r", "-f"],
                        "decision": "rejected",
                        "reason": "dangerous",
                    }
                ]
            )
        )

        apply_fixtures(tmp_db, fixture_file)

        perm_row = tmp_db.execute(
            "SELECT decision, reason FROM permissions "
            "WHERE rule_shape_id IN "
            "(SELECT id FROM rule_shapes WHERE verb = 'rm');"
        ).fetchone()
        assert perm_row is not None
        assert perm_row[0] == "rejected"
        assert perm_row[1] == "dangerous"

    def test_fixture_global_tier_default(self, tmp_db, tmp_path):
        """Fixture without tier defaults to global."""
        fixture_file = tmp_path / "default_tier.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "Read",
                        "flags": [],
                        "decision": "approved",
                    }
                ]
            )
        )

        apply_fixtures(tmp_db, fixture_file)

        perm_row = tmp_db.execute(
            "SELECT session_id, project_id FROM permissions "
            "WHERE rule_shape_id IN "
            "(SELECT id FROM rule_shapes WHERE verb = 'Read');"
        ).fetchone()
        assert perm_row is not None
        assert perm_row[0] is None  # session_id
        assert perm_row[1] is None  # project_id

    def test_fixture_without_reason(self, tmp_db, tmp_path):
        """Fixture without reason stores NULL."""
        fixture_file = tmp_path / "no_reason.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "Read",
                        "flags": [],
                        "decision": "approved",
                    }
                ]
            )
        )

        apply_fixtures(tmp_db, fixture_file)

        perm_row = tmp_db.execute(
            "SELECT reason FROM permissions "
            "WHERE rule_shape_id IN "
            "(SELECT id FROM rule_shapes WHERE verb = 'Read');"
        ).fetchone()
        assert perm_row is not None
        assert perm_row[0] is None

    def test_fixture_multiple_entries(self, tmp_db, tmp_path):
        """Loading multiple fixtures creates all rows."""
        fixture_file = tmp_path / "multi.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {"verb": "Read", "flags": [], "decision": "approved"},
                    {"verb": "Write", "flags": [], "decision": "approved"},
                    {"verb": "Bash", "flags": ["-c"], "decision": "rejected"},
                ]
            )
        )

        shapes_created, perms_created = apply_fixtures(tmp_db, fixture_file)
        assert perms_created == 3

        # Verify all three rules exist
        rows = tmp_db.execute(
            "SELECT COUNT(*) FROM rule_shapes WHERE verb IN ('Read', 'Write', 'Bash');"
        ).fetchone()
        assert rows[0] == 3

    def test_fixture_missing_verb_fails(self, tmp_db, tmp_path):
        """Fixture without verb raises ValueError."""
        fixture_file = tmp_path / "bad_verb.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "flags": [],
                        "decision": "approved",
                    }
                ]
            )
        )

        with pytest.raises(ValueError, match="missing 'verb'"):
            apply_fixtures(tmp_db, fixture_file)

    def test_fixture_missing_decision_fails(self, tmp_db, tmp_path):
        """Fixture without decision raises ValueError."""
        fixture_file = tmp_path / "bad_decision.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "Read",
                        "flags": [],
                    }
                ]
            )
        )

        with pytest.raises(ValueError, match="missing 'decision'"):
            apply_fixtures(tmp_db, fixture_file)

    def test_fixture_missing_flags_fails(self, tmp_db, tmp_path):
        """Fixture without flags raises ValueError."""
        fixture_file = tmp_path / "bad_flags.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "Read",
                        "decision": "approved",
                    }
                ]
            )
        )

        with pytest.raises(ValueError, match="missing 'flags'"):
            apply_fixtures(tmp_db, fixture_file)

    def test_fixture_invalid_decision_fails(self, tmp_db, tmp_path):
        """Fixture with invalid decision raises ValueError."""
        fixture_file = tmp_path / "bad_decision_value.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "Read",
                        "flags": [],
                        "decision": "maybe",
                    }
                ]
            )
        )

        with pytest.raises(ValueError, match="invalid decision"):
            apply_fixtures(tmp_db, fixture_file)

    def test_fixture_non_dict_entry_fails(self, tmp_db, tmp_path):
        """Fixture with non-dict entry raises ValueError."""
        fixture_file = tmp_path / "bad_entry.yaml"
        fixture_file.write_text(yaml.dump(["not a dict"]))

        with pytest.raises(ValueError, match="is not a dict"):
            apply_fixtures(tmp_db, fixture_file)

    def test_fixture_non_list_fails(self, tmp_db, tmp_path):
        """Fixture that is not a list raises ValueError."""
        fixture_file = tmp_path / "not_list.yaml"
        fixture_file.write_text(yaml.dump({"verb": "Read"}))

        with pytest.raises(ValueError, match="must be a YAML list"):
            apply_fixtures(tmp_db, fixture_file)


class TestExportPermissions:
    """Tests for export_permissions()."""

    def test_empty_export(self, tmp_db):
        """Exporting an empty DB returns empty YAML list."""
        yaml_str = export_permissions(tmp_db)
        entries = yaml.safe_load(yaml_str)
        assert entries == []

    def test_export_simple_permission(self, tmp_db, tmp_path):
        """Exporting a single permission returns matching YAML."""
        # Set up a permission
        fixture_file = tmp_path / "export_test.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "Read",
                        "flags": [],
                        "decision": "approved",
                        "reason": "test export",
                    }
                ]
            )
        )
        apply_fixtures(tmp_db, fixture_file)

        # Export and parse
        yaml_str = export_permissions(tmp_db)
        entries = yaml.safe_load(yaml_str)

        assert len(entries) == 1
        assert entries[0]["verb"] == "Read"
        assert entries[0]["flags"] == []
        assert entries[0]["decision"] == "approved"
        assert entries[0]["reason"] == "test export"

    def test_export_with_subcommand(self, tmp_db, tmp_path):
        """Exporting a permission with subcommand includes it."""
        fixture_file = tmp_path / "export_sub.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "git",
                        "subcommand": "push",
                        "flags": ["-u"],
                        "decision": "approved",
                    }
                ]
            )
        )
        apply_fixtures(tmp_db, fixture_file)

        yaml_str = export_permissions(tmp_db)
        entries = yaml.safe_load(yaml_str)

        assert entries[0]["verb"] == "git"
        assert entries[0]["subcommand"] == "push"
        assert entries[0]["flags"] == ["-u"]

    def test_export_with_path_spec(self, tmp_db, tmp_path):
        """Exporting a permission with path_spec includes it."""
        fixture_file = tmp_path / "export_path.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "Read",
                        "flags": [],
                        "path_spec": "$HOME/**",
                        "decision": "approved",
                    }
                ]
            )
        )
        apply_fixtures(tmp_db, fixture_file)

        yaml_str = export_permissions(tmp_db)
        entries = yaml.safe_load(yaml_str)

        assert entries[0]["path_spec"] == "$HOME/**"

    def test_export_flags_wildcard(self, tmp_db, tmp_path):
        """Exporting a permission with flags='*' exports as string."""
        fixture_file = tmp_path / "export_wildcard.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "Bash",
                        "flags": "*",
                        "decision": "approved",
                    }
                ]
            )
        )
        apply_fixtures(tmp_db, fixture_file)

        yaml_str = export_permissions(tmp_db)
        entries = yaml.safe_load(yaml_str)

        assert entries[0]["flags"] == "*"

    def test_export_multiple_permissions(self, tmp_db, tmp_path):
        """Exporting multiple permissions returns all of them."""
        fixture_file = tmp_path / "export_multi.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {"verb": "Read", "flags": [], "decision": "approved"},
                    {"verb": "Write", "flags": [], "decision": "approved"},
                    {"verb": "rm", "flags": ["-r"], "decision": "rejected"},
                ]
            )
        )
        apply_fixtures(tmp_db, fixture_file)

        yaml_str = export_permissions(tmp_db)
        entries = yaml.safe_load(yaml_str)

        assert len(entries) == 3
        verbs = [e["verb"] for e in entries]
        assert "Read" in verbs
        assert "Write" in verbs
        assert "rm" in verbs

    def test_export_to_file(self, tmp_db, tmp_path):
        """Exporting to a file writes the YAML content."""
        fixture_file = tmp_path / "fixture.yaml"
        fixture_file.write_text(
            yaml.dump([{"verb": "Read", "flags": [], "decision": "approved"}])
        )
        apply_fixtures(tmp_db, fixture_file)

        output_file = tmp_path / "exported.yaml"
        export_permissions(tmp_db, output_file)

        assert output_file.exists()
        entries = yaml.safe_load(output_file.read_text())
        assert len(entries) == 1
        assert entries[0]["verb"] == "Read"

    def test_export_omits_global_tier(self, tmp_db, tmp_path):
        """Exporting a global-tier permission omits the tier field."""
        fixture_file = tmp_path / "export_global.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "Read",
                        "flags": [],
                        "tier": "global",
                        "decision": "approved",
                    }
                ]
            )
        )
        apply_fixtures(tmp_db, fixture_file)

        yaml_str = export_permissions(tmp_db)
        entries = yaml.safe_load(yaml_str)

        assert "tier" not in entries[0]

    def test_export_omits_null_reason(self, tmp_db, tmp_path):
        """Exporting a permission without reason omits the field."""
        fixture_file = tmp_path / "export_no_reason.yaml"
        fixture_file.write_text(
            yaml.dump([{"verb": "Read", "flags": [], "decision": "approved"}])
        )
        apply_fixtures(tmp_db, fixture_file)

        yaml_str = export_permissions(tmp_db)
        entries = yaml.safe_load(yaml_str)

        assert "reason" not in entries[0]

    def test_export_includes_reason(self, tmp_db, tmp_path):
        """Exporting a permission with reason includes the field."""
        fixture_file = tmp_path / "export_reason.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "Read",
                        "flags": [],
                        "decision": "approved",
                        "reason": "safe operation",
                    }
                ]
            )
        )
        apply_fixtures(tmp_db, fixture_file)

        yaml_str = export_permissions(tmp_db)
        entries = yaml.safe_load(yaml_str)

        assert entries[0]["reason"] == "safe operation"


class TestRoundTrip:
    """Tests for round-trip idempotency (load → export → load)."""

    def test_roundtrip_simple(self, tmp_db, tmp_path):
        """Loading and exporting returns equivalent YAML."""
        original_yaml = yaml.dump(
            [
                {"verb": "Read", "flags": [], "decision": "approved"},
                {
                    "verb": "git",
                    "subcommand": "push",
                    "flags": ["-u"],
                    "decision": "rejected",
                    "reason": "dangerous",
                },
            ]
        )
        fixture_file = tmp_path / "roundtrip.yaml"
        fixture_file.write_text(original_yaml)

        # Load
        apply_fixtures(tmp_db, fixture_file)

        # Export
        exported_yaml = export_permissions(tmp_db)
        original_entries = yaml.safe_load(original_yaml)
        exported_entries = yaml.safe_load(exported_yaml)

        # Entries should match (order may differ, but content is identical)
        assert len(original_entries) == len(exported_entries)

        # Sort both by (verb, subcommand, decision) for comparison
        original_entries.sort(
            key=lambda e: (e["verb"], e.get("subcommand", ""), e["decision"])
        )
        exported_entries.sort(
            key=lambda e: (e["verb"], e.get("subcommand", ""), e["decision"])
        )

        for orig, exp in zip(original_entries, exported_entries):
            assert orig["verb"] == exp["verb"]
            assert orig.get("subcommand") == exp.get("subcommand")
            assert orig["flags"] == exp["flags"]
            assert orig["decision"] == exp["decision"]
            assert orig.get("reason") == exp.get("reason")

    def test_roundtrip_multiple_times(self, tmp_db, tmp_path):
        """Repeated load/export cycles preserve data."""
        original_yaml = yaml.dump(
            [
                {"verb": "Read", "flags": [], "decision": "approved"},
                {"verb": "Write", "flags": ["-x"], "decision": "approved"},
            ]
        )

        fixture_file = tmp_path / "cycle.yaml"
        fixture_file.write_text(original_yaml)

        # First cycle
        apply_fixtures(tmp_db, fixture_file)
        exported_1 = export_permissions(tmp_db)

        # Clear the DB and do it again with the exported YAML
        tmp_db.execute("DELETE FROM permissions;")
        tmp_db.execute("DELETE FROM rule_shapes;")

        fixture_file.write_text(exported_1)
        apply_fixtures(tmp_db, fixture_file)
        exported_2 = export_permissions(tmp_db)

        # The two exports should be equivalent
        entries_1 = yaml.safe_load(exported_1)
        entries_2 = yaml.safe_load(exported_2)

        assert len(entries_1) == len(entries_2)
        for e1, e2 in zip(
            sorted(entries_1, key=lambda e: e["verb"]),
            sorted(entries_2, key=lambda e: e["verb"]),
        ):
            assert e1["verb"] == e2["verb"]
            assert e1["flags"] == e2["flags"]
            assert e1["decision"] == e2["decision"]


# ===========================================================================
# Phase 2 — context field in seed fixtures
# ===========================================================================


class TestContextField:
    """Seed YAML context field: validate, apply, export, round-trip."""

    def test_apply_fixture_with_context_toplevel(self, tmp_db, tmp_path):
        """Fixture with context='toplevel' stores the value in rule_shapes."""
        fixture_file = tmp_path / "ctx.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "op",
                        "subcommand": "read",
                        "flags": "*",
                        "context": "toplevel",
                        "decision": "rejected",
                        "reason": "op read standalone leaks secret",
                    }
                ]
            )
        )
        apply_fixtures(tmp_db, fixture_file)

        row = tmp_db.execute(
            "SELECT context FROM rule_shapes WHERE verb='op' AND subcommand='read';"
        ).fetchone()
        assert row is not None
        assert row[0] == "toplevel"

    def test_apply_fixture_without_context_defaults_to_any(self, tmp_db, tmp_path):
        """Fixture without context field defaults to 'any'."""
        fixture_file = tmp_path / "no_ctx.yaml"
        fixture_file.write_text(
            yaml.dump([{"verb": "git", "flags": "*", "decision": "approved"}])
        )
        apply_fixtures(tmp_db, fixture_file)

        row = tmp_db.execute(
            "SELECT context FROM rule_shapes WHERE verb='git';"
        ).fetchone()
        assert row is not None
        assert row[0] == "any"

    def test_apply_fixture_invalid_context_raises(self, tmp_db, tmp_path):
        """Fixture with invalid context value raises ValueError."""
        fixture_file = tmp_path / "bad_ctx.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "op",
                        "flags": "*",
                        "decision": "rejected",
                        "context": "outer",
                    }
                ]
            )
        )
        with pytest.raises(ValueError, match="invalid context"):
            apply_fixtures(tmp_db, fixture_file)

    def test_export_omits_context_when_any(self, tmp_db, tmp_path):
        """Export omits the context field when it equals 'any' (default)."""
        fixture_file = tmp_path / "any_ctx.yaml"
        fixture_file.write_text(
            yaml.dump([{"verb": "git", "flags": "*", "decision": "approved"}])
        )
        apply_fixtures(tmp_db, fixture_file)

        yaml_str = export_permissions(tmp_db)
        entries = yaml.safe_load(yaml_str)
        assert len(entries) == 1
        assert "context" not in entries[0], (
            f"context='any' should be omitted from export, got {entries[0]!r}"
        )

    def test_export_includes_context_when_not_any(self, tmp_db, tmp_path):
        """Export includes the context field when it is not 'any'."""
        fixture_file = tmp_path / "toplevel_ctx.yaml"
        fixture_file.write_text(
            yaml.dump(
                [
                    {
                        "verb": "op",
                        "subcommand": "read",
                        "flags": "*",
                        "context": "toplevel",
                        "decision": "rejected",
                        "reason": "standalone leaks secret",
                    }
                ]
            )
        )
        apply_fixtures(tmp_db, fixture_file)

        yaml_str = export_permissions(tmp_db)
        entries = yaml.safe_load(yaml_str)
        assert len(entries) == 1
        assert entries[0].get("context") == "toplevel", (
            f"Expected context='toplevel' in export, got {entries[0]!r}"
        )

    def test_roundtrip_with_context_toplevel(self, tmp_db, tmp_path):
        """Round-trip: load with context='toplevel' → export → reload → same state."""
        original = [
            {
                "verb": "op",
                "subcommand": "read",
                "flags": "*",
                "context": "toplevel",
                "decision": "rejected",
                "reason": "standalone leaks secret",
            }
        ]
        fixture_file = tmp_path / "roundtrip_ctx.yaml"
        fixture_file.write_text(yaml.dump(original))

        # Load → export
        apply_fixtures(tmp_db, fixture_file)
        exported_yaml = export_permissions(tmp_db)

        # Clear and reload from exported YAML
        tmp_db.execute("DELETE FROM permissions;")
        tmp_db.execute("DELETE FROM rule_shapes;")
        fixture_file.write_text(exported_yaml)
        apply_fixtures(tmp_db, fixture_file)

        # Verify final state
        row = tmp_db.execute(
            "SELECT context, verb, subcommand FROM rule_shapes WHERE verb='op';"
        ).fetchone()
        assert row is not None
        assert row[0] == "toplevel"
        assert row[1] == "op"
        assert row[2] == "read"

        # And exported context must appear in the re-loaded export too
        re_exported = yaml.safe_load(export_permissions(tmp_db))
        assert re_exported[0].get("context") == "toplevel"
