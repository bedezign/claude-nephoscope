# Observability Module

First-class observation pipeline for Claude Code tool calls, plus two learners built on top of it:

- **Permission learner** — canonicalizes Bash commands into shapes, persists user trust judgments, auto-approves safe patterns at runtime via a PreToolUse hook.
- **Instinct summarizer** — aggregates recent tool activity into periodic summaries for a background Haiku agent that produces atomic "instincts" in `~/.claude/homunculus/`.

## Layout

```
observability/
  .venv/                          uv-managed venv: bashlex, pyyaml
  scripts/
    setup.sh                      idempotent bootstrap (venv + deps + migrations)
  lib/
    db.py                         connection helpers, lookups, upserts, migrations
    schema/v1.sql … v10.sql       sequential migrations tracked by PRAGMA user_version
    sweep.py                      relabel stale pending rows → orphan
  recorder/
    run.sh                        hook entrypoint: `run.sh pre|post`
    run.py                        writes pre row (pending), updates on post
  learners/
    permission/                   Bash auto-approval learner
      canonicalize.py             bashlex-driven shape extraction (incl. CONTENT_VERBS)
      deny.py                     procedural + YAML deny rules
      learner.py                  scan → candidates → propose / promote / reject / unreject
      hook.py, hook.sh            runtime PreToolUse gate (allow / deny / fall through)
      seed.py                     fixture apply (YAML → DB) and export (DB → YAML)
      config/
        deny.yaml                 declarative deny rules
        learner.toml              promotion thresholds
        fixtures/
          safe_shapes.yaml        round-trippable snapshot of active + rejected lists
      scripts/
        review.sh                 interactive promote/reject CLI
    instinct/                     Haiku-fed pattern-detection feeder
      summarize.py                writes compact text summary; advances cursor
      scripts/
        start-observer.sh         5-min daemon loop: summarize → claude --model haiku → commit
  tests/                          pytest suites (129 tests covering everything above)
```

Runtime DB: `~/.cache/claude/observability/observations.db` (override via `OBSERVABILITY_DB`).

## Setup

```
bash scripts/setup.sh
```

Safe to re-run. Creates the venv, installs deps, applies any pending schema migrations.

## Schema

Current version: **10**. Key entities:

- `tool_calls` — one row per tool invocation; FK-lookup columns for `tool_id`, `status_id`, `permission_mode_id`, `subagent_type_id`, `file_path_id`; sibling `tool_extras` table holds truncated payload + response blobs.
- `command_shapes` — canonicalized `(verb, subcommand, flags)` tuples from Bash calls.
- `tool_call_shapes` — M2M junction: each Bash row links to its canonical leaves.
- `permission_active` — shapes to auto-approve. FK to `command_shapes`.
- `permission_rejected` — shapes the user has explicitly declined to promote. FK to `command_shapes`.
- `permission_candidates` + `permission_candidate_sessions` — learner-side accumulation tables.
- `consumer_cursors` — per-consumer cursor for incremental scanning (`permission-learner`, `instinct-summarizer`).

## SQL views (v10)

Convenience views resolve FK lookups back to names so ad-hoc queries read naturally:

| View | Purpose |
|---|---|
| `v_tool_calls` | Every call with `tool`, `status`, `permission_mode`, `subagent_type`, `file_path`, `session_uuid`, `project_cwd` already resolved. |
| `v_recent_bash` | Bash calls only, most-recent first. |
| `v_shapes` | `command_shapes` aliased for consistency. |
| `v_active` / `v_rejected` | Auto-approval and rejection lists joined to shape fields. |
| `v_candidates` | Promotion candidates with `already_active` / `already_rejected` flags. |
| `v_active_usage` | Active shapes with observation counts (finds zero-hit seeds to prune). |
| `v_shape_usage` | Top shapes by total observations. |
| `v_session_summary` | Per-session totals: ok/err/denied/orphan/pending counts, project. |

## Permission learner

### Runtime

The PreToolUse hook (`learners/permission/hook.sh`) is wired in `~/.claude/settings.json`. On every Bash call it canonicalizes the command, checks deny rules first, then checks `permission_active`. Three outcomes:

- **deny** — matched a deny rule. Writes `denied` status to the row the recorder just inserted, then blocks.
- **allow** — every leaf's shape is in `permission_active`. Emits `permissionDecision=allow` so Claude Code skips the user prompt.
- **fall through** — unknown shape. Normal permission prompt path.

### Review CLI

Candidates accumulate as Bash calls are observed. To triage:

```
bash learners/permission/scripts/review.sh
```

For each eligible candidate: `[y/n/q]`.

- `y` — promote into `permission_active`.
- `n` — persist into `permission_rejected`. Future scans skip the shape entirely; it never re-proposes even as observations keep accumulating. Use `learner unreject` to reverse.
- `q` — quit early.

Thresholds in `learners/permission/config/learner.toml`.

### Fixture round-trip

`learners/permission/config/fixtures/safe_shapes.yaml` is the durable snapshot of user trust judgments — both active and rejected entries. Keyed by `(verb, subcommand, flags)` so it survives DB wipes.

```
python -m learners.permission.seed                # YAML → DB (idempotent)
python -m learners.permission.seed --export       # DB → YAML (overwrites)
```

Typical flow: run `review.sh` to accumulate changes, then `seed --export` to snapshot. If the DB is ever wiped, `seed` rebuilds `permission_active` + `permission_rejected` from the fixture and `learner scan` repopulates `command_shapes` + `permission_candidates` from observations.

### Learner CLI

```
python -m learners.permission.learner scan         # canonicalize new Bash rows
python -m learners.permission.learner propose      # list eligible promotions
python -m learners.permission.learner candidates   # dump candidate table
python -m learners.permission.learner active       # dump active table
python -m learners.permission.learner rejected     # dump rejected table
python -m learners.permission.learner promote --verb X [--subcommand Y] [--flags '["-a"]']
python -m learners.permission.learner reject   --verb X [--subcommand Y] [--flags '["-a"]'] [--reason "..."]
python -m learners.permission.learner unreject --verb X [--subcommand Y] [--flags '["-a"]']
```

## Instinct summarizer

Successor to `continuous-learning-v2`'s observation layer. Runs as a 5-minute daemon loop:

1. `summarize.py write` queries new rows via `v_tool_calls` past its cursor and writes a text summary to `~/.cache/claude/analysis/summary-*.txt` if there are ≥ 10 new calls.
2. The launcher invokes `claude --model haiku` with a prompt pointing at the summary.
3. The Haiku observer agent (`~/.claude/skills/continuous-learning-v2/agents/observer.md`) writes new instincts to `~/.claude/homunculus/instincts/personal/`.
4. On success, `summarize.py commit` advances the cursor.

The homunculus tree and the `/instinct-status`, `/evolve`, `/instinct-export`, `/instinct-import` commands are unchanged — this module only feeds data in.

```
bash learners/instinct/scripts/start-observer.sh start      # background
bash learners/instinct/scripts/start-observer.sh status     # check
bash learners/instinct/scripts/start-observer.sh stop       # stop
```

Log + PID at `~/.cache/claude/observability/`.

## Orphan sweeper

`pending` rows whose post-hook never completed (crashed sessions, permission-denied calls from pre-hook-era) get relabeled to `orphan` via:

```
python -m lib.sweep [--hours N]          # default threshold: 1 hour
```

Safe to run anytime. Idempotent.

## Tests

```
cd ~/.claude/observability
./.venv/bin/python -m pytest tests/ -q
```

129 tests covering recorder, canonicalizer, deny rules, learner scan + propose + promote + reject / unreject, hook allow / deny / fall-through, fixture apply / export / round-trip, sweep, instinct summarizer.

## Relationship to `continuous-learning-v2`

The `continuous-learning-v2` skill used to own the observation hook pipeline (writing to `~/.cache/claude/observations/tools.db` via its own `observe.sh`/`observe.py`/`summarize.py`/`start-observer.sh`). That layer was used as inspiration for this module and has since been retired — only the instinct + evolution half of that skill remains (the Haiku observer agent + the `/evolve` / `/instinct-*` commands + the homunculus tree). This module is the sole observation source as of 2026-04-20. The old `tools.db` remains on disk as a frozen historical archive.
