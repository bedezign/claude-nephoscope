#!/bin/bash
# Observability-owned observer agent launcher.
#
# Replaces the CL-v2 launcher at
# ``~/.claude/skills/continuous-learning-v2/agents/start-observer.sh``.
# Same behaviour (5-min loop, summarize → analyze via Haiku → commit cursor
# on success) but sources rows from the observability DB through our
# ``learners.instinct.summarize`` module.
#
# The homunculus instinct tree at ``~/.claude/homunculus/`` is unchanged —
# this launcher only feeds data in; the Haiku observer agent still writes
# instincts to the same directory it always has.
#
# Usage:
#   start-observer.sh            # start in background
#   start-observer.sh foreground # run in foreground (for debugging)
#   start-observer.sh stop       # stop running observer
#   start-observer.sh status     # check status
#
# State lives under ``~/.cache/claude/observability/`` (log + PID file),
# distinct from CL-v2's ``~/.cache/claude/observations/``, so the two can
# coexist during a migration (not that we need to — the pipeline wiring
# is a clean swap).

set -e

OBS_ROOT="${HOME}/.cache/claude/observability"
ANALYSIS_DIR="${HOME}/.cache/claude/analysis"
HOMUNCULUS_DIR="${HOME}/.claude/homunculus"
VENV_PY="${HOME}/.claude/observability/.venv/bin/python"
SUMMARIZE_MODULE="learners.instinct.summarize"
OBSERVABILITY_ROOT="${HOME}/.claude/observability"
PID_FILE="${OBS_ROOT}/.observer.pid"
LOG_FILE="${OBS_ROOT}/observer.log"

mkdir -p "$OBS_ROOT" "$ANALYSIS_DIR"

observer_loop() {
  set +e

  trap 'echo "[$(date)] Stopping observer" >> "$LOG_FILE"; rm -f "$PID_FILE"; exit 0' TERM INT

  analyze_observations() {
    local summary_file="${ANALYSIS_DIR}/summary-$(date +%Y%m%d-%H%M%S)-$$.txt"
    local meta
    meta=$(cd "$OBSERVABILITY_ROOT" && "$VENV_PY" -m "$SUMMARIZE_MODULE" \
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
      printf 'Read the observation summary at %s. It aggregates recent tool-call activity (tool frequency, repeated sequences, subagent usage, recent errors, per-project breakdown). If the summary shows 3+ occurrences of the same pattern (same tool sequence, same subagent, same recurring error), create an instinct file in %s/instincts/personal/ following the observer agent spec. Be conservative — only create instincts for clear patterns. You may use %s/ for intermediate working files (notes, scripts). Only final instinct .md files go in %s/instincts/personal/.' \
        "$summary_file" "$HOMUNCULUS_DIR" "$ANALYSIS_DIR" "$HOMUNCULUS_DIR" \
        | setsid claude --model haiku --max-turns 6 --print \
            --add-dir "$OBS_ROOT" \
            --add-dir "$ANALYSIS_DIR" \
            --add-dir "$HOMUNCULUS_DIR" \
            >> "$LOG_FILE" 2>&1 \
        && analysis_ok=1
    fi

    if [ "$analysis_ok" = "1" ]; then
      (cd "$OBSERVABILITY_ROOT" && "$VENV_PY" -m "$SUMMARIZE_MODULE" \
        commit --max-id "$max_id" 2>>"$LOG_FILE") \
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
        db="${HOME}/.cache/claude/observability/observations.db"
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
