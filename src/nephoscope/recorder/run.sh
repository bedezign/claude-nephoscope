#!/bin/bash
# Observability recorder — pre/post tool-call hook entrypoint.
#
# Pipes the Claude Code hook payload (JSON on stdin) to run.py, which records
# a pending/completed row in ~/.cache/claude/observability/observations.db.
#
# The hook is registered in settings.json at PreToolUse and PostToolUse with
# the phase passed as $1 ("pre" or "post"). run.py handles both.

set -e

# Opt-out marker shared with the continuous-learning-v2 observer.
if [ -f "${HOME}/.claude/homunculus/disabled" ]; then
  exit 0
fi

exec /home/steve/.claude/observability/.venv/bin/python \
  /home/steve/.claude/observability/recorder/run.py "$@"
