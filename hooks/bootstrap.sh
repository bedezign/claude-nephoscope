#!/usr/bin/env bash
set -euo pipefail
VENV="${CLAUDE_PLUGIN_DATA}/.venv"
CACHED="${CLAUDE_PLUGIN_DATA}/pyproject.toml.cached"
MANIFEST="${CLAUDE_PLUGIN_ROOT}/pyproject.toml"
if ! diff -q "$MANIFEST" "$CACHED" >/dev/null 2>&1 || [ ! -x "$VENV/bin/nephoscope-recorder" ]; then
  mkdir -p "$CLAUDE_PLUGIN_DATA"
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || { echo "nephoscope: Python 3.11+ required (found $(python3 --version))" >&2; exit 1; }
  if [ -d "$VENV" ] && [ ! -x "$VENV/bin/nephoscope-recorder" ]; then
    rm -rf "$VENV"
  fi
  [ -d "$VENV" ] || python3 -m venv "$VENV"
  "$VENV/bin/pip" install "$CLAUDE_PLUGIN_ROOT"
  cp "$MANIFEST" "$CACHED"
fi
