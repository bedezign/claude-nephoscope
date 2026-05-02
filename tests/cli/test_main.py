"""Tests for nephoscope.cli.main — top-level dispatcher.

Verifies that each routed subcommand invokes the right underlying function and
that unknown subcommands cause argparse to exit with code 2.

All dispatch functions are monkeypatched so no real DB is required.
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Stats dispatch
# ---------------------------------------------------------------------------


def test_stats_dispatches_to_cmd_stats(monkeypatch: Any) -> None:
    """'nephoscope stats' must call _cmd_stats with a Namespace containing the
    parsed flags, and return whatever _cmd_stats returns."""
    import nephoscope.cli.main as main_mod

    sentinel = object()
    captured: list[argparse.Namespace] = []

    def fake_stats(args: argparse.Namespace) -> object:
        captured.append(args)
        return sentinel

    monkeypatch.setattr(main_mod, "_cmd_stats", fake_stats)

    result = main_mod.main(["stats"])

    assert result is sentinel, "main() must return the value from _cmd_stats"
    assert len(captured) == 1, "_cmd_stats must be called exactly once"
    assert isinstance(captured[0], argparse.Namespace), (
        "_cmd_stats must receive a Namespace"
    )


def test_stats_show_unused_flag_forwarded(monkeypatch: Any) -> None:
    """'nephoscope stats --show-unused' must set show_unused=True on the Namespace."""
    import nephoscope.cli.main as main_mod

    captured: list[argparse.Namespace] = []

    def fake_stats(args: argparse.Namespace) -> int:
        captured.append(args)
        return 0

    monkeypatch.setattr(main_mod, "_cmd_stats", fake_stats)

    main_mod.main(["stats", "--show-unused"])

    assert captured[0].show_unused is True


# ---------------------------------------------------------------------------
# Reconcile dispatch
# ---------------------------------------------------------------------------


def test_reconcile_dispatches_to_dispatch_reconcile(monkeypatch: Any) -> None:
    """'nephoscope reconcile' must call _dispatch_reconcile and propagate its
    return value back to the caller."""
    import nephoscope.cli.main as main_mod

    sentinel = object()
    captured: list[argparse.Namespace] = []

    def fake_reconcile(args: argparse.Namespace) -> object:
        captured.append(args)
        return sentinel

    monkeypatch.setattr(main_mod, "_dispatch_reconcile", fake_reconcile)

    result = main_mod.main(["reconcile", "--db", "/tmp/fake.db"])

    assert result is sentinel, "main() must propagate _dispatch_reconcile return value"
    assert len(captured) == 1, "_dispatch_reconcile must be called exactly once"


def test_reconcile_mode_flag_forwarded(monkeypatch: Any) -> None:
    """'nephoscope reconcile --mode plan' must set mode='plan' on the Namespace."""
    import nephoscope.cli.main as main_mod

    captured: list[argparse.Namespace] = []

    def fake_reconcile(args: argparse.Namespace) -> int:
        captured.append(args)
        return 0

    monkeypatch.setattr(main_mod, "_dispatch_reconcile", fake_reconcile)

    main_mod.main(["reconcile", "--db", "/tmp/fake.db", "--mode", "plan"])

    assert captured[0].mode == "plan"


# ---------------------------------------------------------------------------
# Unknown subcommand exits with code 2
# ---------------------------------------------------------------------------


def test_unknown_subcommand_exits_code_2() -> None:
    """An unrecognised subcommand must cause argparse to raise SystemExit(2)."""
    import nephoscope.cli.main as main_mod

    with pytest.raises(SystemExit) as excinfo:
        main_mod.main(["no-such-subcommand"])

    assert excinfo.value.code == 2, (
        f"expected exit code 2 for unknown subcommand, got {excinfo.value.code}"
    )


def test_no_subcommand_exits_code_2() -> None:
    """Invoking 'nephoscope' with no arguments must also exit with code 2."""
    import nephoscope.cli.main as main_mod

    with pytest.raises(SystemExit) as excinfo:
        main_mod.main([])

    assert excinfo.value.code == 2, (
        f"expected exit code 2 when no subcommand given, got {excinfo.value.code}"
    )


# ---------------------------------------------------------------------------
# Mirror-status dispatch
# ---------------------------------------------------------------------------


def test_mirror_status_dispatches(monkeypatch: Any) -> None:
    """'nephoscope mirror-status --db X' must call mirror_status_cmd(X)."""
    import nephoscope.cli.main as main_mod

    calls: list[str] = []

    def fake_mirror_status(db_path: str) -> int:
        calls.append(db_path)
        return 0

    monkeypatch.setattr(main_mod, "mirror_status_cmd", fake_mirror_status)
    monkeypatch.setattr(main_mod, "_require_db", lambda _cmd, _args: None)

    main_mod.main(["mirror-status", "--db", "/tmp/test.db"])

    assert calls == ["/tmp/test.db"]


# ---------------------------------------------------------------------------
# Init dispatch
# ---------------------------------------------------------------------------


def test_init_dispatches_to_init_main(monkeypatch: Any) -> None:
    """'nephoscope init' must delegate to init_cmd.main with no extra argv."""
    import nephoscope.cli.main as main_mod

    calls: list[list[str]] = []

    def fake_init_main(argv: list[str] | None = None) -> int:
        calls.append(argv if argv is not None else [])
        return 0

    monkeypatch.setattr(main_mod.init_cmd, "main", fake_init_main)

    result = main_mod.main(["init"])

    assert result == 0
    assert calls == [[]], "init_cmd.main must be called with an empty argv list"


def test_init_no_workspace_prompts_forwarded(monkeypatch: Any) -> None:
    """'nephoscope init --no-workspace-prompts' must pass the flag to init_cmd.main."""
    import nephoscope.cli.main as main_mod

    calls: list[list[str]] = []

    def fake_init_main(argv: list[str] | None = None) -> int:
        calls.append(argv if argv is not None else [])
        return 0

    monkeypatch.setattr(main_mod.init_cmd, "main", fake_init_main)

    main_mod.main(["init", "--no-workspace-prompts"])

    assert "--no-workspace-prompts" in calls[0]


# ---------------------------------------------------------------------------
# Migrate dispatch
# ---------------------------------------------------------------------------


def test_migrate_dispatches_to_migrate_main(monkeypatch: Any) -> None:
    """'nephoscope migrate' must delegate to migrate_cmd.main."""
    import nephoscope.cli.main as main_mod

    calls: list[list[str]] = []

    def fake_migrate_main(argv: list[str] | None = None) -> int:
        calls.append(argv if argv is not None else [])
        return 0

    monkeypatch.setattr(main_mod.migrate_cmd, "main", fake_migrate_main)

    main_mod.main(["migrate"])

    assert calls == [[]]
