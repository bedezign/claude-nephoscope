# Getting started

This page takes you from a fresh machine to a running install with your first rule in place. If you've already installed nephoscope and want the concepts, skip to [how it works](how-it-works.md).

## Prerequisites

- **Claude Code**, recent release — plugin support is required.
- **Python 3.11 or newer**, available on your `PATH`. Check with `python3 --version`.
- **SQLite CLI** (`sqlite3`), used by the `/nephoscope:permissions` slash command for ad-hoc queries.

## Install — three modes

Pick one. The GitHub marketplace path is the primary option; the other two are for contributors and one-off testing.

### From GitHub marketplace (recommended)

No clone required. From a Claude Code session:

```
/plugin marketplace add bedezign/nephoscope
/plugin install nephoscope@bedezign
```

Updates ship on version bumps — `/plugin update nephoscope@bedezign` pulls the latest published version.

Pin to a specific tag or branch with `bedezign/nephoscope@<tag-or-branch>`.

### From a local clone (contributors)

Clone the repository and register it as a local marketplace:

```bash
git clone https://github.com/bedezign/nephoscope.git
cd nephoscope
```

From a Claude Code session:

```
/plugin marketplace add /absolute/path/to/nephoscope
/plugin install nephoscope@bedezign
```

Edits to source do not automatically flow through to the running hooks. To promote changes, bump the `version` field in `.claude-plugin/plugin.json`, then run `/plugin update nephoscope@bedezign`.

### Per-invocation (`--plugin-dir`)

For one-off testing without registering the plugin at all:

```bash
git clone https://github.com/bedezign/nephoscope.git
claude --plugin-dir /absolute/path/to/nephoscope
```

### Official marketplace (pending)

```
/plugin install nephoscope@claude-plugins-official
```

Marketplace submission is pending; the GitHub-hosted path above is the current primary option.

### Standalone (no Claude Code yet)

`install.py` at the repo root is a stdlib-only first-run installer. It creates a venv, installs nephoscope via pip, and runs `nephoscope-init` — no UV or external tools required.

```bash
python3 install.py              # install from PyPI (post-publish)
python3 install.py --source .  # install from local repo clone
```

Use this when bootstrapping fresh environments where Claude Code isn't yet set up.

## Upgrading

Pull the latest published version from a Claude Code session:

```
/plugin update nephoscope@bedezign
```

**Schema migrations.** When a version bump includes database changes, apply them after the update:

```bash
nephoscope migrate
```

`migrate` is safe to run at any time — it applies only pending migrations and does nothing if the schema is already current.

**Seeded rules after an upgrade.** Profiles and credential-leak rules that nephoscope seeded into your database stay as-is across upgrades. Rules you removed stay removed; new versions never overwrite your existing rules. New versions may ship additional credential-leak patterns or new profiles — check the [CHANGELOG](../CHANGELOG.md) and [credential-leak coverage](credential-leak-coverage.md) after upgrading to see what's new.

## First-run bootstrap

The first time Claude Code loads the plugin, the `SessionStart` hook runs `hooks/bootstrap.sh`, which:

1. Creates a private Python environment at `${CLAUDE_PLUGIN_DATA}/.venv`.
2. Installs the plugin's Python package into that environment.
3. Caches the manifest at `${CLAUDE_PLUGIN_DATA}/pyproject.toml.cached`, so subsequent sessions skip the install step unless the manifest changed or the venv's entry-point binary is missing.

You may notice a short delay on the first session while the environment is built. Later sessions start instantly.

The observations database is created lazily on first tool call. You can also force-create it yourself:

```bash
"${CLAUDE_PLUGIN_DATA}/.venv/bin/nephoscope-init"
```

Pass `--db-path /some/other/path.db` to materialise the database at a non-default location (useful for tests or parallel consumers).

Use `--no-workspace-prompts` to suppress interactive prompts (trusted directories and optional permission profiles) explicitly. This is useful when bootstrapping from a script or CI, where no terminal is available.

### Workspace-roots configuration

After DB init, `nephoscope-init` may prompt for **trusted directories** — top-level project directories you want pre-approved for all file access. The prompt only runs when stdin is a TTY, so it does not fire when bootstrap runs from Claude Code, in a pipe, or in CI. You can suppress it explicitly using `--no-workspace-prompts`.

If you enter paths at the prompt, each is canonicalized (tilde-expanded and realpath-resolved) and written to `~/.config/nephoscope/config.toml` under the `trusted_dirs` key. Paths you add become eligible for the `$TRUSTED_DIR` placeholder in rules. Pressing Enter on a blank line ends the prompt; nothing is written if no paths are entered.

#### Jump-starting with a profile

Profiles are bundles of pre-tested rules for common tools — load one and you get a useful starting point without waiting for nephoscope to learn your patterns. Browse what ships with the plugin, then load whichever match your stack:

```
/nephoscope:permissions profiles list
/nephoscope:permissions profiles load python-dev
```

Profiles that ship out of the box:

- `credential-file-tools` — Deny Claude Code's file tools (Read, Write, Edit, MultiEdit) against credential paths
- `dev-tools` — curl, wget, make, openssl, touch, man
- `python-dev` — uv, ruff, pytest, pyright, mypy, pip (read-only subcommands)
- `javascript` — node, npx, deno (scoped to trusted dirs), npm/yarn/pnpm subcommands, tsc, eslint, vite, vitest
- `git` — Git version control — approve common read-only commands
- `devops` — kubectl, helm, docker, terraform, ansible (read/inspect subcommands)
- `project-dev` — full file read/write/edit access within trusted project directories, plus python3/python/bash script execution

`credential-file-tools` is the profile that covers the file-tool layer of the credential-leak protection — loading it adds Read, Write, Edit, and MultiEdit deny rules for credential paths on top of the Bash-level blocking and output-scanner redaction that ship active by default.

`nephoscope-init` also offers these interactively when stdin is a TTY, but you can load any profile at any time with the slash command above.

See [Configuration file](#configuration-file) in [Reference](reference.md) for environment variables and config options, and [Placeholders](how-it-works.md#placeholders) for how `$TRUSTED_DIR` works.

### Adding trusted directories later

To add a trusted directory after the initial bootstrap, edit the config file directly:

```toml
# ~/.config/nephoscope/config.toml
trusted_dirs = ["/home/you/code/myproject", "/home/you/work/client"]
```

The change takes effect when nephoscope next reconciles its mirror — either automatically on the next session start, or immediately via:

```
/nephoscope:permissions reconcile
```

Alternatively, set `auto_register_project_paths = true` in the config to have nephoscope silently add each new working directory to `trusted_dirs` on first visit.

## Verify install

After a fresh session has fired at least one tool call, ask SQLite to count the observations:

```bash
DB="${OBSERVABILITY_DB:-${CLAUDE_PLUGIN_DATA}/observations.db}"
sqlite3 "$DB" 'SELECT COUNT(*) FROM tool_calls;'
```

A non-zero number means the recorder is running and tool calls are landing in the database.

## Opt out temporarily

To silence all nephoscope hooks without uninstalling:

```bash
touch "${NEPHOSCOPE_DISABLE_MARKER:-${CLAUDE_PLUGIN_DATA}/disabled}"
```

While this marker file exists, every nephoscope hook exits immediately with no effect. Remove the file to re-enable:

```bash
rm "${NEPHOSCOPE_DISABLE_MARKER:-${CLAUDE_PLUGIN_DATA}/disabled}"
```

## Uninstall

For marketplace or local-marketplace installs:

```
/plugin uninstall nephoscope@bedezign
```

For the official marketplace (when available):

```
/plugin uninstall nephoscope@claude-plugins-official
```

For `--plugin-dir` installs, just stop passing the flag (or remove the alias) the next time you launch.

Optional — drop the plugin's data directory too, including the learned rules database:

```bash
rm -rf "${CLAUDE_PLUGIN_DATA}"
```

Note that rules already written into your Claude Code settings files (`~/.claude/settings.json` and per-project `.claude/settings.local.json`) are not removed by uninstalling the plugin. Delete those entries by hand if you want a fully clean slate.

## Your first rule

A two-minute exercise. Open a Claude Code session and run:

```
/nephoscope:permissions status
```

You'll see a small dashboard — a rule count matrix, a queue of pending asks, and the most frequent verbs. If you've been using Claude Code for a while, the *Top asks* line probably has a verb or two that keeps coming back. Let's allow `ls` everywhere:

```
/nephoscope:permissions promote --verb ls --flags '*' --tier global
```

Verify the rule landed:

```
/nephoscope:permissions status
```

The *approved / global* cell should show one more than before. From now on, Claude Code won't ask about `ls` again.

**Terminal use.** The `nephoscope` command is also available at the terminal for scripting or when you're working outside a Claude Code session: `nephoscope stats`, `nephoscope reconcile`, `nephoscope mirror-status`, and others. See [Reference](reference.md) for the full list.

## Next

- Read [how it works](how-it-works.md) to understand what the dashboard, the rules, and the scopes actually mean.
- Skim [recipes](recipes.md) for starter patterns that cover the most common situations.
