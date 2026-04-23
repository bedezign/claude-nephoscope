# nephoscope

A Claude Code plugin that observes tool-call activity, learns permission rules from your trust decisions, and renders them back into `settings.json` so the native permission gate auto-approves (or denies) without a hook round-trip.

Two learners are built on the same observation pipeline:

- **Permission learner** — canonicalizes tool calls into rule shapes, persists user trust decisions in a consolidated `permissions` table, and renders them into `~/.claude/settings.json` (or a project `settings.json`).
- **Instinct summarizer** — aggregates recent tool activity into periodic text summaries consumed by a separate background agent that authors atomic "instinct" markdown files in the configured instincts directory.

Nephoscope is distributed as a Claude Code plugin. Installing the plugin registers four hooks, materialises a SQLite database in the plugin data directory, and installs the Python runtime in a plugin-scoped virtual environment. No `settings.json` edits are required by the user.

## Prerequisites

- Claude Code (recent release — plugin support is required).
- Python 3.10 or newer, available on `PATH`.
- [`uv`](https://docs.astral.sh/uv/) for dependency management. The SessionStart bootstrap hook calls `uv venv` and `uv pip install -e`.
- SQLite CLI (`sqlite3`) for the `/permissions` slash command's ad-hoc queries.

## Install

### Local development install

Clone the repository and register it as a local marketplace, then install via the marketplace interface:

```bash
git clone https://github.com/<owner>/nephoscope.git
cd nephoscope
```

From a Claude Code session:

```
/plugin marketplace add /path/to/nephoscope
/plugin install nephoscope@bedezign
```

Source edits do not automatically flow through to the running hooks — changes are promoted by bumping the `version` field in `.claude-plugin/plugin.json`, then running `/plugin update nephoscope@bedezign`.

### Early adopter install

Clone the repository and point Claude Code at the checkout with a per-invocation flag (no marketplace setup required):

```bash
git clone https://github.com/<owner>/nephoscope.git
claude --plugin-dir /absolute/path/to/nephoscope
```

### Marketplace install

```
/plugin install nephoscope@claude-plugins-official
```

(Marketplace submission is pending; the local marketplace or `--plugin-dir` paths above are the current options.)

## First-run bootstrap

The first time Claude Code loads the plugin, the `SessionStart` hook runs `hooks/bootstrap.sh`, which:

1. Creates `${CLAUDE_PLUGIN_DATA}/.venv`.
2. Installs the plugin's Python package into that venv (sourcing the cache copy of the code, not a live checkout).
3. Copies the current `pyproject.toml` to `${CLAUDE_PLUGIN_DATA}/pyproject.toml.cached`, so subsequent sessions skip the install step unless the manifest changed.

The observations database is created lazily on first tool call: the recorder checks for `${OBSERVABILITY_DB:-${CLAUDE_PLUGIN_DATA}/observations.db}`, materialises the parent directory, and applies `lib/schema.sql` if the file is missing. You can also force-create the DB yourself:

```bash
"${CLAUDE_PLUGIN_DATA}/.venv/bin/nephoscope-init"
```

`nephoscope-init --db-path /some/other/path.db` materialises a DB at a non-default location (useful for tests or parallel consumers).

## Verify install

After a fresh session fires at least one tool call:

```bash
DB="${OBSERVABILITY_DB:-${CLAUDE_PLUGIN_DATA}/observations.db}"
sqlite3 "$DB" 'SELECT COUNT(*) FROM tool_calls;'
```

Should print a non-zero row count.

## Uninstall

For marketplace or local marketplace installs:

```
/plugin uninstall nephoscope@bedezign
```

Or for official marketplace (when available):

```
/plugin uninstall nephoscope@claude-plugins-official
```

For `--plugin-dir` installs, remove the per-invocation flag or any alias you created.

Optional — drop the plugin's data directory as well:

```bash
rm -rf "${CLAUDE_PLUGIN_DATA}"
```

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `OBSERVABILITY_DB` | Observations database path | `${CLAUDE_PLUGIN_DATA}/observations.db`, else `~/.cache/nephoscope/observations.db` |
| `NEPHOSCOPE_DISABLE_MARKER` | Opt-out marker path — if the file exists, all hooks short-circuit silently | `${CLAUDE_PLUGIN_DATA}/disabled`, else `~/.config/nephoscope/disabled` |
| `NEPHOSCOPE_INSTINCT_DIR` | Where the instinct summarizer expects the observer to write `.md` files | `${CLAUDE_PLUGIN_DATA}/instincts`, else `~/.claude/instincts` |
| `CLAUDE_EXTRA_DIRS` | Colon-separated additional directories to treat as in-project for scope classification | unset |
| `HOOK_FULL_MATCH` | Debug: force the runtime hook to fully dispatch even for mirror-covered tool classes | unset |

## Opting out temporarily

```bash
touch "${NEPHOSCOPE_DISABLE_MARKER:-${CLAUDE_PLUGIN_DATA}/disabled}"
```

All hooks exit 0 silently while the marker exists. Remove the marker to re-enable.

## Architecture at a glance

- **Single source of truth: the DB.** The observations database owns every permission rule as a structured row. `settings.json` files are mirrors, regenerated eagerly on every DB write.
- **Single flat schema.** `lib/schema.sql` holds every `CREATE TABLE` / `CREATE VIEW`. No migration system, no `vN.sql` sequence, no `PRAGMA user_version`. Schema changes edit that file and rebuild against it.
- **Rule shapes carry patterns.** `rule_shapes` supports literal and pattern forms on every axis: `verb` may be a `$VAR/...` prefix, `flags` may be `"*"` (wildcard), `path_spec` may be a `$HOME` / `$CWD` / `$PROJECT_ROOT` glob.
- **Three-tier scope on one table.** `permissions(rule_shape_id, session_id?, project_id?, decision, source, reason, decided_at)` — at most one of `session_id` / `project_id` set; both NULL = global. Match priority: session → project → global.
- **Tool-class-aware matching.** Bash, file tools (Read/Edit/Write/NotebookEdit), flat tools (Grep/Glob/WebSearch), MCP (`mcp__ns__tool`), and orchestration tools each dispatch to their own matcher under `src/nephoscope/learners/permission/match/`.

## Layout

```
nephoscope/
  .claude-plugin/
    plugin.json                        plugin manifest
    marketplace.json                   local marketplace definition
  hooks/
    hooks.json                         declares the 4 runtime hooks
    bootstrap.sh                       idempotent venv + package install
  commands/
    permissions.md                     /permissions slash-command doc
  src/nephoscope/
    cli/
      init_cmd.py                      nephoscope-init CLI (explicit DB bootstrap)
      permissions_cmd.py               reconcile / mirror-status / mirror-dry-run / reload-hint
    lib/
      schema.sql                       full schema
      db.py                            connection helpers, lookups, upserts
      paths.py                         runtime path resolution (DB / disable marker / instinct dir)
      scope.py                         project-root resolution, paths_for_tool_call
      sweep.py                         relabel stale pending rows → orphan
      prune.py                         delete stale candidates
      gc_sessions.py                   GC idle session-tier rules and stale ask_pending
      mirror/                          atomic JSON mirror writer + ingester + reconciler
    recorder/
      run.py                           SessionStart / PreToolUse / PostToolUse recorder + lazy DB bootstrap
    learners/
      permission/                      canonicalize, deny, learn, hook, match/, seed
      instinct/                        summarize.py + daemon launcher
  scripts/
    setup.sh                           manual dev-time venv + install + init-db
  tests/                               pytest suites
  pyproject.toml
  LICENSE
```

## Mirror model

`settings.json` files are rendered from the DB, not authored by hand.

**Write flow** (any promote / reject / unpermit / seed):

1. Learner writes the DB row and commits.
2. `lib.mirror.writer.sync_affected(permission_id)` acquires `flock` on the target mirror, re-hashes the on-disk file, and compares to the stored `sha256`.
3. On match: build the new content from DB rows → write `<path>.tmp` → `fsync` → atomic `rename` → rehash → stamp `settings_json_sha256` + `settings_json_last_synced`.
4. On hash mismatch: raise `MirrorHashMismatch`. The learner CLI surfaces `"Settings file modified externally. Run '/permissions reconcile' and retry."`
5. Three-attempt retry loop absorbs the rare race between re-hash and rename. Stale `.tmp` siblings older than 5 min get cleaned on startup.

**Canonical forms** rendered by `serializer.py`:

| Tool class | Example |
|---|---|
| Bash literal | `Bash(git status)` |
| Bash pattern | `Bash(git *)` |
| File with path | `Read(//abs/path/**)`, `Edit(//abs/file)` |
| File, no path | bare `Edit` |
| Flat | bare `Grep`, `Glob`, `WebSearch` |
| MCP | `mcp__example__action`, `mcp__example__*` |
| Orchestration | never serialized — default-allow |

`ingester.py` is the inverse (strict parse, no fuzzy normalization — malformed entries raise with the offending string and source path). Round-trip is property-tested.

## Reconcile

`/permissions reconcile` diffs the JSON mirror against the DB and offers three resolutions:

- `db-wins` — the DB is authoritative; the JSON file gets regenerated to match.
- `json-wins` — the user's hand edits are authoritative; the DB adopts the JSON entries.
- `per-entry` — interactive walk-through for each differing rule.

First-touch path: when `settings_json_sha256 IS NULL` (DB has never seen this file), `reconcile` runs in **adopt mode** — default resolution is `json-wins` so existing user rules enter the DB cleanly.

Non-interactive modes:

- `--mode plan` — print the diff and exit.
- `--mode auto-db-wins` / `--mode auto-json-wins` — apply the default resolution without prompting.

## Runtime hook priority

On every Bash tool call the permissions hook follows this priority:

1. **Rule-deny** — procedural guards (`deny.py`: sudo, writes to guarded system paths, truncate-redirects) and declarative rules (`deny.yaml`). Hard block.
2. **DB rejected** — tier-ordered lookup in `permissions` WHERE decision=`rejected`. First match wins. Hard deny with reason.
3. **Unified clearance** — per leaf: tool-class dispatch → matcher finds `(rule_shape, permission)` tuples → tier priority (session → project → global) → `approved` yields `allow`.
4. **Fall through** — `NoOpinion`. The native gate (reading the rendered `settings.json`) handles the prompt.

Outcomes: `deny` (hard block) / `ask` (register pending row, prompt user) / `allow` (auto-approve) / fall-through.

The JSON mirror is the primary gate: once a rule is in `settings.json`, the native gate approves it without the hook firing. The hook short-circuits to `NoOpinion` for mirror-covered tool classes unless `HOOK_FULL_MATCH=1` (debugging). Session-tier approvals live DB-only (no JSON analogue) and stay fully active through the hook.

## Review CLI

Candidates accumulate as tool calls are observed. To triage:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/src/nephoscope/learners/permission/scripts/review.sh"
```

Per eligible candidate: per-axis prompts (verb / paths / flags — literal or generalize) then tier (session / project / global). The script shells out to `nephoscope-learn promote --sync`; on `MirrorHashMismatch` it stops and instructs the user to run `/permissions reconcile`.

## Fixture round-trip

`src/nephoscope/learners/permission/config/fixtures/safe_shapes.yaml` is the durable, version-controlled snapshot of user trust decisions. `nephoscope-learn seed` loads it into the DB (and syncs mirrors); `nephoscope-learn seed --export` dumps the DB back to YAML.

## `/permissions` subcommands

Full docs in `commands/permissions.md`. Key subcommands:

- `status` — per-tier rule counts, candidate count, recent asks.
- `scan` / `propose` / `review` — learner accumulation + triage.
- `list [approved|rejected|candidates] [--tier ...]` — query `v_permissions` / `v_candidates`.
- `promote` / `reject` / `unpermit` — DB mutations, each auto-syncs the affected mirrors.
- `seed [--export]` — fixture apply / dump.
- `prune [--stale-days N]` / `gc` / `sweep` — housekeeping.
- `reconcile [--project <path>] [--mode ...]` — diff and resolve a mirror.
- `mirror-status` — table of all mirrors with `stamped` / `null` / `mismatch` status.
- `mirror-dry-run [--project <path>]` — render projected content to stdout without touching disk.
- `reload-hint` — touch `settings.json` mtime to force a native-gate reload.

## Instinct summarizer

Runs as a 5-minute daemon loop that writes compact text summaries over recent tool-call activity and, when a `claude` CLI is available on `PATH`, spawns a Haiku sub-session to produce atomic instinct `.md` files in the configured instinct directory.

```bash
bash "${CLAUDE_PLUGIN_ROOT}/src/nephoscope/learners/instinct/scripts/start-observer.sh" start|status|stop
```

State lives under `${CLAUDE_PLUGIN_DATA}` (log + PID file).

## Housekeeping CLIs

- `nephoscope-learn sweep [--hours N]` — relabel stale `pending` rows (crashed sessions) to `orphan`. Default 1 hour. Idempotent.
- `python -m nephoscope.lib.prune [--stale-days N]` — delete old candidates with no corresponding ask_pending. Default 30 days.
- `python -m nephoscope.lib.gc_sessions [--session-idle-days N] [--ask-pending-hours N]` — drop session-tier approvals from idle sessions, drop stale ask_pending rows.

## Tests

```bash
bash scripts/setup.sh
source .venv/bin/activate
uv run pytest
```

Test suites cover the recorder, scope classifier, canonicalizer, deny rules, learner scan / propose / promote / reject / unpermit (with mirror sync), tool-class matcher dispatch, hook priority, session approvals, fixture round-trip, GC / prune / sweep, mirror serializer / ingester / writer (including `flock`, `fsync`, hash-mismatch, retry loop), reconcile engine (adopt / interactive / auto modes), `/permissions` subcommands, and end-to-end integration flow.

**Regression guard:** `tests/test_live_db_isolation.py` records the live DB's sha256 at pytest collection (via a `pytest_configure` hook in `tests/conftest.py`) and asserts byte-equality at test-session end. This guards against the test-leak-to-live failure mode where a fixture or teardown inadvertently mutates the live DB during a test run.

## License

Apache-2.0. See [LICENSE](LICENSE).
