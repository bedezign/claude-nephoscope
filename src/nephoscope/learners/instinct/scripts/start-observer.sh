#!/bin/bash
# Background observer launcher.
#
# 5-minute loop: summarize new tool_calls rows, invoke `claude --model haiku`
# with a pointer to the summary, commit the cursor on success. Intended as a
# manual daemon — not a Claude Code hook — so it resolves its own paths from
# env vars with plugin-data-aware fallbacks.
#
# Usage:
#   start-observer.sh            # start in background
#   start-observer.sh foreground # run in foreground (for debugging)
#   start-observer.sh stop       # stop running observer
#   start-observer.sh status     # check status
#
# Environment:
#   OBSERVABILITY_DB            observations database path (optional)
#   CLAUDE_PLUGIN_DATA          plugin data directory — used to locate the
#                               plugin-scoped venv and default the instinct dir
#   NEPHOSCOPE_INSTINCT_DIR     target directory for instinct .md files
#                               (defaults to ${CLAUDE_PLUGIN_DATA}/instincts
#                               or ~/.claude/instincts)
#   NEPHOSCOPE_VENV             path to the Python interpreter to use
#                               (defaults to ${CLAUDE_PLUGIN_DATA}/.venv/bin/python
#                               or the current `python3`)

set -e

# --- path resolution --------------------------------------------------------

if [ -n "${CLAUDE_PLUGIN_DATA:-}" ]; then
  DEFAULT_STATE_DIR="${CLAUDE_PLUGIN_DATA}"
  DEFAULT_INSTINCT_DIR="${CLAUDE_PLUGIN_DATA}/instincts"
  DEFAULT_VENV_PY="${CLAUDE_PLUGIN_DATA}/.venv/bin/python"
else
  DEFAULT_STATE_DIR="${HOME}/.cache/nephoscope"
  DEFAULT_INSTINCT_DIR="${HOME}/.claude/instincts"
  DEFAULT_VENV_PY="$(command -v python3 || echo python)"
fi

STATE_DIR="${NEPHOSCOPE_STATE_DIR:-${DEFAULT_STATE_DIR}}"
ANALYSIS_DIR="${NEPHOSCOPE_ANALYSIS_DIR:-${STATE_DIR}/analysis}"
INSTINCT_DIR="${NEPHOSCOPE_INSTINCT_DIR:-${DEFAULT_INSTINCT_DIR}}"
VENV_PY="${NEPHOSCOPE_VENV:-${DEFAULT_VENV_PY}}"
SUMMARIZE_MODULE="nephoscope.learners.instinct.summarize"
PID_FILE="${STATE_DIR}/.observer.pid"
LOG_FILE="${STATE_DIR}/observer.log"

mkdir -p "$STATE_DIR" "$ANALYSIS_DIR"

observer_loop() {
  set +e

  trap 'echo "[$(date)] Stopping observer" >> "$LOG_FILE"; rm -f "$PID_FILE"; exit 0' TERM INT

  analyze_observations() {
    local summary_file="${ANALYSIS_DIR}/summary-$(date +%Y%m%d-%H%M%S)-$$.txt"
    local meta
    meta=$("$VENV_PY" -m "$SUMMARIZE_MODULE" \
      write --output "$summary_file" --min-rows 10 2>>"$LOG_FILE")
    local rc=$?

    if [ "$rc" -eq 2 ]; then
      return  # nothing new
    fi
    if [ "$rc" -ne 0 ]; then
      echo "[$(date)] summarize failed (rc=$rc)" >> "$LOG_FILE"
      return
    fi

    local max_id
    max_id=$("$VENV_PY" -c 'import json,sys; print(json.loads(sys.argv[1])["max_id"])' \
      "$meta" 2>/dev/null || echo "")
    local rows
    rows=$("$VENV_PY" -c 'import json,sys; print(json.loads(sys.argv[1])["rows"])' \
      "$meta" 2>/dev/null || echo "0")

    if [ -z "$max_id" ]; then
      echo "[$(date)] summarize metadata parse failed: $meta" >> "$LOG_FILE"
      return
    fi

    echo "[$(date)] Analyzing $rows new tool calls (cursor → $max_id)..." >> "$LOG_FILE"

    local analysis_ok=0
    if command -v claude &> /dev/null; then
      # Prompt via stdin; setsid isolates claude's process group so its
      # exit-time signals don't reach us.
      printf 'Read the observation summary at %s. It aggregates recent tool-call activity (tool frequency, repeated sequences, subagent usage, recent errors, per-project breakdown). If the summary shows 3+ occurrences of the same pattern (same tool sequence, same subagent, same recurring error), create an instinct file in %s/personal/ following the observer agent spec. Be conservative — only create instincts for clear patterns. You may use %s/ for intermediate working files (notes, scripts). Only final instinct .md files go in %s/personal/.' \
        "$summary_file" "$INSTINCT_DIR" "$ANALYSIS_DIR" "$INSTINCT_DIR" \
        | setsid claude --model haiku --max-turns 6 --print \
            --add-dir "$STATE_DIR" \
            --add-dir "$ANALYSIS_DIR" \
            --add-dir "$INSTINCT_DIR" \
            >> "$LOG_FILE" 2>&1 \
        && analysis_ok=1
    fi

    if [ "$analysis_ok" = "1" ]; then
      "$VENV_PY" -m "$SUMMARIZE_MODULE" \
        commit --max-id "$max_id" 2>>"$LOG_FILE" \
        && echo "[$(date)] Cursor advanced to $max_id" >> "$LOG_FILE"
    else
      echo "[$(date)] Analysis failed; cursor held, will retry next cycle." >> "$LOG_FILE"
    fi

    # Clean up stale analysis files (keeps ANALYSIS_DIR small).
    find "$ANALYSIS_DIR" -type f -mmin +60 -delete 2>/dev/null || true
  }

  # Handle SIGUSR1 for on-demand analysis.
  trap 'analyze_observations' USR1

  echo "$$" > "$PID_FILE"
  echo "[$(date)] Observer started (PID: $$)" >> "$LOG_FILE"

  while true; do
    sleep 300 || true
    analyze_observations
  done
}

case "${1:-start}" in
  stop)
    if [ -f "$PID_FILE" ]; then
      pid=$(cat "$PID_FILE")
      if kill -0 "$pid" 2>/dev/null; then
        echo "Stopping observer (PID: $pid)..."
        kill "$pid"
        rm -f "$PID_FILE"
        echo "Observer stopped."
      else
        echo "Observer not running (stale PID file)."
        rm -f "$PID_FILE"
      fi
    else
      echo "Observer not running."
    fi
    exit 0
    ;;

  status)
    if [ -f "$PID_FILE" ]; then
      pid=$(cat "$PID_FILE")
      if kill -0 "$pid" 2>/dev/null; then
        echo "Observer is running (PID: $pid)"
        echo "Log: $LOG_FILE"
        db="${OBSERVABILITY_DB:-${STATE_DIR}/observations.db}"
        if [ -f "$db" ]; then
          rows=$(sqlite3 "$db" "SELECT COUNT(*) FROM tool_calls;" 2>/dev/null || echo "?")
          cursor=$(sqlite3 "$db" "SELECT last_processed_id FROM consumer_cursors WHERE consumer='instinct-summarizer';" 2>/dev/null || echo "?")
          echo "tool_calls: $rows (summarizer cursor: ${cursor:-0})"
        fi
        exit 0
      else
        echo "Observer not running (stale PID file)"
        rm -f "$PID_FILE"
        exit 1
      fi
    else
      echo "Observer not running"
      exit 1
    fi
    ;;

  foreground)
    echo "Running observer in foreground (Ctrl+C to stop)..."
    observer_loop
    ;;

  start)
    if [ -f "$PID_FILE" ]; then
      pid=$(cat "$PID_FILE")
      if kill -0 "$pid" 2>/dev/null; then
        echo "Observer already running (PID: $pid)"
        exit 0
      fi
      rm -f "$PID_FILE"
    fi

    echo "Starting observer agent..."
    setsid nohup "$0" foreground >> "$LOG_FILE" 2>&1 < /dev/null &
    disown
    sleep 1

    if [ -f "$PID_FILE" ]; then
      echo "Observer started (PID: $(cat "$PID_FILE"))"
      echo "Log: $LOG_FILE"
    else
      echo "Failed to start observer"
      exit 1
    fi
    ;;

  *)
    echo "Usage: $0 {start|stop|status|foreground}"
    exit 1
    ;;
esac
