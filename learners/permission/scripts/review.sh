#!/bin/bash
# Interactive permission-candidate review.
#
# Runs a fresh scan (to surface any new candidates), then for each eligible
# promotion prompts y/n/q. `y` promotes into permission_active, `n` rejects
# (removes the candidate row so the next review isn't cluttered with it).
# `q` quits early. Final summary reports promoted/rejected/skipped counts.
#
# Pipe-delimited lines out of `learner propose` are:
#   verb|subcommand|flags_json|obs|sessions
# Empty second field = NULL subcommand. The separator is `|` (not tab) because
# bash collapses consecutive whitespace IFS, which would lose the empty field.

set -e

PY=/home/steve/.claude/observability/.venv/bin/python
cd /home/steve/.claude/observability

# 1. Refresh candidates from the log so we see everything currently eligible.
"$PY" -m learners.permission.learner scan >/dev/null

# 2. Pull the eligible-for-promotion list as TSV.
proposals_file=$(mktemp)
trap 'rm -f "$proposals_file"' EXIT
"$PY" -m learners.permission.learner propose > "$proposals_file"

if [ ! -s "$proposals_file" ]; then
  echo "no promotion candidates meet thresholds"
  exit 0
fi

promoted=0
rejected=0
skipped=0

# Iterate over proposals. Reading from a separate FD keeps stdin free for
# the interactive prompt (the TTY). Bash 4+ guarantees this FD form.
while IFS='|' read -r verb sub flags obs sessions <&3; do
  sub_display="${sub:--}"
  printf 'Promote? [y/n/q] %s %s flags=%s (obs=%s, sessions=%s) ' \
    "$verb" "$sub_display" "$flags" "$obs" "$sessions"

  # Read answers from the caller's stdin (FD 0). Prefer the TTY when one is
  # attached for interactive use; fall back to piped input (e.g. from a test
  # harness: `printf 'n\nn\n' | review.sh`). If neither reads, treat as quit.
  if [ -t 0 ] && [ -r /dev/tty ]; then
    read -r answer </dev/tty || answer=q
  else
    read -r answer || answer=q
  fi

  case "$answer" in
    y|Y)
      if [ -n "$sub" ]; then
        "$PY" -m learners.permission.learner promote \
          --verb "$verb" --subcommand "$sub" --flags "$flags"
      else
        "$PY" -m learners.permission.learner promote \
          --verb "$verb" --flags "$flags"
      fi
      promoted=$((promoted + 1))
      ;;
    n|N)
      if [ -n "$sub" ]; then
        "$PY" -m learners.permission.learner reject \
          --verb "$verb" --subcommand "$sub" --flags "$flags"
      else
        "$PY" -m learners.permission.learner reject \
          --verb "$verb" --flags "$flags"
      fi
      rejected=$((rejected + 1))
      ;;
    q|Q)
      echo "quitting"
      break
      ;;
    *)
      echo "  skipped"
      skipped=$((skipped + 1))
      ;;
  esac
done 3< "$proposals_file"

echo
echo "summary: promoted ${promoted}, rejected ${rejected}, skipped ${skipped}"
