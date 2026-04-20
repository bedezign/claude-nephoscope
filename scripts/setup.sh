#!/bin/bash
# Idempotent bootstrap for ~/.claude/observability/.
#
# Creates the shared venv (bashlex + pyyaml), then applies any pending
# schema migrations against ~/.cache/claude/observability/observations.db
# (honoring OBSERVABILITY_DB if set). Safe to re-run; each step is a no-op
# when already satisfied.

set -euo pipefail

OBS_ROOT="/home/steve/.claude/observability"
VENV_DIR="${OBS_ROOT}/.venv"
PY="${VENV_DIR}/bin/python"

# --- 1. venv -----------------------------------------------------------------

if [[ ! -x "${PY}" ]]; then
  echo "[setup] creating venv at ${VENV_DIR}"
  uv venv --no-config "${VENV_DIR}" >/dev/null
else
  echo "[setup] venv already present at ${VENV_DIR}"
fi

# --- 2. deps -----------------------------------------------------------------

echo "[setup] ensuring bashlex + pyyaml + pytest installed"
VIRTUAL_ENV="${VENV_DIR}" uv pip install --no-config --quiet bashlex pyyaml pytest

# --- 3. migrations -----------------------------------------------------------

echo "[setup] applying pending schema migrations"
"${PY}" - <<'PY'
import sys
sys.path.insert(0, "/home/steve/.claude/observability")

from lib.db import DB_PATH, _migrate, _open

conn = _open()
try:
    _migrate(conn)
    version = conn.execute("PRAGMA user_version;").fetchone()[0]
    rows = conn.execute("SELECT COUNT(*) FROM tool_calls;").fetchone()[0]
finally:
    conn.close()

print(f"[setup] db={DB_PATH}")
print(f"[setup] schema_version={version}")
print(f"[setup] tool_calls_rows={rows}")
PY

echo "[setup] done"
