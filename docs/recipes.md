# nephoscope recipes

Copy-paste commands for common situations. Each recipe shows the command to run from inside a Claude Code session as `/nephoscope:permissions ...`. If you're at a terminal outside a session, the same commands work via the venv binary, e.g.:

```bash
"${CLAUDE_PLUGIN_DATA}/.venv/bin/nephoscope-learn" promote --verb ls --flags '*' --tier global
```

For concept background, see [how it works](how-it-works.md). For hands-on walkthroughs of the dashboard and the review pipeline, see [daily use](daily-use.md). For every flag and every subcommand, see [`../commands/permissions.md`](../commands/permissions.md).

---

## Jump-start with a profile

**Problem.** Starting from a blank install, Claude will ask about every command. If you already know which tools you use, you'd rather pre-approve the obvious ones than wait for the queue to build up.

```
/nephoscope:permissions profiles list
```

Then load whichever match your stack:

```
/nephoscope:permissions profiles load dev-tools python-dev
```

**What this does.** `profiles list` shows every bundled profile with its id and a short description. `profiles load` inserts the selected profiles' rules idempotently and syncs the mirror so Claude Code picks them up immediately. A `[Y/n]` prompt shows a summary of what will be added before anything is written. Combine multiple profile ids in one command — they're applied in order and any rule already present is skipped. `project-dev` is the broadest profile (full file access inside trusted directories, plus `python3`/`python`/`bash` script execution) and should only be loaded once you've configured `trusted_dirs`.

---

## Allow `ls` and `cat` everywhere

**Problem.** Every time Claude wants to list a directory or peek at a file, you get a popup. Both commands are read-only and you trust them anywhere.

```
/nephoscope:permissions promote --verb ls --flags '*' --tier global
/nephoscope:permissions promote --verb cat --flags '*' --tier global
```

**What this does.** Adds two global allow-rules. `--flags '*'` matches any options, so `ls -la`, `cat -n file`, and so on are all covered. Future prompts for either command are auto-approved.

---

## Allow any `rm` inside the current project

**Problem.** You delete files all day inside one project and don't want to confirm each one. Outside this project, you'd rather still be asked.

```
/nephoscope:permissions promote --verb rm --flags '*' --path-spec '$PROJECT_ROOT/**' --tier project
```

**What this does.** Creates a project-scoped rule for `rm` with any options, restricted to paths inside the project's tree. The `$PROJECT_ROOT` placeholder gets resolved to the current project each time, so the rule travels with the project rather than a fixed path.

---

## Allow a specific `curl` target globally

**Problem.** A particular CLI tool fetches a config from a known URL with `curl` and you'd rather skip the prompt every time, but you don't want to allow `curl` against arbitrary URLs.

```
/nephoscope:permissions promote --verb curl --subcommand 'https://api.example.com/config.json' --flags '[]' --tier global
```

**What this does.** Pins the rule to one exact first argument. `curl` against any other URL still asks. `--flags '[]'` means "no extra options" — if your real call uses something like `-sS`, switch to `--flags '*'` (or list the exact flags) so the rule matches.

---

## Allow a two-word-subcommand tool

**Problem.** You use a CLI where the subcommand is two words — `vault kv get`, `kubectl get pods`, `terraform state list`. A rule with just `--subcommand kv` won't match; you need the canonical two-word join.

```
/nephoscope:permissions promote --verb vault --subcommand 'kv get' --flags '*' --tier global
```

**What this does.** Creates an allow-rule for `vault kv get` with any flags. The value `'kv get'` is the canonical form — both words joined by a single space. Other `vault` subcommands (`vault kv put`, `vault login`) are not covered and need their own rules.

Some real-world examples:

| Command | `--verb` | `--subcommand` |
|---|---|---|
| `vault kv get` | `vault` | `'kv get'` |
| `doppler secrets get` | `doppler` | `'secrets get'` |
| `kubectl get pods` | `kubectl` | `'get pods'` |
| `terraform state list` | `terraform` | `'state list'` |

See [Multi-word subcommands](reference.md#multi-word-subcommands) for the full canonical-form rule.

---

## Reject all `chmod` on your home directory

**Problem.** You never want Claude to change file permissions in your home tree, full stop.

```
/nephoscope:permissions reject --verb chmod --flags '*' --path-spec '$HOME/**' --tier global
```

**What this does.** Adds a global deny rule. Any `chmod` against a path under your home directory is hard-blocked — Claude won't even ask you. To narrow the block to a sub-folder, change `$HOME/**` to e.g. `$HOME/.ssh/**`.

---

## Allow a custom script from your PATH

**Problem.** You have a personal helper at `~/.local/bin/deploy.sh` that Claude calls often, and you'd like it to run without prompting.

```
/nephoscope:permissions promote --verb '$HOME/.local/bin/deploy.sh' --flags '*' --tier global
```

**What this does.** Uses the absolute path to the script as the verb, with the `$HOME` placeholder so the rule works on any machine where the script lives in the same relative spot. Any options passed to the script are allowed. If the script lives inside a project, swap in `$PROJECT_ROOT/scripts/deploy.sh` and use `--tier project` instead.

---

## Allow one rule for this session only

**Problem.** You're trying out a new tool and want to grant it permission for the rest of this chat, then have the rule vanish on its own.

```
/nephoscope:permissions promote --verb my-experiment --flags '*' --tier session
```

**What this does.** Adds a session-tier allow-rule. The rule applies for the rest of the current Claude Code chat and is dropped when the chat ends — no cleanup required, no leftover entries in your settings file.

---

## Approve everything for a single project without touching global

**Problem.** A specific project has its own build/test/format toolchain you fully trust. You want broad permissions inside that project, while keeping global rules conservative.

```
/nephoscope:permissions promote --verb npm --flags '*' --path-spec '$PROJECT_ROOT/**' --tier project
/nephoscope:permissions promote --verb pytest --flags '*' --path-spec '$PROJECT_ROOT/**' --tier project
/nephoscope:permissions promote --verb make --flags '*' --path-spec '$PROJECT_ROOT/**' --tier project
```

**What this does.** Repeat for each command you want auto-approved in this project. All rules are stored at the project tier, so they only kick in inside this codebase. To find which commands you actually need to add, run `/nephoscope:permissions status` and look at the *Top asks* line while working inside the project.

---

## Revert a rule I added by mistake

**Problem.** You promoted a rule too broadly and want it gone.

```
/nephoscope:permissions unpermit --verb rm --flags '*' --tier global
```

**What this does.** Deletes the matching rule. The flags must match exactly what you originally promoted (verb, optional subcommand, flags, tier). If you're not sure what's in place, run `/nephoscope:permissions list` first to see every rule and copy the values from there.

---

## Start fresh — clear everything

**Problem.** Things have gotten messy and you'd rather wipe the slate and start over.

```bash
rm "${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data/nephoscope-bedezign}/observations.db"
```

**What this does.** Deletes the entire nephoscope database — every ask, every candidate, every rule. The next time you run a tool call, the recorder lazily recreates an empty database. Note that this does not clean the rule entries already mirrored into your Claude Code settings files; remove those by hand if you want a totally clean state, then either start fresh or run `/nephoscope:permissions reconcile` and pick *json-wins* to re-import them into the new database.

## Next

- [Reference](reference.md) — placeholder table, environment variables, subcommand overview.

## See also

- [How it works](how-it-works.md) — the vocabulary behind these commands.
- [Daily use](daily-use.md) — when to use which recipe.
- [`../commands/permissions.md`](../commands/permissions.md) — full flag reference.
