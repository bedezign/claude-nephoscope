---
description: Manage permission rules and candidates learned by nephoscope
argument-hint: "[status|review|scan|propose|list|promote|reject|unpermit|seed|prune|gc|sweep|reconcile|mirror-status|mirror-dry-run|reload-hint] [options]"
allowed-tools: Bash(${CLAUDE_PLUGIN_DATA}/.venv/bin/nephoscope-*:*), Bash(${CLAUDE_PLUGIN_DATA}/.venv/bin/python:*), Bash(grep:*), Bash(cut:*), Bash(awk:*), Bash(sort:*), Bash(uniq:*), Bash(head:*), Bash(tail:*), Bash(sqlite3:*), Bash(pwd:*), Bash(cd:*), Bash(echo:*), Read
---

## Invocation

Always invoked as **`/nephoscope:permissions`**. The unqualified `/permissions`
is Claude Code's built-in allow/deny UI; our command lives under the plugin
namespace so the two don't collide.

## Context

- Plugin venv: !`test -f "${CLAUDE_PLUGIN_DATA}/.venv/bin/nephoscope-learn" && echo "ready" || echo "not bootstrapped — start a new session and the SessionStart hook will install it"`
- DB path: !`echo "${OBSERVABILITY_DB:-${CLAUDE_PLUGIN_DATA}/observations.db}"`

## Preflight

Before executing any subcommand:

1. Verify `${CLAUDE_PLUGIN_DATA}/.venv/bin/nephoscope-learn` exists. If not, exit with a note that the SessionStart hook needs to run once (start a new session).
2. Resolve the DB path as `${OBSERVABILITY_DB:-${CLAUDE_PLUGIN_DATA}/observations.db}` and export it for all subprocess invocations.

If any preflight fails, STOP and report the error.

## Subcommands

Parse `$ARGUMENTS` to extract the subcommand and options:

### `status` (default if no subcommand)

Display current permission state: per-tier rule counts, candidate count, recent asks.

```bash
DB="${OBSERVABILITY_DB:-${CLAUDE_PLUGIN_DATA}/observations.db}"
sqlite3 "$DB" <<EOF
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
DB="${OBSERVABILITY_DB:-${CLAUDE_PLUGIN_DATA}/observations.db}"
sqlite3 "$DB" <<EOF
.headers on
.mode column
SELECT asked_at, verb, subcommand, flags FROM permission_ask_pending
  ORDER BY asked_at DESC LIMIT 10;
EOF
```

### `scan`

Scan new Bash tool calls and accumulate into `permission_candidates`.

```bash
"${CLAUDE_PLUGIN_DATA}/.venv/bin/nephoscope-learn" scan
```

### `propose`

Emit eligible candidates (thresholds met, not on deny list) for review.

```bash
"${CLAUDE_PLUGIN_DATA}/.venv/bin/nephoscope-learn" propose
```

### `review`

Interactively walk through candidates with per-axis (verb/paths/flags) and tier prompts.

The `review.sh` launcher lives inside the installed package; invoke it via its absolute path under `${CLAUDE_PLUGIN_ROOT}`:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/src/nephoscope/learners/permission/scripts/review.sh"
```

### `list`

List permission rules with optional filtering.

**Usage:**
```
/nephoscope:permissions list [approved|rejected|candidates] [--tier global|project|session]
```

**Examples:**
- `/nephoscope:permissions list` — all rules
- `/nephoscope:permissions list approved --tier global` — approved global-tier rules
- `/nephoscope:permissions list rejected --tier project` — rejected project-tier rules
- `/nephoscope:permissions list candidates` — all candidates (not rules)

Implemented via SQLite queries:

```bash
DB="${OBSERVABILITY_DB:-${CLAUDE_PLUGIN_DATA}/observations.db}"
# If filter is "candidates", query v_candidates; otherwise query v_permissions
# with a WHERE clause for decision + tier.
sqlite3 "$DB" ".headers on" ".mode column" \
  "SELECT * FROM v_permissions [WHERE decision='...' AND tier='...'] [ORDER BY decided_at DESC];"
```

### `promote`

Promote a candidate to an approved rule at the specified tier.

**Usage:**
```
/nephoscope:permissions promote --verb <verb> [--subcommand <sub>] --flags <flags> --tier global|project|session [--path-spec <spec>] [--reason <text>]
```

```bash
"${CLAUDE_PLUGIN_DATA}/.venv/bin/nephoscope-learn" promote --sync \
  --verb "$VERB" [--subcommand "$SUB"] --flags "$FLAGS" --tier "$TIER" \
  [--path-spec "$PATHSPEC"] [--reason "$REASON"]
```

After the DB op, print one-line sync status: `sync: ok` or `sync: skipped (session-tier)`.

On `MirrorHashMismatch` (exit 1): echo
`"Settings file modified externally. Run '/nephoscope:permissions reconcile' and retry."`

If flags promoted to wildcard (`*`), offer to subsume sibling concrete rules:

```bash
"${CLAUDE_PLUGIN_DATA}/.venv/bin/nephoscope-learn" count-concrete-siblings \
  --verb "$VERB" --subcommand "$SUB" --tier "$TIER"
```

If count > 0, prompt: "Found N concrete sibling rules for `verb/subcommand` at `$TIER`. Delete them? [Y/n]"

If yes, run:

```bash
"${CLAUDE_PLUGIN_DATA}/.venv/bin/nephoscope-learn" subsume-siblings \
  --verb "$VERB" --subcommand "$SUB" --tier "$TIER"
```

### `reject`

Reject a candidate (add a rejected rule).

**Usage:**
```
/nephoscope:permissions reject --verb <verb> [--subcommand <sub>] --flags <flags> --tier global|project|session [--reason <text>]
```

```bash
"${CLAUDE_PLUGIN_DATA}/.venv/bin/nephoscope-learn" reject \
  --verb "$VERB" [--subcommand "$SUB"] --flags "$FLAGS" --tier "$TIER" \
  [--reason "$REASON"]
```

After the DB op, print one-line sync status: `sync: ok` or `sync: skipped (session-tier)`.

### `unpermit`

Delete a permission rule.

**Usage:**
```
/nephoscope:permissions unpermit --verb <verb> [--subcommand <sub>] --flags <flags> --tier global|project|session
```

```bash
"${CLAUDE_PLUGIN_DATA}/.venv/bin/nephoscope-learn" unpermit \
  --verb "$VERB" [--subcommand "$SUB"] --flags "$FLAGS" --tier "$TIER"
```

After the DB op, print one-line sync status: `sync: ok` or `sync: skipped (session-tier)`.

### `seed`

Seed fixture rules (load from YAML or export to YAML).

**Usage:**
```
/nephoscope:permissions seed [--export]
```

- `/nephoscope:permissions seed` — load `config/fixtures/safe_shapes.yaml` into the DB
- `/nephoscope:permissions seed --export` — dump current `permissions` to stdout in YAML format

```bash
"${CLAUDE_PLUGIN_DATA}/.venv/bin/python" -m nephoscope.learners.permission.seed [--export]
```

After load, print one-line sync status: `sync: ok (N rules loaded)`.

### `prune`

Delete stale candidates (older than threshold, no corresponding ask_pending row).

**Usage:**
```
/nephoscope:permissions prune [--stale-days N]
```

Default: 30 days.

```bash
"${CLAUDE_PLUGIN_DATA}/.venv/bin/python" -m nephoscope.lib.prune [--stale-days 30]
```

### `gc`

Garbage collect: drop session-tier rules from idle sessions, drop stale ask_pending rows.

**Usage:**
```
/nephoscope:permissions gc [--session-idle-days N] [--ask-pending-hours H]
```

Defaults: 7 days (sessions), 1 hour (asks).

```bash
"${CLAUDE_PLUGIN_DATA}/.venv/bin/python" -m nephoscope.lib.gc_sessions [--session-idle-days 7] [--ask-pending-hours 1]
```

### `sweep`

Run both `prune` and `gc` in sequence.

```bash
/nephoscope:permissions prune && /nephoscope:permissions gc
```

### `reconcile [--project <path>]`

Diff the JSON mirror against the DB and (optionally) apply a resolution.

**Usage:**
```
/nephoscope:permissions reconcile [--project <settings-json-path>]
```

- Omit `--project` to reconcile the global mirror.
- Default mode: `interactive` (auto-switches to `adopt` when stored hash is NULL).

```bash
"${CLAUDE_PLUGIN_DATA}/.venv/bin/nephoscope-permissions" reconcile \
  [--project "$SETTINGS_PATH"] [--mode interactive|plan|auto-db-wins|auto-json-wins|adopt]
```

### `mirror-status`

Print a table showing the global mirror and each registered project.

Columns: scope, path, last_synced, hash_status (`stamped` / `null` / `mismatch`).

```bash
"${CLAUDE_PLUGIN_DATA}/.venv/bin/nephoscope-permissions" mirror-status
```

### `mirror-dry-run [--project <path>]`

Build the mirror JSON from DB (same path `sync_*` uses) and write to stdout — no disk writes.

**Usage:**
```
/nephoscope:permissions mirror-dry-run [--project <settings-json-path>]
```

```bash
"${CLAUDE_PLUGIN_DATA}/.venv/bin/nephoscope-permissions" mirror-dry-run \
  [--project "$SETTINGS_PATH"]
```

### `reload-hint`

Touch `settings.json` mtime via `Path.touch()` to force Claude Code's settings re-read.

**Safety:** only touches the path supplied via `--settings-path`. Tests must always pass a sandbox path.

```bash
"${CLAUDE_PLUGIN_DATA}/.venv/bin/nephoscope-permissions" reload-hint \
  --settings-path "$SETTINGS_PATH"
```

## Implementation Notes

1. **Routing**: Parse `$ARGUMENTS` to extract subcommand and option flags. Route each subcommand to the appropriate `nephoscope-*` console script (installed into the plugin venv by the SessionStart bootstrap hook), the `nephoscope.*` module, or a direct SQLite query.

2. **Error handling**: If any Bash invocation fails, report the stderr and exit non-zero. Do not swallow errors.

3. **Context capture**: The `--home`, `--cwd`, `--project-root` flags needed by pattern-variants are available via Bash environment and current directory.

4. **Narrow tool allowlist**: Only Bash (scoped to the plugin venv, standard text utilities, and `sqlite3`) and Read. No Agent spawning, no external API calls, no network access.

5. **Output format**: Learner commands produce pipe-delimited or JSON output; `review.sh` and GC scripts print counts/results to stdout; SQLite queries use `.headers` and `.mode column` for readability.

## Example workflows

**View current state:**
```
/nephoscope:permissions status
```

**Review and promote a candidate:**
```
/nephoscope:permissions propose      # see candidates
/nephoscope:permissions review       # walk through prompts
```

**Clean up old data:**
```
/nephoscope:permissions prune --stale-days 30
/nephoscope:permissions gc --session-idle-days 7
```

**Export and seed fixture:**
```
/nephoscope:permissions seed --export > my_rules.yaml
/nephoscope:permissions seed            # reload from config/fixtures/safe_shapes.yaml
```
