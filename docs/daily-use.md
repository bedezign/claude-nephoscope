# Daily use

Hands-on coverage of the commands you'll run day to day. For the concepts behind these commands, see [how it works](how-it-works.md). For copy-paste-ready command patterns, see [recipes](recipes.md).

## Reading `/nephoscope:permissions status`

`status` is the dashboard. Run it any time to see where things stand. A typical output looks like this:

```
  Rules       global  project  session
    approved      14        2        0
    rejected       3        0        0

  Queue:      7 asks (prompts awaiting a rule)
              2 candidates (recurring patterns for promotion)
  Top asks:   rm ×4, curl ×2, chmod ×1

  Try next:   /nephoscope:permissions scan → propose → review
  Or write a scoped rule directly, e.g. allow rm inside this project:
    /nephoscope:permissions promote --verb rm --flags '*' \
        --path-spec '$PROJECT_ROOT/**' --tier project
```

Walk through it block by block.

### Rules matrix

The 2×3 grid at the top shows how many rules you currently have, broken down by decision (approved or rejected) and tier (global, project, session). In this example you have 14 global allow-rules, 2 project-scoped allow-rules, no session rules, and 3 global deny-rules.

A healthy mature install usually has a small double-digit number of global approved rules for your most-used read-only commands (`ls`, `cat`, `grep`, `git` read operations), a handful of project-tier rules for a few specific projects, and a small number of rejected rules for things you never want done.

### Queue

Two numbers here:

- **Asks** are permission prompts that have already happened but don't yet have a rule covering them. Each ask started life as a click on *Allow* or *Deny*; it sits in the queue until either a rule gets written or the ask is cleaned up.
- **Candidates** are recurring patterns nephoscope has flagged as ready for review. A candidate is born from a handful of related asks — if you've approved `rm` a few times on different paths, a candidate for `rm` will show up here.

If both numbers are zero, you're caught up. `status` will say so and skip the *Try next* block.

### Top asks

The most frequent verbs in the ask queue. If Claude keeps asking about `rm`, `rm` will be on top here. This is your hint about what's worth promoting next.

### Try next

A suggested next step. If there are asks or candidates pending, `status` points you at the scan → propose → review flow, and also shows a concrete `promote` command for the most common verb, so you can copy-paste a starter rule if you'd rather not go through the queue.

## Reviewing candidates

The `scan → propose → review` pipeline turns piled-up asks into actual rules. You'll run it when *Top asks* starts to look repetitive.

### `scan`

```
/nephoscope:permissions scan
```

This looks at recent asks, groups related ones together, and writes candidates into the database. It's fast and idempotent — run it as often as you like. Nothing changes about Claude's behaviour yet; you're just letting the tool notice patterns.

### `propose`

```
/nephoscope:permissions propose
```

Lists candidates that meet the threshold for review — typically, patterns that have shown up enough times that nephoscope thinks they're worth promoting. If the list is empty, there's nothing ripe yet; come back after more activity.

### `review`

```
/nephoscope:permissions review
```

The interactive part. For each candidate you'll see:

1. **Which verb.** E.g. "`rm` has shown up 5 times in the last day."
2. **Paths: literal or generalize?** If the asks were on `build/a.o`, `build/b.o`, `build/c.o`, nephoscope may offer a choice: narrow (match only that exact path), or generalize to a placeholder-based pattern like `$PROJECT_ROOT/build/**`.
3. **Flags: literal or wildcard?** Same idea for the options passed to the command. Match exactly the flags you've seen, or allow any flags with `*`.
4. **Tier.** Session, project, or global. The walkthrough explains what each means inline.

At the end of each candidate you either approve it (becomes an allow-rule), reject it (becomes a deny-rule), or skip it (nephoscope asks again next time). Approved and rejected rules are written to the database, and the matching settings file is updated immediately so Claude Code picks them up right away.

If you need to bail out mid-review, hit Ctrl-C. Your already-answered candidates stay as rules; the unanswered ones remain candidates for next time.

**Session scope (in-session default).** When you run `review` from inside a Claude Code session, the candidate list defaults to candidates first observed in *that* session — the header line `Scoped to session <short-uuid> — N candidates (M total in DB)` makes it visible. Add `--session=all` to see the full global view, or `--session=<uuid>` for a specific past session. Outside a Claude Code session (cron, CI, manual shell with no `CLAUDE_CODE_SESSION_ID`), behaviour is unchanged — you see all candidates.

## Loading profiles

Use a profile when you're starting fresh, or when you're about to dive into a stack you haven't worked in yet and would rather pre-approve the obvious commands than wait for the ask queue to build up.

Browse what's available:

```
/nephoscope:permissions profiles list
```

This prints each profile's id and a one-line description.

Apply one:

```
/nephoscope:permissions profiles load python-dev
```

You'll see a summary of what the profile contains and a `[Y/n]` confirm before any rules are inserted. The summary is the full preview — it lists every rule that would be added. Answer `n` to cancel cleanly without any changes.

Multiple profiles can be loaded in one command:

```
/nephoscope:permissions profiles load dev-tools python-dev
```

Profiles are idempotent — loading the same one twice is safe and inserts nothing the second time. They also never overwrite or remove rules you've already written; loading a profile only adds.

The `credential-file-tools` profile is worth calling out specifically — it adds file-tool deny rules (Read, Write, Edit, and MultiEdit blocked on credential paths like `.env*`, `*.pem`, `~/.aws/**`, `~/.ssh/**`), which is the complement to the Bash-level credential blocking and output-scanner redaction that are already active by default.

## Writing a rule by hand

Worked example: you keep getting asked about `rm` while working inside one specific project. You don't want to allow `rm` everywhere — that's too broad — but you do trust yourself to remove files inside this project's tree.

The command:

```
/nephoscope:permissions promote --verb rm --flags '*' --path-spec '$PROJECT_ROOT/**' --tier project
```

Piece by piece:

- **`--verb rm`** — this rule is about the `rm` command.
- **`--flags '*'`** — match `rm` with *any* options. `rm file`, `rm -rf folder`, `rm -i thing` — all of them. Without this, the rule would match only `rm` with no options at all.
- **`--path-spec '$PROJECT_ROOT/**'`** — only match when the path being removed is somewhere inside the current project. The `**` part means "any depth", so subfolders are covered too. If you ran `rm` against a file in your home directory, this rule would *not* match and you'd still get a prompt.
- **`--tier project`** — store the rule as a project rule, not a global one. It will apply automatically the next time you open Claude Code in this project, and not in any other project.

After you run that command, two things happen:

1. The rule is saved in nephoscope's database.
2. The rule is written into the project's `.claude/settings.local.json` file (or your `~/.claude/settings.json` for global rules), so Claude Code's built-in permission gate can use it.

From the next prompt onward, `rm` inside the project just works — no popup.

## Rule usage stats

```bash
nephoscope-permissions stats
nephoscope-permissions stats --show-unused
```

Prints a summary of how often each permission rule has fired: total approved/rejected counts, total hits, top 10 rules by hit count, and the most recently matched rule. Pass `--show-unused` to list every rule with zero hits — useful for pruning stale entries.

## Routine maintenance

Nephoscope accumulates ask records, candidates, and session-tier rules over time. Running a periodic sweep keeps the database lean and the queue manageable.

The easiest way is to run everything in one step:

```
/nephoscope:permissions sweep
```

`sweep` runs two steps in sequence:

- **`prune`** — removes candidates that are no longer backed by pending asks. These are patterns nephoscope noticed but that have since gone quiet. Default cutoff: 30 days. Pass `--stale-days N` to change it.
- **`gc`** — removes session-tier rules from idle sessions and clears stale pending asks. Pass `--session-idle-days N` (default: 7) and `--ask-pending-hours H` (default: 72) to adjust the cutoffs.

Neither step touches your approved or rejected rules — only candidates, session rules, and stale ask records are affected.

Running `sweep` once a month is plenty for most users. Run it sooner if `/nephoscope:permissions status` shows a large ask or candidate queue that isn't shrinking on its own.

## Undoing a rule

If you promoted something by mistake, or you changed your mind:

```
/nephoscope:permissions unpermit --verb rm --flags '*' --tier project
```

The flags must match the rule you originally wrote. If you only want to remove the rule for this session and keep the global one, pass `--tier session`. If you want to remove the global one and keep a project-specific one, pass `--tier global`. The match is exact — only the rule at the named tier is touched.

Not sure what's in place? List first:

```
/nephoscope:permissions list
```

That prints every rule as a table. Copy the exact verb, flags, and tier from the row you want to remove and paste them into the `unpermit` command.

## Troubleshooting

### Claude still asks about X even though I approved it

Most likely explanations, in order:

1. **You promoted at the session tier and started a new chat.** Session-tier rules live only for the current Claude Code conversation. When the chat ends, the rule is gone. Run `/nephoscope:permissions status` — if the rule you expected is missing, re-promote at `--tier project` or `--tier global` so it sticks.
2. **The flags don't match.** A rule for `rm` with `--flags '[]'` (no options) won't match `rm -rf`. Use `--flags '*'` if you want any options allowed, or list exactly the flags you use. Re-run `/nephoscope:permissions list` to see what's actually stored.
3. **The path-spec doesn't cover the path.** `$PROJECT_ROOT/**` matches anywhere in the current project but nothing outside it. If you're running the command from a different working directory, check what `$PROJECT_ROOT` resolves to.
4. **The subcommand isn't in canonical form.** Commands like `vault kv get` are stored with subcommand `kv get` — both words joined by one space. A rule written with `--subcommand kv` alone won't match. Run `/nephoscope:permissions list` to see the exact subcommand value stored; it must match character-for-character. See [Multi-word subcommands](reference.md#multi-word-subcommands).
5. **The settings file drifted.** If something edited your `~/.claude/settings.json` or `.claude/settings.local.json` outside of nephoscope — another tool, a manual edit, or Claude Code's own "allow always" option — the database and settings file can fall out of sync. The rule exists in the database but is absent from (or overridden in) the file Claude Code reads. Run `/nephoscope:permissions reconcile` to detect and fix the difference. See [mirror-status shows a mismatch](#mirror-status-shows-a-mismatch) for details.

### `mirror-status` shows a mismatch

`mirror-status` compares the permission rules nephoscope wrote into your settings file against what's actually there. A mismatch means the `permissions.allow`/`deny`/`ask` arrays were changed by something other than nephoscope — usually a hand-edit or another tool. Edits to other parts of `settings.json` (hooks, env, model, anything outside those three arrays) are ignored on purpose, so unrelated edits never trigger a mismatch.

When trusted directories are configured, `mirror-status` also prints a "Workspace coverage" section listing each trusted directory with a `✓` if its allowed-tools entries are present in the global settings file, or `✗` if not. A hint message suggests running `reconcile` when any entries are missing.

Run:

```
/nephoscope:permissions reconcile
```

This shows you the difference and asks which side should win: the database, the file, or per-entry (a walkthrough that lets you pick for each differing rule). For a first reconcile on a file nephoscope has never touched before, the default is to adopt what's already there (so existing rules land in the database cleanly).

### I see 0 rules but I've approved lots of things via Claude Code's UI

Two separate things are going on here.

First: Claude Code has its own "allow always" option in the permission dialog, and that writes straight to `~/.claude/settings.json`. Those entries are real, persistent rules — they work — but they bypass nephoscope. On the next run of `/nephoscope:permissions reconcile`, nephoscope notices those entries and adopts them into its own database, after which they show up in `status` counts.

Second: approving a Claude Code prompt through the normal (non-"always") path doesn't create a rule at all. It only records the ask in nephoscope's database. To turn those approvals into real rules, run the learner pipeline:

```
/nephoscope:permissions scan
/nephoscope:permissions propose
/nephoscope:permissions review
```

Or, if you already know what you want, skip the queue and use `promote` directly as shown above.

### How do I wipe everything and start over

Delete the database file. That erases every ask, every candidate, and every rule nephoscope has learned:

```bash
rm "${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data/nephoscope-bedezign}/observations.db"
```

The next time a tool call fires, the recorder lazily recreates an empty database.

One caveat: deleting the database does not clean up the rule entries nephoscope already mirrored into your Claude Code settings files. Either remove those entries by hand for a fully fresh state, or run `/nephoscope:permissions reconcile` afterwards and pick *json-wins* to re-import them into the new database.

## Next

- [Recipes](recipes.md) — starter patterns for common situations.
- [Reference](reference.md) — environment variables, placeholders, subcommand table.

## See also

- [How it works](how-it-works.md) — the concepts behind the dashboard and the pipeline.
- [`../commands/permissions.md`](../commands/permissions.md) — every flag, every subcommand.
