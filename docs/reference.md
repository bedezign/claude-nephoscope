# Reference

Dense lookup tables. For concepts, read [how it works](how-it-works.md); for command walkthroughs, [daily use](daily-use.md); for every flag and option, [`../commands/permissions.md`](../commands/permissions.md).

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `OBSERVABILITY_DB` | Observations database path | `${CLAUDE_PLUGIN_DATA}/observations.db`, else `~/.cache/nephoscope/observations.db` |
| `NEPHOSCOPE_CONFIG` | Path to the nephoscope config file (TOML) | `~/.config/nephoscope/config.toml` |
| `NEPHOSCOPE_DISABLE_MARKER` | Opt-out marker path — if the file exists, all hooks short-circuit silently | `${CLAUDE_PLUGIN_DATA}/disabled`, else `~/.config/nephoscope/disabled` |
| `NEPHOSCOPE_INSTINCT_DIR` | Where the instinct summarizer expects the observer to write `.md` files | `${CLAUDE_PLUGIN_DATA}/instincts`, else `~/.claude/instincts` |
| `HOOK_FULL_MATCH` | Debug: force the runtime hook to fully dispatch even for mirror-covered tool classes | unset |

`${CLAUDE_PLUGIN_DATA}` is set by Claude Code when a plugin loads; for a `nephoscope@bedezign` install it resolves to `~/.claude/plugins/data/nephoscope-bedezign/`.

## Configuration file

Nephoscope reads its settings from a TOML file at `$NEPHOSCOPE_CONFIG` (default `~/.config/nephoscope/config.toml`). An absent file is fine — all settings default silently. Malformed TOML raises an error.

Three keys are supported:

| Key | Type | Default | Purpose |
|---|---|---|---|
| `trusted_dirs` | list of strings | `[]` | Top-level project directories. Files under these paths are pre-approved for Read/Edit/Write/MultiEdit/NotebookEdit via injection into the global mirror's `_nephoscopeAllowedTools` key. Also enables the `$TRUSTED_DIR` placeholder. |
| `auto_register_project_paths` | boolean | `false` | When true, `nephoscope-init` silently adds the current working directory to `trusted_dirs` instead of prompting. |
| `non_bash_tool_matching` | boolean | `false` | Enables full DB matching for non-Bash tool classes (Write/Edit/MultiEdit/NotebookEdit/Read). When false (default), non-Bash tool matching follows mirror-only behaviour. |

Example configuration file:

```toml
trusted_dirs = ["/home/you/code/myproject", "/opt/company/shared"]
auto_register_project_paths = false
non_bash_tool_matching = false
```

## Placeholders

Path-spec shortcuts that nephoscope expands at the time a rule is evaluated.

| Placeholder | Resolves to | When it's set |
|---|---|---|
| `$HOME` | Your home directory (e.g. `/home/you`) | Always available. |
| `$CWD` | The directory Claude Code was started in | Set per session, fixed for the life of the session. |
| `$PROJECT_ROOT` | The project root — the nearest ancestor directory of `$CWD` that looks like a project (has a `.git`, `pyproject.toml`, `package.json`, or similar marker) | Set when a recognisable project is detected; otherwise falls back to `$CWD`. |
| `$TRUSTED_DIR` | A trusted directory listed in `trusted_dirs` in the config file. | Set when at least one trusted directory is configured. |
| `$ADDITIONAL_DIR` | An additional directory — listed in `permissions.additionalDirectories` in the settings file or passed via `claude --add-dir` at launch. | Set when at least one additional directory is registered. |
| `$CLAUDE_DIR` | The Claude Code config directory (typically `~/.claude/`). | Always available. Use in rules for hook scripts and harness files, e.g. `path_spec: "$CLAUDE_DIR/hooks/**"`. |

Use them inside `--path-spec` values. Examples:

- `$HOME/Downloads/**` — any file anywhere under your Downloads folder.
- `$PROJECT_ROOT/build/**` — any file under the current project's `build/` folder.
- `$PROJECT_ROOT/**/.env` — any file named `.env` anywhere inside the project, regardless of subdirectory depth.
- `$CWD` — the exact directory Claude Code was started from.

`**` means "any depth" inside the directory. A single `*` matches one path segment.

## Multi-word subcommands

Some CLIs use a two-word subgroup-action pattern. When writing rules for these tools, the canonical `subcommand` value joins both words with a single space. For example:

- `vault kv get` → `subcommand: "kv get"`
- `doppler secrets get` → `subcommand: "secrets get"`

A rule with `subcommand: "kv get"` matches `vault kv get <anything>`; a rule with just `subcommand: "kv"` does not (the canonical form is the full two-word join).

## Inline absolute path-specs

When a tool call's path falls under an additional directory — one listed in the persistent settings file (`permissions.additionalDirectories`) or passed as a launch-time `--add-dir` flag — nephoscope writes the rule using the real path rather than a placeholder. For example, if `/opt/company/shared` is an additional directory, a rule covering files there will appear as `/opt/company/shared/**` — not `$EXTRA/...` or any other shorthand. These specs are written verbatim into the settings file, so the rule works regardless of which project or session is active.

Mid-session additions typed into the `/permissions` UI (which prints "for this session") are kept in Claude Code's memory only and are not currently visible to nephoscope. To have a runtime-added directory tracked, add it to `settings.local.json` or relaunch with `--add-dir`. See [How it works](how-it-works.md) for the full picture.

## Initialisation flags

`nephoscope-init` accepts the following flags:

| Flag | Purpose |
|---|---|
| `--db-path <path>` | Override the resolved database path. Bypasses `$OBSERVABILITY_DB` and plugin data dir defaults. Useful for tests or alternative locations. |
| `--no-workspace-prompts` | Skip the interactive trusted directories configuration prompt. Use when scripting or in CI where no terminal is available. |

## Slash subcommands

Short summary. Every one of these is invoked as `/nephoscope:permissions <sub>`. For full options and examples, see [`../commands/permissions.md`](../commands/permissions.md).

| Subcommand | What it does |
|---|---|
| `status` (default) | Show the dashboard — rule counts per tier, pending asks, top verbs, suggested next step. |
| `scan` | Group recent asks into candidates. |
| `propose` | List candidates that are ready for review. |
| `review` | Interactive walkthrough — per candidate, pick verb/paths/flags match style and tier. |
| `list [approved\|rejected\|candidates] [--tier ...]` | Query rules or candidates and print a table. |
| `promote` | Turn a shape into an approved rule immediately, without going through the queue. |
| `reject` | Add a rejected rule (hard block). |
| `unpermit` | Delete a rule. Flags must match the original exactly. |
| `seed [--export]` | Load the fixture rule set from YAML, or dump the current rules back to YAML. |
| `prune [--stale-days N]` | Delete old candidates that no longer have pending asks. Default 30 days. |
| `gc [--session-idle-days N] [--ask-pending-hours H]` | Drop session-tier rules from idle sessions and stale pending asks. |
| `sweep` | Run `prune` and `gc` in sequence. |
| `reconcile [--project <path>] [--mode ...]` | Diff the settings file against the database; resolve the difference. |
| `mirror-status` | Print a table of every settings file with its sync status: `stamped`, `null`, or `mismatch`. |
| `mirror-dry-run [--project <path>]` | Render the settings file content to stdout without writing anything. |
| `reload-hint` | Touch the settings file's modification time so Claude Code re-reads it. |

## File layout (plugin package)

The shape of the installed plugin. Most of this is interesting only if you plan to contribute.

```
nephoscope/
  .claude-plugin/
    plugin.json           plugin manifest
    marketplace.json      local marketplace definition
  hooks/
    hooks.json            declares the 4 runtime hooks
    bootstrap.sh          idempotent environment + package install
  commands/
    permissions.md        /nephoscope:permissions slash command doc
  docs/                   the pages you're reading
  src/nephoscope/         the Python package (internals)
  scripts/                dev-time helpers
  tests/                  pytest suites
  pyproject.toml
  LICENSE
```

For a full breakdown of the `src/nephoscope/` tree, see [contributing](contributing.md).

## Data files

Where nephoscope keeps its state and where the settings mirrors land.

| Path | Contents |
|---|---|
| `${CLAUDE_PLUGIN_DATA}/observations.db` | The observations database — asks, candidates, rules. Authoritative. |
| `${CLAUDE_PLUGIN_DATA}/.venv/` | Plugin-private Python environment (created on first session). |
| `${CLAUDE_PLUGIN_DATA}/pyproject.toml.cached` | Cached install manifest — bootstrap skips reinstall if unchanged. |
| `${CLAUDE_PLUGIN_DATA}/disabled` | Opt-out marker (create to silence hooks, delete to re-enable). |
| `${CLAUDE_PLUGIN_DATA}/instincts/` | Instinct summarizer output — plain-text Markdown notes (optional feature). |
| `~/.claude/settings.json` | Global-tier rule mirror. |
| `<project>/.claude/settings.local.json` | Project-tier rule mirror, per project. |

Session-tier rules live only in the database — they are never mirrored to a settings file, because they're dropped at session end.

## Next

- [Contributing](contributing.md) — internal architecture, data flow, tests.

## See also

- [Getting started](getting-started.md) — install and first rule.
- [`../commands/permissions.md`](../commands/permissions.md) — exhaustive flag reference.
