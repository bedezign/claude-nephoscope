#!/bin/bash
# Manual development setup — create a venv, install the package in editable
# mode, and materialise the observations database.
#
# In a plugin install the same work is done by hooks/bootstrap.sh on first
# SessionStart; this script only exists for local development outside the
# plugin host (running tests, exercising the CLI by hand).
#
# Re-running is safe: uv venv + uv pip install are idempotent, and the
# ``nephoscope-init`` call is a no-op against an existing DB.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

VENV_DIR="${VENV_DIR:-${REPO_ROOT}/.venv}"
if [ ! -d "$VENV_DIR" ]; then
  echo "Creating venv at $VENV_DIR..."
  uv venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

echo "Installing nephoscope (editable) + dev extras..."
uv pip install -e "${REPO_ROOT}[dev]"

echo "Bootstrapping observations DB..."
nephoscope-init

echo "Setup complete."
