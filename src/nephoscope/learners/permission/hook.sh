#!/bin/bash
# Permission learner — PreToolUse runtime gate.
#
# Reads the Claude Code PreToolUse payload on stdin and emits either {} (fall
# through to normal prompt) or a permissionDecision object (deny or allow).
# See hook.py for the full protocol.

set -e

# Opt-out marker shared with the continuous-learning-v2 observer and the
# observability recorder — flipping it disables all observability side
# effects including this gate.
if [ -f "${HOME}/.claude/homunculus/disabled" ]; then
  exit 0
fi

exec /home/steve/.claude/observability/.venv/bin/python \
  /home/steve/.claude/observability/learners/permission/hook.py
