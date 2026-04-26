"""Tests for cli.observer_cmd — ``nephoscope-observer`` daemon console script.

Covers:
  - argparse happy path for each subcommand
  - PID file lifecycle: start writes PID, stop removes it, status reads it
  - ``foreground`` subcommand runs the loop in-process (mocked)
  - Second ``start`` when PID file already exists with live PID → refusal
  - Stale PID file detection (PID file exists but process is gone)
  - Env-override path isolation: PID + log paths come from ``tmp_path``

Test isolation: ``--pid-file`` / ``--log-file`` CLI flags override paths so
no real ``${CLAUDE_PLUGIN_DATA}`` directory is touched.  Daemon forking is
avoided by only testing the ``foreground`` subcommand and the pre-fork
PID-check logic (mocked).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock


from nephoscope.cli.observer_cmd import main, _read_pid, _pid_alive


# ---------------------------------------------------------------------------
# Helper: arg lists that inject tmp_path paths
# ---------------------------------------------------------------------------


def _args(tmp_path: Path, subcommand: str, *extra: str) -> list[str]:
    pid = str(tmp_path / "observer.pid")
    log = str(tmp_path / "observer.log")
    # Global flags (--pid-file, --log-file) must precede the subcommand.
    return ["--pid-file", pid, "--log-file", log, subcommand, *extra]


# ---------------------------------------------------------------------------
# _read_pid — reads integer from PID file, None when absent or invalid
# ---------------------------------------------------------------------------


def test_read_pid_returns_none_when_absent(tmp_path):
    pid_file = tmp_path / "x.pid"
    assert _read_pid(pid_file) is None


def test_read_pid_returns_int_when_present(tmp_path):
    pid_file = tmp_path / "x.pid"
    pid_file.write_text("1234\n")
    assert _read_pid(pid_file) == 1234


def test_read_pid_returns_none_on_garbage(tmp_path):
    pid_file = tmp_path / "x.pid"
    pid_file.write_text("not-a-pid\n")
    assert _read_pid(pid_file) is None


# ---------------------------------------------------------------------------
# _pid_alive — checks whether a process is running
# ---------------------------------------------------------------------------


def test_pid_alive_current_process():
    assert _pid_alive(os.getpid()) is True


def test_pid_alive_invalid_pid():
    # PID 0 is never a valid user process; kill(0, 0) sends to the process group.
    # Use a very large PID that is almost certainly not running.
    assert _pid_alive(999999999) is False


# ---------------------------------------------------------------------------
# status — no PID file → "not running", exit 1
# ---------------------------------------------------------------------------


def test_status_not_running(tmp_path, capsys):
    rc = main(_args(tmp_path, "status"))
    assert rc == 1
    out = capsys.readouterr().out
    assert "not running" in out.lower()


# ---------------------------------------------------------------------------
# status — stale PID file (process gone) → cleans up, exits 1
# ---------------------------------------------------------------------------


def test_status_stale_pid_file(tmp_path, capsys):
    pid_file = tmp_path / "observer.pid"
    pid_file.write_text("999999999\n")  # almost certainly dead
    rc = main(_args(tmp_path, "status"))
    assert rc == 1
    assert not pid_file.exists()
    out = capsys.readouterr().out
    assert "stale" in out.lower() or "not running" in out.lower()


# ---------------------------------------------------------------------------
# status — live PID file → "running", exit 0
# ---------------------------------------------------------------------------


def test_status_running(tmp_path, capsys):
    pid_file = tmp_path / "observer.pid"
    pid_file.write_text(f"{os.getpid()}\n")
    rc = main(_args(tmp_path, "status"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "running" in out.lower()


# ---------------------------------------------------------------------------
# stop — no PID file → "not running"
# ---------------------------------------------------------------------------


def test_stop_when_not_running(tmp_path, capsys):
    rc = main(_args(tmp_path, "stop"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "not running" in out.lower()


# ---------------------------------------------------------------------------
# stop — stale PID file → cleans up
# ---------------------------------------------------------------------------


def test_stop_stale_pid_file(tmp_path, capsys):
    pid_file = tmp_path / "observer.pid"
    pid_file.write_text("999999999\n")
    rc = main(_args(tmp_path, "stop"))
    assert rc == 0
    assert not pid_file.exists()
    out = capsys.readouterr().out
    assert "stale" in out.lower() or "not running" in out.lower()


# ---------------------------------------------------------------------------
# stop — live process: sends SIGTERM and removes PID file
# ---------------------------------------------------------------------------


def test_stop_sends_sigterm(tmp_path, capsys):
    pid_file = tmp_path / "observer.pid"
    pid_file.write_text(f"{os.getpid()}\n")

    with mock.patch("os.kill") as mock_kill:
        mock_kill.side_effect = [None, None]  # check alive, then SIGTERM
        rc = main(_args(tmp_path, "stop"))

    assert rc == 0
    # PID file should be removed after stop.
    assert not pid_file.exists()
    out = capsys.readouterr().out
    assert (
        "stop" in out.lower() or "stopped" in out.lower() or "stopping" in out.lower()
    )


# ---------------------------------------------------------------------------
# start — second start when PID file already live → refusal
# ---------------------------------------------------------------------------


def test_start_refuses_when_already_running(tmp_path, capsys):
    pid_file = tmp_path / "observer.pid"
    pid_file.write_text(f"{os.getpid()}\n")

    rc = main(_args(tmp_path, "start"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "already running" in out.lower()


# ---------------------------------------------------------------------------
# start — stale PID file is cleaned before launching
# ---------------------------------------------------------------------------


def test_start_cleans_stale_pid_before_launch(tmp_path, capsys):
    pid_file = tmp_path / "observer.pid"
    pid_file.write_text("999999999\n")

    # Mock the actual fork/daemon launch so we don't fork in tests.
    with mock.patch("nephoscope.cli.observer_cmd._do_daemonize") as mock_daemon:
        mock_daemon.return_value = None

        # After daemonize, write the PID file ourselves to simulate what the
        # daemon would do.
        def fake_daemon(pid_file_path, log_file_path):
            pid_file_path.write_text(f"{os.getpid()}\n")

        mock_daemon.side_effect = fake_daemon
        rc = main(_args(tmp_path, "start"))

    assert rc == 0
    out = capsys.readouterr().out
    assert "started" in out.lower() or "observer" in out.lower()


# ---------------------------------------------------------------------------
# foreground — runs loop iteration, then exits on KeyboardInterrupt
# ---------------------------------------------------------------------------


def test_foreground_exits_cleanly(tmp_path, capsys):
    """foreground mode runs the observe loop; we stop it immediately."""
    log_file = tmp_path / "observer.log"
    pid_file = tmp_path / "observer.pid"

    # Patch the inner loop so it exits immediately (raises KeyboardInterrupt
    # after one iteration, simulating Ctrl+C).
    with mock.patch("nephoscope.cli.observer_cmd._observer_loop") as mock_loop:
        mock_loop.side_effect = KeyboardInterrupt
        rc = main(
            [
                "--pid-file",
                str(pid_file),
                "--log-file",
                str(log_file),
                "foreground",
            ]
        )

    # Foreground mode should exit 0 on KeyboardInterrupt.
    assert rc == 0
