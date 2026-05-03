<p align="center">
  <img src="docs/images/logo.svg" alt="nephoscope" width="180">
</p>
<p align="center">
  <img src="docs/images/claude-mark.svg" alt="Claude Code" width="16">&nbsp;Built for <strong>Claude Code</strong>
</p>

# nephoscope

*The diff between stuck and done.*

*Stop Claude Code asking about the same commands twice.*

Claude Code asks for permission before every shell command, file write, or web fetch. That's good for safety — until you've answered the same prompt twenty times in a row. Nephoscope watches your answers, turns the recurring ones into persistent rules, and writes those rules straight into your Claude Code settings. The yellow popups quietly disappear. Everything happens locally.

## What it does

- **Learns from your answers.** Every *Allow* or *Deny* you click is recorded, and recurring patterns surface as rules you can promote with one command.
- **Ships with ready-to-use rule sets.** Five bundled profiles cover common stacks — Python, JavaScript, DevOps, developer tools, and project-level file access. Load one in a single command and skip the learning period entirely.
- **Scopes rules the way you work.** Allow a tool everywhere, or only inside one project, or just for this one chat — your choice, per rule.
- **Stays out of the way.** Rules are written into your normal `settings.json`, so Claude Code's built-in permission gate handles them without any hook round-trip.
- **Ships with credential-leak guards.** All deny rules go into `settings.json` and are enforced by Claude Code's permission gate — no advisory `CLAUDE.md` text, no model goodwill required. Two protections are active from day one: Bash commands are blocked from reading credential files (`.env*`, `*.pem`, `*.key`, `~/.aws/**`, `~/.ssh/**`, `~/.npmrc`, `~/.netrc`, bash and zsh history, common secrets directories, and more), and a PostToolUse output scanner redacts known API key patterns (Anthropic, Stripe, GitHub, AWS, Slack, SendGrid, JWTs, private key blocks) from Bash, Grep, and Read tool output before it reaches the model. Standalone secret-manager reads (`op read`, `vault kv get`, and others) are also blocked; the safe inline form — `$(op read 'op://...')` — is unaffected.
- **File-tool protection via the `credential-file-tools` profile.** Claude Code's native Read, Write, Edit, and MultiEdit tools are denied against the same credential paths once you load the profile (`/nephoscope:permissions profiles load credential-file-tools`). Together with the two Bash and output-scanner defaults above, this closes the three ways secrets leak from Claude Code sessions: direct file reads, runtime command output that contains credentials, and grep matches that hit config files.

## Why nephoscope

Nephoscope works the same way regardless of which Claude Code tier you're on (Pro, Max, Team, Enterprise) or which model you're using (Opus, Sonnet, Haiku) — the rules live in your local settings and Claude Code's built-in permission gate enforces them. It's a middle ground between the rough edges of other approaches: unlike bypass mode, it doesn't disable credential-leak guards; unlike the default "ask every time", it learns from your own answers and respects your project and session boundaries. The rules are yours to shape, and they travel with your settings.

## Install

From inside a Claude Code session:

```
/plugin marketplace add bedezign/claude-and-me
/plugin install nephoscope@bedezign
```

The first new session auto-installs a small Python environment under the plugin's data directory. No config files to edit, no path setup, no SQL migrations to run.

## In a hurry?

Three commands cover the most common day-one moves — check what's been observed, promote a rule, and walk through pending suggestions:

```
/nephoscope:permissions status
```
Prints a snapshot of recorded answers and which patterns are eligible for promotion.

```
/nephoscope:permissions promote --verb ls --flags '*' --tier global
```
Writes an *Allow* rule for `ls` (with any flags) into your global `settings.json`.

```
/nephoscope:permissions review
```
Walks you through every pending suggestion interactively, one at a time.

## Documentation

Read in order — each page builds on the last:

1. [Getting started](docs/getting-started.md) — install, verify, your first rule
2. [How it works](docs/how-it-works.md) — concepts and flow, in plain language
3. [Daily use](docs/daily-use.md) — reading status, reviewing, troubleshooting
4. [Recipes](docs/recipes.md) — copy-pasteable rule patterns for common situations
5. [Reference](docs/reference.md) — environment variables, placeholders, subcommand table
6. [Contributing](docs/contributing.md) — architecture, data flow, tests

Supplementary:

- [Credential-leak coverage](docs/credential-leak-coverage.md) — what's blocked by default and why

## Part of

nephoscope ships in **[Claude & Me](https://github.com/bedezign/claude-and-me)** — the bedezign Claude Code plugin marketplace.

## License

Apache-2.0. See [LICENSE](LICENSE).
