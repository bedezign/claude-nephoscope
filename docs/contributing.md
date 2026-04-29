# Contributing

This page covers the plugin's internals. If you just want to use nephoscope, the user docs above are the right entry — start at [getting started](getting-started.md).

This page collects the architecture, data flow, mirror pipeline, reconcile engine, review CLI, fixture round-trip, and test suites. It's aimed at anyone who wants to change the plugin, not just use it.

## Overview

Nephoscope is a Claude Code plugin that observes tool-call activity, learns permission rules from your trust decisions, and renders them back into `settings.json` so the native permission gate auto-approves (or denies) without a hook round-trip.

Two learners are built on the same observation pipeline:

- **Permission learner** — canonicalizes tool calls into rule shapes, persists user trust decisions in a consolidated `permissions` table, and renders them into `~/.claude/settings.json` (or a project `settings.json`).
- **Instinct summarizer** — aggregates recent tool activity into periodic text summaries. An "instinct" is a short Markdown note of the form *"when X, prefer Y"* distilled from observed usage; a separate Claude Code skill (not part of this plugin) consumes those notes to surface learned patterns during later sessions. Nephoscope only writes the notes — it does not load or act on them itself.

Installing the plugin registers four hooks, materialises a SQLite database in the plugin data directory, and installs the Python runtime in a plugin-scoped virtual environment. No `settings.json` edits are required by the user.

## Architecture at a glance

- **Single source of truth: the DB.** The observations database owns every permission rule as a structured row. `settings.json` files are mirrors, regenerated eagerly on every DB write.
- **Single flat schema.** `lib/schema.sql` holds every `CREATE TABLE` / `CREATE VIEW` and sets `PRAGMA user_version`. Additive changes (new columns) are also applied to live DBs via `_MIGRATIONS` in `lib/db.py`; the schema file remains the authoritative definition for fresh installs.
- **Rule shapes carry patterns.** `rule_shapes` supports literal and pattern forms on every axis: `verb` may be a `$VAR/...` prefix or `"*"` (matches any verb), `flags` may be `"*"` (wildcard), `path_spec` may be a `$HOME` / `$CWD` / `$PROJECT_ROOT` glob or basename glob (`$VAR/**/<filename>`), and `context` may be `"any"`, `"toplevel"`, or `"substitution"` to scope a rule to where a command appears in the shell tree.
- **Three-tier scope on one table.** `permissions(rule_shape_id, session_id?, project_id?, decision, source, reason, decided_at)` — at most one of `session_id` / `project_id` set; both NULL = global. Match priority: session → project → global.
- **Tool-class-aware matching.** Bash, file tools (Read/Edit/Write/MultiEdit/NotebookEdit), flat tools (Grep/Glob/WebSearch), MCP (`mcp__ns__tool`), and orchestration tools each dispatch to their own matcher under `src/nephoscope/learners/permission/match/`.

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
    permissions.md                     /nephoscope:permissions slash-command doc
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
2. `lib.mirror.writer.sync_affected(permission_id)` acquires `flock` on the target mirror and computes the **permissions-slice hash** of the on-disk file via `lib.mirror.permissions_hash.settings_permissions_hash` (covers only `permissions.{allow, deny, ask}`, sorted to a canonical form). Compares that hash to the stored `sha256`.
3. On match: build the new content from DB rows → write `<path>.tmp` → `fsync` → atomic `rename` → re-hash the new content's permissions slice → stamp `settings_json_sha256` + `settings_json_last_synced`.
4. On hash mismatch: raise `MirrorHashMismatch`. The learner CLI surfaces `"Settings file modified externally. Run '/nephoscope:permissions reconcile' and retry."`
5. Three-attempt retry loop absorbs the rare race between re-hash and rename. Stale `.tmp` siblings older than 5 min get cleaned on startup.

The slice-only hash means edits to **non-permissions** parts of `settings.json` (hooks block, env, model, `permissions.defaultMode`, `permissions.additionalDirectories`) do not flip the stored hash to mismatch — only changes to the rule arrays themselves do. Malformed or non-UTF-8 content is treated as a mismatch (the writer raises `MirrorHashMismatch` with a "settings.json is malformed" message; `mirror-status` reports `mismatch`).

When trusted directories are configured (global mirror only), the write flow also manages a top-level `_nephoscopeAllowedTools` key. The `_inject_permissions` entry point in `lib/mirror/writer.py` generates `Write(<root>/**)`, `Edit(<root>/**)`, `MultiEdit(<root>/**)`, `NotebookEdit(<root>/**)`, `Read(<root>/**)` entries for each configured trusted directory and appends them to `permissions.allow`. The dedicated `_nephoscopeAllowedTools` key tracks exactly which entries nephoscope wrote, so re-syncs replace rather than accumulate. When `trusted_dirs` is empty, the key is removed.

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

`/nephoscope:permissions reconcile` diffs the JSON mirror against the DB and offers three resolutions:

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

Candidates accumulate as tool calls are observed. The primary entry is `/nephoscope:permissions review` from a Claude Code session. To invoke directly from a terminal:

```bash
nephoscope-review
```

Per eligible candidate: per-axis prompts (verb / paths / flags — literal or generalize) then tier (session / project / global). Calls into `lib.db` and `learner` directly; on `MirrorHashMismatch` it stops and instructs the user to run `/nephoscope:permissions reconcile`.

## Fixture round-trip

The fixture files in `src/nephoscope/learners/permission/config/fixtures/` split into two roles:

- **Shipped seed defaults** — `credential_leaks.yaml`, `secret_manager_standalones.yaml`, and `safe_shapes.yaml`. All three are auto-loaded by `nephoscope-init` on a fresh DB.
- **Durable trust-decision snapshot** — `safe_shapes.yaml`. Version-controlled record of user-shaped rules.

`nephoscope-learn seed` can be used to reload fixtures or export the current permissions to YAML via `nephoscope-learn seed --export`, which dumps the DB back to YAML format.

## `/nephoscope:permissions` subcommands

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

It's a manual daemon, not a plugin hook — launch it from your own shell:

```bash
nephoscope-observer start     # start in background
nephoscope-observer status    # check status
nephoscope-observer stop      # stop daemon
nephoscope-observer foreground  # run in foreground (for debugging)
```

State (log + PID file) lives under `${CLAUDE_PLUGIN_DATA}` — for a `nephoscope@bedezign` install that resolves to `~/.claude/plugins/data/nephoscope-bedezign/`.

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

Test suites cover:

- Recorder + scope classifier + canonicalizer + deny rules.
- Learner lifecycle: scan, propose, promote, reject, unpermit — each with mirror sync.
- Tool-class matcher dispatch, hook priority, session-tier approvals, fixture round-trip.
- Housekeeping: GC, prune, sweep.
- Mirror pipeline: serializer + ingester + writer (including `flock`, `fsync`, hash-mismatch, retry loop).
- Reconcile engine (adopt / interactive / auto modes).
- `/nephoscope:permissions` subcommands and end-to-end integration flow.

**Regression guard:** `tests/test_live_db_isolation.py` records the live DB's sha256 at pytest collection (via a `pytest_configure` hook in `tests/conftest.py`) and asserts byte-equality at test-session end. This guards against the test-leak-to-live failure mode where a fixture or teardown inadvertently mutates the live DB during a test run.
