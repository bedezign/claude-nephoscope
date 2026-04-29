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

- Plugin venv: `${CLAUDE_PLUGIN_DATA}/.venv/` (installed by the SessionStart hook on first run)
- DB path: `$OBSERVABILITY_DB` if set; otherwise `${CLAUDE_PLUGIN_DATA}/observations.db`

## Preflight

Before executing any subcommand:

1. Verify `${CLAUDE_PLUGIN_DATA}/.venv/bin/nephoscope-learn` exists. If not, exit with a note that the SessionStart hook needs to run once (start a new session).
2. Resolve the DB path as `${OBSERVABILITY_DB:-${CLAUDE_PLUGIN_DATA}/observations.db}` and export it for all subprocess invocations.

If any preflight fails, STOP and report the error.

## Subcommands

Parse `$ARGUMENTS` to extract the subcommand and options:

### `status` (default if no subcommand)

Show a summary of the current permission state: approved/rejected rules per tier,
queued asks and candidates, the most frequent asks, and a suggested next step.

```bash
DB="${OBSERVABILITY_DB:-${CLAUDE_PLUGIN_DATA}/observations.db}"

# Collect every number we need in one sqlite3 call, tagged per line so the
# formatter can pick fields without doing its own SQL. Pipe-separated so a
# verb containing whitespace (e.g. an absolute path with spaces) still parses
# as a single field. Output shape:
#   M|<decision>|<tier>|<count>     rule-matrix cells
#   A|<count>                       total asks pending
#   C|<count>                       total candidates pending
#   T|<verb>|<count>                top ask verbs (up to 5)
sqlite3 "$DB" <<'EOF' | awk '
BEGIN {
  FS = "|"
  # Rule-matrix defaults — always show zero cells.
  decisions["approved"]=1; decisions["rejected"]=1
  tiers["global"]=1; tiers["project"]=1; tiers["session"]=1
  for (d in decisions) for (t in tiers) rules[d,t]=0
  asks=0; candidates=0; top_count=0
}
$1 == "M" { rules[$2,$3] = $4 ; next }
$1 == "A" { asks       = $2 ; next }
$1 == "C" { candidates = $2 ; next }
$1 == "T" { top_count++; top_verb[top_count]=$2; top_n[top_count]=$3 ; next }
END {
  # --- Rules matrix --------------------------------------------------------
  printf "  Rules       %6s  %7s  %7s\n", "global", "project", "session"
  printf "    approved  %6d  %7d  %7d\n",
    rules["approved","global"], rules["approved","project"], rules["approved","session"]
  printf "    rejected  %6d  %7d  %7d\n",
    rules["rejected","global"], rules["rejected","project"], rules["rejected","session"]
  printf "\n"

  # --- Queue block ---------------------------------------------------------
  if (asks == 0 && candidates == 0) {
    printf "  Queue:      no asks, no candidates. All caught up.\n"
  } else {
    if (asks > 0) {
      printf "  Queue:      %d asks (prompts awaiting a rule)\n", asks
      if (candidates > 0) {
        printf "              %d candidates (recurring patterns for promotion)\n", candidates
      } else {
        printf "              0 candidates\n"
      }
    } else {
      printf "  Queue:      0 asks\n"
      printf "              %d candidates (recurring patterns for promotion)\n", candidates
    }
  }

  # --- Top asks ------------------------------------------------------------
  if (top_count > 0) {
    line = "  Top asks:   "
    for (i = 1; i <= top_count; i++) {
      if (i > 1) line = line ", "
      line = line top_verb[i] " \xc3\x97" top_n[i]
    }
    print line
  }

  # --- Try next ------------------------------------------------------------
  if (asks > 0 || candidates > 0) {
    # Pick a sample verb for the scoped-rule example. Use the dominant verb
    # when one verb owns >= 60% of asks; otherwise fall back to "rm".
    sample = "rm"
    if (top_count > 0 && asks > 0) {
      if (top_n[1] * 100 >= asks * 60) sample = top_verb[1]
    }
    printf "\n"
    printf "  Try next:   /nephoscope:permissions scan \xe2\x86\x92 propose \xe2\x86\x92 review\n"
    printf "  Or write a scoped rule directly, e.g. allow %s inside this project:\n", sample
    printf "    /nephoscope:permissions promote --verb %s --flags '"'"'*'"'"' \\\n", sample
    printf "        --path-spec '"'"'$PROJECT_ROOT/**'"'"' --tier project\n"
  }
}
'
.mode list
.separator "|"
-- Rules matrix: approved/rejected per tier.
SELECT 'M', 'approved', 'global',  COUNT(*) FROM permissions
 WHERE session_id IS NULL AND project_id IS NULL AND decision = 'approved'
UNION ALL
SELECT 'M', 'approved', 'project', COUNT(*) FROM permissions
 WHERE project_id IS NOT NULL AND decision = 'approved'
UNION ALL
SELECT 'M', 'approved', 'session', COUNT(*) FROM permissions
 WHERE session_id IS NOT NULL AND decision = 'approved'
UNION ALL
SELECT 'M', 'rejected', 'global',  COUNT(*) FROM permissions
 WHERE session_id IS NULL AND project_id IS NULL AND decision = 'rejected'
UNION ALL
SELECT 'M', 'rejected', 'project', COUNT(*) FROM permissions
 WHERE project_id IS NOT NULL AND decision = 'rejected'
UNION ALL
SELECT 'M', 'rejected', 'session', COUNT(*) FROM permissions
 WHERE session_id IS NOT NULL AND decision = 'rejected';

-- Queue totals.
SELECT 'A', COUNT(*) FROM permission_ask_pending;
SELECT 'C', COUNT(*) FROM permission_candidates;

-- Top 5 ask verbs by count.
SELECT 'T', verb, COUNT(*) AS n
  FROM permission_ask_pending
 GROUP BY verb
 ORDER BY n DESC, verb ASC
 LIMIT 5;
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

Invoke the `nephoscope-review` console script:

```bash
nephoscope-review
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

When trusted directories are configured in the nephoscope config file, `mirror-status` also prints a "Workspace coverage" section below the table. This section lists each trusted directory with a `✓` if its Read/Edit/Write entries are present in the global settings file, or `✗` if not. A hint at the end suggests running `reconcile` when any entries are missing.

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

5. **Output format**: Learner commands produce pipe-delimited or JSON output; `nephoscope-review` and GC scripts print counts/results to stdout; SQLite queries use `.headers` and `.mode column` for readability.

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
