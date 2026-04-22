# Observability Module

First-class observation pipeline for Claude Code tool calls, plus two learners built on top of it:

- **Permission learner** — canonicalizes tool calls into rule shapes, persists user trust decisions in a consolidated `permissions` table, and renders them into `~/.claude/settings.json` so the native permission gate auto-approves (or denies) without a hook round-trip.
- **Instinct summarizer** — aggregates recent tool activity into periodic summaries for a background Haiku agent that produces atomic "instincts" in `~/.claude/homunculus/`.

## Architecture at a glance

- **Single source of truth: the DB.** `~/.cache/claude/observability/observations.db` owns every permission rule as a structured row. `settings.json` files are mirrors, regenerated eagerly on every DB write.
- **Single flat schema.** `lib/schema.sql` holds every `CREATE TABLE` / `CREATE VIEW`. No migration system, no `vN.sql` sequence, no `PRAGMA user_version`. Schema changes edit that file and rebuild against it.
- **Rule shapes carry patterns.** `rule_shapes` supports literal and pattern forms on every axis: `verb` may be a `$VAR/...` prefix, `flags` may be `"*"` (wildcard), `path_spec` may be a `$HOME` / `$CWD` / `$PROJECT_ROOT` glob.
- **Three-tier scope on one table.** `permissions(rule_shape_id, session_id?, project_id?, decision, source, reason, decided_at)` — at most one of `session_id` / `project_id` set; both NULL = global. Match priority: session → project → global.
- **Tool-class-aware matching.** Bash, file tools (Read/Edit/Write/NotebookEdit), flat tools (Grep/Glob/WebSearch), MCP (`mcp__ns__tool`), and orchestration tools each dispatch to their own matcher under `learners/permission/match/`.

## Layout

```
observability/
  scripts/
    setup.sh                           idempotent bootstrap (venv + deps + schema)
  lib/
    schema.sql                         full schema (projects, sessions, permissions, rule_shapes, …)
    db.py                              connection helpers, lookups, upserts, mirror-hash helpers
    scope.py                           project-root resolution, paths_for_tool_call
    sweep.py                           relabel stale pending rows → orphan
    prune.py                           delete stale candidates
    gc_sessions.py                     GC idle session-tier rules and stale ask_pending
    mirror/
      serializer.py                    structured row → canonical string (one renderer per tool class)
      ingester.py                      canonical string → structured row (strict parse)
      writer.py                        atomic JSON mirror writer (flock + fsync + rename + rehash)
      reconcile.py                     diff engine + interactive resolver (DB-wins / JSON-wins / per-entry / adopt)
      tool_class.py                    verb → tool-class dispatch map
  recorder/
    run.sh                             hook entrypoint: `run.sh pre|post`
    run.py                             writes pre row (pending), updates on post
  learners/
    permission/
      canonicalize.py                  bashlex-driven shape extraction (+ CONTENT_VERBS, numeric-flag collapse)
      deny.py                          procedural + YAML deny/ask rules
      learner.py                       scan → candidates → propose / promote / reject / unpermit (all sync-to-mirror)
      hook.py, hook.sh                 runtime PreToolUse gate (shrunk: mirror is primary gate)
      seed.py                          fixture apply (YAML → DB, sync-to-mirror) / export (DB → YAML)
      match/
        bash.py                        verb/subcommand/flags matcher
        file.py                        path-glob matcher for Read/Edit/Write/NotebookEdit
        flat.py                        bare-verb matcher for Grep/Glob/WebSearch
        mcp.py                         fully-qualified MCP tool matcher (`mcp__ns__tool`, wildcards)
        orchestration.py               default-allow for orchestration tools
      config/
        deny.yaml                      declarative deny + ask rules
        learner.toml                   promotion thresholds
        fixtures/safe_shapes.yaml      durable snapshot of approved + rejected decisions
      scripts/
        review.sh                      interactive promote/reject CLI
    instinct/
      summarize.py                     writes compact text summary for the Haiku observer
      scripts/start-observer.sh        5-min daemon loop
  commands/
    permissions.md                     /permissions slash-command doc (BookStack-guarded)
    permissions_cmd.py                 reconcile / mirror-status / mirror-dry-run / reload-hint implementations
  tests/                               pytest suites (645 tests)
```

Runtime DB: `/home/steve/.cache/claude/observability/observations.db` (override via `OBSERVABILITY_DB`).

**Important:** the DB path is resolved **lazily on each `_open()` call** via `lib.db._db_path()` rather than captured at module import. Tests or wrappers overriding `OBSERVABILITY_DB` via `monkeypatch.setenv` work correctly with no additional setup. The previous module-level `DB_PATH` constant is removed. This prevents a class of test-isolation bugs where tests would read from the live DB because the env override ran after module import.

## Setup

```
bash scripts/setup.sh
```

Safe to re-run. Creates the venv, installs deps (`bashlex`, `pyyaml`), and builds the DB from `lib/schema.sql` if missing.

## Schema highlights (`lib/schema.sql`)

- `projects` — root paths with auto-resolved `root` column plus mirror bookkeeping (`settings_json_path`, `settings_json_sha256`, `settings_json_last_synced`).
- `global_mirror` — singleton row (id=1) carrying the same mirror triplet for `~/.claude/settings.json`.
- `rule_shapes` — one row per rule pattern. `UNIQUE(verb, subcommand-or-empty, flags, path_spec-or-empty)`.
- `permissions` — `(rule_shape_id, session_id?, project_id?, decision, source, reason?, decided_at)`. `decision ∈ {approved, rejected, ask}`. `CHECK` enforces "at most one of session_id / project_id".
- `permission_ask_pending` — transient hook→recorder correlation by `tool_use_id` (shape fields inlined).
- `permission_candidates` + `permission_candidate_sessions` — learner accumulation (inline shape fields + stats; no separate `observed_shapes`).
- `tool_calls` — one row per invocation; FK lookups for `tool_id`, `status_id`, `permission_mode_id`, `subagent_type_id`, `file_path_id`. Sidecar `tool_extras` holds truncated payload + response blobs.
- `consumer_cursors` — per-consumer cursor (`permission-learner`, `instinct-summarizer`).

Views (`v_tool_calls`, `v_recent_bash`, `v_permissions`, `v_candidates`, `v_rule_shapes`, `v_session_summary`) resolve FK lookups back to names for ad-hoc queries. **Notable:** `v_tool_calls` exposes `project_name` (from `projects.name`) alongside the existing `project_cwd`. This is used by `learners/instinct/summarize.py` and is generally useful for ad-hoc queries that need the short project name rather than the full cwd.

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
| MCP | `mcp__claude-peers__send_message`, `mcp__claude-peers__*` |
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

The PreToolUse hook at `learners/permission/hook.sh` is wired in `~/.claude/settings.json`. On every tool call it follows this priority:

1. **Rule-deny** — procedural guards (`deny.py`: sudo, writes to guarded system paths, truncate-redirects) and declarative rules (`deny.yaml`). Hard block.
2. **DB rejected** — tier-ordered lookup in `permissions` WHERE decision=`rejected`. First match wins. Hard deny with reason.
3. **Unified clearance** — per leaf: tool-class dispatch → matcher finds `(rule_shape, permission)` tuples → tier priority (session → project → global) → `approved` yields `allow`.
4. **Fall through** — `NoOpinion`. The native gate (reading the rendered `settings.json`) handles the prompt.

Outcomes: `deny` (hard block) / `ask` (register pending row, prompt user) / `allow` (auto-approve) / fall-through.

**Hook role shrank in Phase 8.5.** The JSON mirror is the primary gate: once a rule is in `settings.json`, the native gate approves it without the hook firing. The hook short-circuits to `NoOpinion` for mirror-covered tool classes unless `HOOK_FULL_MATCH=1` (debugging). Session-tier approvals still live DB-only (no JSON analogue) and stay fully active through the hook.

## Review CLI

Candidates accumulate as tool calls are observed. To triage:

```
bash learners/permission/scripts/review.sh
```

Per eligible candidate: per-axis prompts (verb / paths / flags — literal or generalize) then tier (session / project / global). The script shells out to `learner promote --sync`; on `MirrorHashMismatch` it stops and instructs the user to run `/permissions reconcile`.

## Fixture round-trip

`learners/permission/config/fixtures/safe_shapes.yaml` is the durable, version-controlled snapshot of user trust decisions. `python -m learners.permission.seed` loads it into the DB (and syncs mirrors); `--export` dumps the DB back to YAML.

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

Successor to `continuous-learning-v2`'s observation layer. Runs as a 5-minute daemon loop:

1. `summarize.py write` queries new rows via `v_tool_calls` past its cursor and writes a text summary to `~/.cache/claude/analysis/summary-*.txt` if there are ≥ 10 new calls.
2. The launcher invokes `claude --model haiku` with a prompt pointing at the summary.
3. The Haiku observer agent (`~/.claude/skills/continuous-learning-v2/agents/observer.md`) writes new instincts to `~/.claude/homunculus/instincts/personal/`.
4. On success, `summarize.py commit` advances the cursor.

```
bash learners/instinct/scripts/start-observer.sh start|status|stop
```

Log + PID at `~/.cache/claude/observability/`.

## Housekeeping

- `python -m lib.sweep [--hours N]` — relabel stale `pending` rows (crashed sessions) to `orphan`. Default 1 hour. Idempotent.
- `python -m lib.prune [--stale-days N]` — delete old candidates with no corresponding ask_pending. Default 30 days.
- `python -m lib.gc_sessions [--session-idle-days N] [--ask-pending-hours N]` — drop session-tier approvals from idle sessions, drop stale ask_pending rows.

## Tests

```
cd /home/steve/.claude/observability
./.venv/bin/python -m pytest tests/ -q
```

645 tests covering recorder, scope, canonicalizer, deny rules, learner scan / propose / promote / reject / unpermit (with mirror sync), tool-class matcher dispatch, hook priority, session approvals, fixture round-trip, GC / prune / sweep, mirror serializer / ingester / writer (including `flock`, `fsync`, hash-mismatch, retry loop), reconcile engine (adopt / interactive / auto modes), `/permissions` subcommands, end-to-end integration flow, instinct summarizer.

**Regression guard:** `tests/test_live_db_isolation.py` is a sentinel test that records the live DB's sha256 at pytest collection (via a `pytest_configure` hook in `tests/conftest.py`) and asserts byte-equality at test-session end. This guards against the test-leak-to-live failure mode where a fixture or teardown inadvertently mutates the live DB during a test run. If this test fails, a fixture somewhere is hitting the live DB and needs isolation.

## Relationship to `continuous-learning-v2`

`continuous-learning-v2` used to own the observation hook pipeline. That layer was retired 2026-04-20; only the instinct + evolution half of the skill remains (the Haiku observer agent + the `/evolve` / `/instinct-*` commands + the homunculus tree). This module is the sole observation source. The old `~/.cache/claude/observations/tools.db` remains on disk as a frozen historical archive.
