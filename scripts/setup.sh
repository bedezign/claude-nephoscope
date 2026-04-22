#!/bin/bash
set -euo pipefail

# Setup script for Phase 8 observability sandbox.
# Idempotent: safe to re-run.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OBSERVABILITY_ROOT="$(dirname "$SCRIPT_DIR")"
OBSERVABILITY_DB="${OBSERVABILITY_DB:=/tmp/claude/observability-phase8/observations.db}"

echo "Setup: OBSERVABILITY_DB=$OBSERVABILITY_DB"
echo "Setup: OBSERVABILITY_ROOT=$OBSERVABILITY_ROOT"

# 1. Create/activate venv if not already present
VENV_DIR="$OBSERVABILITY_ROOT/.venv"
if [ ! -d "$VENV_DIR" ]; then
  echo "Creating venv at $VENV_DIR..."
  uv venv "$VENV_DIR"
else
  echo "Venv already exists at $VENV_DIR"
fi

# Activate venv
source "$VENV_DIR/bin/activate"

# 2. Install dependencies (idempotent with uv)
echo "Installing dependencies..."
uv pip install bashlex pyyaml

# 3. Apply schema to DB (idempotent: sqlite3 CREATE TABLE IF NOT EXISTS style is enforced at schema level)
echo "Applying schema to $OBSERVABILITY_DB..."
sqlite3 "$OBSERVABILITY_DB" < "$OBSERVABILITY_ROOT/lib/schema.sql"

# 4. Seed lookup tables (INSERT OR IGNORE is idempotent)
echo "Seeding lookup tables..."
sqlite3 "$OBSERVABILITY_DB" <<'EOF'
INSERT OR IGNORE INTO permission_modes (name) VALUES ('default'),('acceptEdits'),('bypassPermissions'),('plan'),('auto');
INSERT OR IGNORE INTO call_statuses    (name) VALUES ('pending'),('ok'),('err'),('denied'),('orphan');
INSERT OR IGNORE INTO global_mirror (id, settings_json_path, settings_json_sha256, settings_json_last_synced)
  VALUES (1, '~/.claude/settings.json', NULL, NULL);
EOF

echo "Setup complete. DB at $OBSERVABILITY_DB."
