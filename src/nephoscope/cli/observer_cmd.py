"""Background observer daemon for instinct summarization.

Replaces ``src/nephoscope/learners/instinct/scripts/start-observer.sh``.

Subcommands:
  start       Start the observer as a background daemon (double-fork).
  stop        Signal the daemon to stop (SIGTERM) and remove the PID file.
  status      Report whether the daemon is running.
  foreground  Run the loop in the current process (useful for debugging).

Environment variables:
  CLAUDE_PLUGIN_DATA      Plugin data directory; used for default PID/log paths.
  OBSERVABILITY_DB        Observations database path.
  NEPHOSCOPE_INSTINCT_DIR Target directory for instinct .md files.
  NEPHOSCOPE_STATE_DIR    Override for the state directory.
  NEPHOSCOPE_ANALYSIS_DIR Override for the analysis scratch directory.

The ``--pid-file`` and ``--log-file`` CLI flags override the env-derived paths
so tests can inject tmp_path-scoped locations without touching real state dirs.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Path resolution (lazy, env-based)
# ---------------------------------------------------------------------------


def _default_state_dir() -> Path:
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA", "")
    if plugin_data:
        return Path(plugin_data)
    return Path.home() / ".cache" / "nephoscope"


def _default_instinct_dir() -> Path:
    env = os.environ.get("NEPHOSCOPE_INSTINCT_DIR", "")
    if env:
        return Path(env)
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA", "")
    if plugin_data:
        return Path(plugin_data) / "instincts"
    return Path.home() / ".claude" / "instincts"


def _state_dir() -> Path:
    env = os.environ.get("NEPHOSCOPE_STATE_DIR", "")
    return Path(env) if env else _default_state_dir()


def _analysis_dir() -> Path:
    env = os.environ.get("NEPHOSCOPE_ANALYSIS_DIR", "")
    return Path(env) if env else _state_dir() / "analysis"


def _default_pid_file() -> Path:
    return _state_dir() / ".observer.pid"


def _default_log_file() -> Path:
    return _state_dir() / "observer.log"


def _summarize_module() -> str:
    return "nephoscope.learners.instinct.summarize"


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------


def _read_pid(pid_file: Path) -> int | None:
    """Return the PID from ``pid_file``, or None if absent or invalid."""
    try:
        text = pid_file.read_text().strip()
        return int(text)
    except (FileNotFoundError, ValueError, OSError):
        return None


def _pid_alive(pid: int) -> bool:
    """Return True if ``pid`` refers to a running process."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Daemonization (double-fork)
# ---------------------------------------------------------------------------


def _do_daemonize(pid_file: Path, log_file: Path) -> None:
    """Fork twice, detach from the controlling terminal, redirect stdio.

    The grandchild (daemon) writes its PID to ``pid_file`` and starts the
    observer loop.  The parent and intermediate child both exit immediately.

    This function never returns in the parent process — it either forks or
    raises an exception.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    # First fork.
    pid = os.fork()
    if pid > 0:
        # Parent: wait briefly so the grandchild can write the PID file, then
        # report success.
        time.sleep(1)
        return  # parent continues into start() to read and print the PID

    # Intermediate child: create new session, fork again.
    os.setsid()

    pid2 = os.fork()
    if pid2 > 0:
        # Intermediate child exits.
        os._exit(0)

    # Grandchild (daemon): redirect stdio, write PID, run loop.
    log_fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    devnull_fd = os.open("/dev/null", os.O_RDONLY)
    os.dup2(devnull_fd, 0)  # stdin → /dev/null
    os.dup2(log_fd, 1)  # stdout → log
    os.dup2(log_fd, 2)  # stderr → log
    os.close(devnull_fd)
    os.close(log_fd)

    pid_file.write_text(f"{os.getpid()}\n")
    _write_log(log_file, f"Observer started (PID: {os.getpid()})")

    try:
        _observer_loop(pid_file, log_file)
    except Exception as exc:  # noqa: BLE001
        _write_log(log_file, f"Observer crashed: {exc}")
    finally:
        pid_file.unlink(missing_ok=True)

    os._exit(0)


# ---------------------------------------------------------------------------
# Observer loop
# ---------------------------------------------------------------------------


def _write_log(log_file: Path, msg: str) -> None:
    import datetime as _dt

    ts = (
        _dt.datetime.now(tz=_dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    try:
        with log_file.open("a") as fh:
            fh.write(f"[{ts}] {msg}\n")
    except OSError:
        pass  # heartbeat loss is acceptable; daemon cannot emit observability signal here


def _run_summarizer(log_file: Path, summary_file: Path) -> tuple[int, int] | None:
    """Run the summarize subcommand and return (rows, max_id) or None on skip/error."""
    import json as _json
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            _summarize_module(),
            "write",
            "--output",
            str(summary_file),
            "--min-rows",
            "10",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode == 2:
        return None  # nothing new, no log

    if result.returncode != 0:
        _write_log(log_file, f"summarize failed (rc={result.returncode})")
        return None

    try:
        meta = _json.loads(result.stdout)
        return int(meta["rows"]), int(meta["max_id"])
    except (KeyError, ValueError, _json.JSONDecodeError):
        _write_log(log_file, f"summarize metadata parse failed: {result.stdout!r}")
        return None


def _run_claude_analysis(
    log_file: Path,
    summary_file: Path,
    state_dir: Path,
    analysis_dir: Path,
    instinct_dir: Path,
) -> bool:
    """Invoke claude to analyse the summary. Returns True on success."""
    import shutil
    import subprocess

    if not shutil.which("claude"):
        return False

    prompt = (
        f"Read the observation summary at {summary_file}. It aggregates recent "
        f"tool-call activity (tool frequency, repeated sequences, subagent usage, "
        f"recent errors, per-project breakdown). If the summary shows 3+ occurrences "
        f"of the same pattern (same tool sequence, same subagent, same recurring error), "
        f"create an instinct file in {instinct_dir}/personal/ following the observer "
        f"agent spec. Be conservative — only create instincts for clear patterns. "
        f"You may use {analysis_dir}/ for intermediate working files (notes, scripts). "
        f"Only final instinct .md files go in {instinct_dir}/personal/."
    )
    claude_result = subprocess.run(
        [
            "claude",
            "--model",
            "haiku",
            "--max-turns",
            "6",
            "--print",
            "--add-dir",
            str(state_dir),
            "--add-dir",
            str(analysis_dir),
            "--add-dir",
            str(instinct_dir),
        ],
        input=prompt,
        capture_output=True,
        text=True,
    )
    if claude_result.returncode != 0:
        _write_log(log_file, "claude invocation failed")
        return False
    return True


def _advance_cursor(log_file: Path, max_id: int) -> None:
    """Advance the summarizer cursor to max_id via the commit subcommand."""
    import subprocess

    adv = subprocess.run(
        [
            sys.executable,
            "-m",
            _summarize_module(),
            "commit",
            "--max-id",
            str(max_id),
        ],
        capture_output=True,
        text=True,
    )
    if adv.returncode == 0:
        _write_log(log_file, f"Cursor advanced to {max_id}")
    else:
        _write_log(log_file, f"commit cursor failed (rc={adv.returncode})")


def _cleanup_analysis_dir(log_file: Path, analysis_dir: Path) -> None:
    """Remove analysis files older than one hour; log any OSErrors."""
    cutoff = time.time() - 3600
    failures = 0
    for f in analysis_dir.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        except OSError:
            failures += 1
    if failures:
        _write_log(log_file, f"analysis-dir cleanup: {failures} OSError(s)")


def _analyze_once(log_file: Path) -> None:
    """Run one summarize+claude cycle.

    On summarize rc=2 (nothing new): silent no-op.
    On summarize failure: log and return (cursor not advanced).
    On claude unavailable: log and return.
    On success: advance cursor.
    """
    import datetime as _dt

    state_dir = _state_dir()
    analysis_dir = _analysis_dir()
    instinct_dir = _default_instinct_dir()

    analysis_dir.mkdir(parents=True, exist_ok=True)
    instinct_dir.mkdir(parents=True, exist_ok=True)

    ts_stamp = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    summary_file = analysis_dir / f"summary-{ts_stamp}-{os.getpid()}.txt"

    result = _run_summarizer(log_file, summary_file)
    if result is None:
        return  # nothing new or error already logged

    rows, max_id = result
    _write_log(log_file, f"Analyzing {rows} new tool calls (cursor → {max_id})...")

    analysis_ok = _run_claude_analysis(
        log_file, summary_file, state_dir, analysis_dir, instinct_dir
    )

    if analysis_ok:
        _advance_cursor(log_file, max_id)
    else:
        _write_log(log_file, "Analysis failed; cursor held, will retry next cycle.")

    _cleanup_analysis_dir(log_file, analysis_dir)


def _observer_loop(pid_file: Path, log_file: Path) -> None:
    """Main daemon loop: sleep 5 min, analyze, repeat.

    Installs SIGTERM and SIGINT handlers for clean shutdown.
    Installs SIGUSR1 handler for on-demand analysis.
    """
    _stop_requested = False

    def _handle_stop(signum, frame):  # noqa: ANN001
        nonlocal _stop_requested
        _stop_requested = True
        _write_log(log_file, "Stopping observer")
        pid_file.unlink(missing_ok=True)
        sys.exit(0)

    def _handle_usr1(signum, frame):  # noqa: ANN001
        _analyze_once(log_file)

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGUSR1, _handle_usr1)

    while not _stop_requested:
        time.sleep(300)
        if not _stop_requested:
            _analyze_once(log_file)


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _cmd_start(pid_file: Path, log_file: Path) -> int:
    pid = _read_pid(pid_file)
    if pid is not None:
        if _pid_alive(pid):
            print(f"Observer already running (PID: {pid})")
            return 0
        pid_file.unlink(missing_ok=True)

    print("Starting observer agent...")
    _do_daemonize(pid_file, log_file)

    # We're back in the parent after fork; check if the PID file was written.
    pid = _read_pid(pid_file)
    if pid is not None:
        print(f"Observer started (PID: {pid})")
        print(f"Log: {log_file}")
        return 0
    print("Failed to start observer", file=sys.stderr)
    return 1


def _cmd_stop(pid_file: Path) -> int:
    pid = _read_pid(pid_file)
    if pid is None:
        print("Observer not running.")
        return 0

    if not _pid_alive(pid):
        print("Observer not running (stale PID file).")
        pid_file.unlink(missing_ok=True)
        return 0

    print(f"Stopping observer (PID: {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as exc:
        print(f"Failed to stop observer: {exc}", file=sys.stderr)
        return 1
    pid_file.unlink(missing_ok=True)
    print("Observer stopped.")
    return 0


def _cmd_status(pid_file: Path, log_file: Path) -> int:
    pid = _read_pid(pid_file)
    if pid is None:
        print("Observer not running")
        return 1

    if not _pid_alive(pid):
        print("Observer not running (stale PID file)")
        pid_file.unlink(missing_ok=True)
        return 1

    print(f"Observer is running (PID: {pid})")
    print(f"Log: {log_file}")

    # Report DB stats if available (best-effort, same as bash version).
    try:
        import sqlite3 as _sql

        from nephoscope.lib.paths import observations_db_path

        db_path = observations_db_path()
        if db_path.exists():
            conn = _sql.connect(str(db_path))
            try:
                rows = conn.execute("SELECT COUNT(*) FROM tool_calls;").fetchone()[0]
                cursor_row = conn.execute(
                    "SELECT last_processed_id FROM consumer_cursors"
                    " WHERE consumer='instinct-summarizer';"
                ).fetchone()
                cursor = cursor_row[0] if cursor_row else 0
                print(f"tool_calls: {rows} (summarizer cursor: {cursor})")
            finally:
                conn.close()
    except Exception:  # noqa: BLE001
        pass  # heartbeat: status printed above; DB stats are best-effort

    return 0


def _cmd_foreground(pid_file: Path, log_file: Path) -> int:
    print("Running observer in foreground (Ctrl+C to stop)...")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        _observer_loop(pid_file, log_file)
    except KeyboardInterrupt:
        pass
    return 0


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nephoscope-observer",
        description=(
            "Background observer daemon for instinct summarization.\n"
            "\n"
            "Runs a 5-minute loop: summarize new tool_calls rows, invoke\n"
            "``claude --model haiku`` with a pointer to the summary, commit\n"
            "the cursor on success."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--pid-file",
        default=None,
        dest="pid_file",
        help=(
            "Path to the PID file. Defaults to\n"
            "${CLAUDE_PLUGIN_DATA}/.observer.pid (or ~/.cache/nephoscope/.observer.pid)."
        ),
    )
    parser.add_argument(
        "--log-file",
        default=None,
        dest="log_file",
        help=(
            "Path to the log file. Defaults to\n"
            "${CLAUDE_PLUGIN_DATA}/observer.log (or ~/.cache/nephoscope/observer.log)."
        ),
    )

    sub = parser.add_subparsers(dest="subcommand")
    sub.add_parser("start", help="Start the observer daemon in the background.")
    sub.add_parser("stop", help="Stop the running observer daemon.")
    sub.add_parser("status", help="Report whether the observer daemon is running.")
    sub.add_parser(
        "foreground", help="Run the observer in the foreground (for debugging)."
    )

    args = parser.parse_args(argv)

    pid_file = Path(args.pid_file) if args.pid_file else _default_pid_file()
    log_file = Path(args.log_file) if args.log_file else _default_log_file()

    subcommand = args.subcommand or "start"

    match subcommand:
        case "start":
            return _cmd_start(pid_file, log_file)
        case "stop":
            return _cmd_stop(pid_file)
        case "status":
            return _cmd_status(pid_file, log_file)
        case "foreground":
            return _cmd_foreground(pid_file, log_file)
        case _:
            print(f"Unknown subcommand: {subcommand}", file=sys.stderr)
            return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
