---
description: Manage permission rules and candidates for the observability module
argument-hint: "[status|review|scan|propose|list|promote|reject|unpermit|seed|prune|gc|sweep] [options]"
allowed-tools: Bash(/home/steve/.claude/observability/**), Bash(grep:*), Bash(cut:*), Bash(awk:*), Bash(sort:*), Bash(uniq:*), Bash(head:*), Bash(tail:*), Bash(sqlite3:*), Bash(python:*), Bash(pwd:*), Bash(cd:*), Bash(echo:*), Read
---

## Context

- Observability root: !`test -d /home/steve/.claude/observability && echo "/home/steve/.claude/observability" || echo "ERROR: sandbox not found"`
- DB path: !`echo "${OBSERVABILITY_DB:-/home/steve/.cache/claude/observability/observations.db}"`
- Venv: !`test -f /home/steve/.claude/observability/.venv/bin/python && echo "ready" || echo "not set up"`

## Preflight

Before executing any subcommand:

1. Verify the observability root exists and contains `learners/permission/learner.py`
2. Ensure `$OBSERVABILITY_DB` is set (default `/home/steve/.cache/claude/observability/observations.db`)
3. If venv check fails, suggest running `bash /home/steve/.claude/observability/scripts/setup.sh`

If any preflight fails, STOP and report the error.

## Subcommands

Parse `$ARGUMENTS` to extract the subcommand and options:

### `status` (default if no subcommand)

Display current permission state: per-tier rule counts, candidate count, recent asks.

```bash
OBSERVABILITY_DB=/home/steve/.cache/claude/observability/observations.db \
  sqlite3 /home/steve/.cache/claude/observability/observations.db <<EOF
.headers on
.mode column
SELECT 'Approved global' as tier, COUNT(*) as count
  FROM permissions p JOIN rule_shapes rs ON rs.id = p.rule_shape_id
  WHERE p.session_id IS NULL AND p.project_id IS NULL AND p.decision = 'approved'
UNION ALL
SELECT 'Rejected global', COUNT(*) FROM permissions p JOIN rule_shapes rs ON rs.id = p.rule_shape_id
  WHERE p.session_id IS NULL AND p.project_id IS NULL AND p.decision = 'rejected'
UNION ALL
SELECT 'Approved (project scope)', COUNT(*) FROM permissions p JOIN rule_shapes rs ON rs.id = p.rule_shape_id
  WHERE p.project_id IS NOT NULL AND p.decision = 'approved'
UNION ALL
SELECT 'Rejected (project scope)', COUNT(*) FROM permissions p JOIN rule_shapes rs ON rs.id = p.rule_shape_id
  WHERE p.project_id IS NOT NULL AND p.decision = 'rejected'
UNION ALL
SELECT 'Approved (session scope)', COUNT(*) FROM permissions p JOIN rule_shapes rs ON rs.id = p.rule_shape_id
  WHERE p.session_id IS NOT NULL AND p.decision = 'approved'
UNION ALL
SELECT 'Rejected (session scope)', COUNT(*) FROM permissions p JOIN rule_shapes rs ON rs.id = p.rule_shape_id
  WHERE p.session_id IS NOT NULL AND p.decision = 'rejected'
UNION ALL
SELECT 'Candidates pending', COUNT(*) FROM permission_candidates
UNION ALL
SELECT 'Asks pending', COUNT(*) FROM permission_ask_pending;
EOF
```

Then show recent asks:

```bash
OBSERVABILITY_DB=/home/steve/.cache/claude/observability/observations.db \
  sqlite3 /home/steve/.cache/claude/observability/observations.db <<EOF
.headers on
.mode column
SELECT asked_at, verb, subcommand, flags FROM permission_ask_pending
  ORDER BY asked_at DESC LIMIT 10;
EOF
```

### `scan`

Scan new Bash tool calls and accumulate into `permission_candidates`.

Routes to: `learners.permission.learner scan`

```bash
cd /home/steve/.claude/observability && \
  OBSERVABILITY_DB=/home/steve/.cache/claude/observability/observations.db \
  .venv/bin/python -m learners.permission.learner scan
```

### `propose`

Emit eligible candidates (thresholds met, not on deny list) for review.

Routes to: `learners.permission.learner propose`

```bash
cd /home/steve/.claude/observability && \
  OBSERVABILITY_DB=/home/steve/.cache/claude/observability/observations.db \
  .venv/bin/python -m learners.permission.learner propose
```

### `review`

Interactively walk through candidates with per-axis (verb/paths/flags) and tier prompts.

Routes to: `learners/permission/scripts/review.sh`

```bash
cd /home/steve/.claude/observability && \
  OBSERVABILITY_DB=/home/steve/.cache/claude/observability/observations.db \
  bash learners/permission/scripts/review.sh
```

### `list`

List permission rules with optional filtering.

**Usage:**
```
/permissions list [approved|rejected|candidates] [--tier global|project|session]
```

**Examples:**
- `/permissions list` — all rules
- `/permissions list approved --tier global` — approved global-tier rules
- `/permissions list rejected --tier project` — rejected project-tier rules
- `/permissions list candidates` — all candidates (not rules)

Implemented via SQLite queries:

```bash
# Determine filter from $ARGUMENTS
# If filter is "candidates", query v_candidates
# Otherwise, query v_permissions with WHERE clause for decision + tier
OBSERVABILITY_DB=/home/steve/.cache/claude/observability/observations.db \
  sqlite3 /home/steve/.cache/claude/observability/observations.db ".headers on" ".mode column" \
  "SELECT * FROM v_permissions [WHERE decision='...' AND tier='...'] [ORDER BY decided_at DESC];"
```

### `promote`

Promote a candidate to an approved rule at the specified tier.

Routes to: `learners.permission.learner promote`

**Usage:**
```
/permissions promote --verb <verb> [--subcommand <sub>] --flags <flags> --tier global|project|session [--path-spec <spec>] [--reason <text>]
```

Delegates to learner:

```bash
cd /home/steve/.claude/observability && \
  OBSERVABILITY_DB=/home/steve/.cache/claude/observability/observations.db \
  .venv/bin/python -m learners.permission.learner promote --sync \
    --verb "$VERB" [--subcommand "$SUB"] --flags "$FLAGS" --tier "$TIER" \
    [--path-spec "$PATHSPEC"] [--reason "$REASON"]
```

After the DB op, print one-line sync status: `sync: ok` or `sync: skipped (session-tier)`.

On `MirrorHashMismatch` (exit 1): echo
`"Settings file modified externally. Run '/permissions reconcile' and retry."`

If flags promoted to wildcard (`*`), offer to subsume sibling concrete rules:

```bash
cd /home/steve/.claude/observability && \
  OBSERVABILITY_DB=/home/steve/.cache/claude/observability/observations.db \
  .venv/bin/python -m learners.permission.learner count-concrete-siblings \
    --verb "$VERB" --subcommand "$SUB" --tier "$TIER"
```

If count > 0, prompt: "Found N concrete sibling rules for `verb/subcommand` at `$TIER`. Delete them? [Y/n]"

If yes, run:

```bash
cd /home/steve/.claude/observability && \
  OBSERVABILITY_DB=/home/steve/.cache/claude/observability/observations.db \
  .venv/bin/python -m learners.permission.learner subsume-siblings \
    --verb "$VERB" --subcommand "$SUB" --tier "$TIER"
```

### `reject`

Reject a candidate (add a rejected rule).

Routes to: `learners.permission.learner reject`

**Usage:**
```
/permissions reject --verb <verb> [--subcommand <sub>] --flags <flags> --tier global|project|session [--reason <text>]
```

```bash
cd /home/steve/.claude/observability && \
  OBSERVABILITY_DB=/home/steve/.cache/claude/observability/observations.db \
  .venv/bin/python -m learners.permission.learner reject \
    --verb "$VERB" [--subcommand "$SUB"] --flags "$FLAGS" --tier "$TIER" \
    [--reason "$REASON"]
```

After the DB op, print one-line sync status: `sync: ok` or `sync: skipped (session-tier)`.

### `unpermit`

Delete a permission rule.

Routes to: `learners.permission.learner unpermit`

**Usage:**
```
/permissions unpermit --verb <verb> [--subcommand <sub>] --flags <flags> --tier global|project|session
```

```bash
cd /home/steve/.claude/observability && \
  OBSERVABILITY_DB=/home/steve/.cache/claude/observability/observations.db \
  .venv/bin/python -m learners.permission.learner unpermit \
    --verb "$VERB" [--subcommand "$SUB"] --flags "$FLAGS" --tier "$TIER"
```

After the DB op, print one-line sync status: `sync: ok` or `sync: skipped (session-tier)`.

### `seed`

Seed fixture rules (load from YAML or export to YAML).

Routes to: `learners/permission/seed.py`

**Usage:**
```
/permissions seed [--export]
```

- `/permissions seed` — load `config/fixtures/safe_shapes.yaml` into the DB
- `/permissions seed --export` — dump current `permissions` to stdout in YAML format

```bash
cd /home/steve/.claude/observability && \
  OBSERVABILITY_DB=/home/steve/.cache/claude/observability/observations.db \
  .venv/bin/python -m learners.permission.seed [--export]
```

After load, print one-line sync status: `sync: ok (N rules loaded)`.

### `prune`

Delete stale candidates (older than threshold, no corresponding ask_pending row).

Routes to: `lib/prune.py`

**Usage:**
```
/permissions prune [--stale-days N]
```

Default: 30 days.

```bash
cd /home/steve/.claude/observability && \
  OBSERVABILITY_DB=/home/steve/.cache/claude/observability/observations.db \
  .venv/bin/python -m lib.prune [--stale-days 30]
```

### `gc`

Garbage collect: drop session-tier rules from idle sessions, drop stale ask_pending rows.

Routes to: `lib.gc_sessions` (via learner CLI or direct module invocation)

**Usage:**
```
/permissions gc [--session-idle-days N] [--ask-pending-hours H]
```

Defaults: 7 days (sessions), 1 hour (asks).

```bash
cd /home/steve/.claude/observability && \
  OBSERVABILITY_DB=/home/steve/.cache/claude/observability/observations.db \
  .venv/bin/python -m lib.gc_sessions [--session-idle-days 7] [--ask-pending-hours 1]
```

### `sweep`

Run both `prune` and `gc` in sequence.

```bash
/permissions prune && /permissions gc
```

### `reconcile [--project <path>]`

Diff the JSON mirror against the DB and (optionally) apply a resolution.

**Usage:**
```
/permissions reconcile [--project <settings-json-path>]
```

- Omit `--project` to reconcile the global mirror.
- Default mode: `interactive` (auto-switches to `adopt` when stored hash is NULL).

```bash
cd /home/steve/.claude/observability && \
  OBSERVABILITY_DB=/home/steve/.cache/claude/observability/observations.db \
  .venv/bin/python -m commands.permissions_cmd reconcile \
    [--project "$SETTINGS_PATH"] [--mode interactive|plan|auto-db-wins|auto-json-wins|adopt]
```

### `mirror-status`

Print a table showing the global mirror and each registered project.

Columns: scope, path, last_synced, hash_status (`stamped` / `null` / `mismatch`).

```bash
cd /home/steve/.claude/observability && \
  OBSERVABILITY_DB=/home/steve/.cache/claude/observability/observations.db \
  .venv/bin/python -m commands.permissions_cmd mirror-status
```

### `mirror-dry-run [--project <path>]`

Build the mirror JSON from DB (same path `sync_*` uses) and write to stdout — no disk writes.

**Usage:**
```
/permissions mirror-dry-run [--project <settings-json-path>]
```

```bash
cd /home/steve/.claude/observability && \
  OBSERVABILITY_DB=/home/steve/.cache/claude/observability/observations.db \
  .venv/bin/python -m commands.permissions_cmd mirror-dry-run \
    [--project "$SETTINGS_PATH"]
```

### `reload-hint`

Touch `~/.claude/settings.json` mtime via `Path.touch()` to force Claude Code's settings re-read.

**Sandbox rule:** only touches the path supplied via `--settings-path`. Never touches the real
`~/.claude/settings.json` in tests.

```bash
cd /home/steve/.claude/observability && \
  .venv/bin/python -m commands.permissions_cmd reload-hint \
    --settings-path "$SETTINGS_PATH"
```

## Implementation Notes

1. **Routing**: Parse `$ARGUMENTS` to extract subcommand and option flags. Route each subcommand to its handler (learner CLI, review.sh, prune.py, gc_sessions.py, or direct SQLite query).

2. **Error handling**: If any Bash invocation fails, report the stderr and exit non-zero. Do not swallow errors.

3. **Context capture**: The `--home`, `--cwd`, `--project-root` flags needed by pattern-variants are available via Bash environment and current directory.

4. **Narrow tool allowlist**: Only Bash (scoped to observability paths) and Read. No Agent spawning, no external API calls, no network access.

5. **Output format**: Learner commands produce pipe-delimited or JSON output; review.sh and GC scripts print counts/results to stdout; SQLite queries use `.headers` and `.mode column` for readability.

## Example workflows

**View current state:**
```
/permissions status
```

**Review and promote a candidate:**
```
/permissions propose      # see candidates
/permissions review       # walk through prompts
```

**Clean up old data:**
```
/permissions prune --stale-days 30
/permissions gc --session-idle-days 7
```

**Export and seed fixture:**
```
/permissions seed --export > my_rules.yaml
/permissions seed            # reload from config/fixtures/safe_shapes.yaml
```
