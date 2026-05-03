All notable changes to this project are documented here. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] — 2026-05-03

First publicly released version.

### Added

- Config-backed database path — `database` key in the TOML config file overrides `$OBSERVABILITY_DB` and the plugin data dir default. Useful for custom install locations or shared databases.
- Comprehensive user documentation — getting started, how it works, daily use, recipes, reference, contributing, CHANGELOG.
- `git` profile bundled (7 total bundled profiles: `credential-file-tools`, `dev-tools`, `git`, `javascript`, `devops`, `project-dev`, `python-dev`).

### Changed

- `uv.lock` removed from repository; added to `.gitignore`.

## [0.3.0] — 2026-05-02

### Added

- `nephoscope` top-level CLI dispatcher — primary terminal interface, replacing the fragmented set of console scripts. Subcommands: `init`, `stats`, `status`, `reconcile`, `mirror-status`, `mirror-dry-run`, `reload-hint`, `profiles`, `migrate`.
- `nephoscope-output-scanner` PostToolUse hook — scans Bash, Grep, and Read tool output and redacts recognised credential patterns before they reach the model. Recognised patterns: Anthropic keys, Stripe keys, GitHub tokens, AWS access key IDs, Slack tokens, SendGrid keys, JWTs, PEM private key blocks.

### Changed

- Legacy console scripts (`nephoscope-permissions`, `nephoscope-init`, `nephoscope-profiles`, `nephoscope-migrate`, `nephoscope-review`, `nephoscope-observer`) retained as backward-compatible shims; the dispatcher is the recommended entry point.

## [0.2.1] — 2026-05-02

### Added

- `credential-file-tools` meta-profile — deny rules for Claude Code's file tools (Read, Write, Edit, MultiEdit) against credential paths. Complements the default Bash-level protection.
- `verb_types` entries in meta-profiles — profiles can now configure canonicalization behaviour (task runners, content verbs, script runners), not just permission rules.
- `sqlite3` added to the `dev-tools` profile.

### Changed

- Meta-profiles system replaces the optional fixture files mechanism.

## [0.2.0] — 2026-04-30

### Added

- `install.py` — stdlib-only standalone installer; no external tools required. Creates a venv, installs the package, runs `init`.
- Opt-in permission profiles — `nephoscope-init` prompts for trusted directories and offers numbered profile selection on first run.
- Five initial bundled profiles: `dev-tools`, `python-dev`, `javascript`, `devops`, `project-dev`.

## [0.1.2] — 2026-04-30

### Added

- `$CLAUDE_DIR` path placeholder — resolves to `~/.claude/` at match time. Use in rules covering hook scripts and harness files.
- Session-ask audit trail — permission asks are recorded with full context for candidate generation.
- `trusted_dirs` config key — pre-approves file access under configured directories. All trusted-dir matches go through the DB tier; no unconditional bypass.

## [0.1.1] — 2026-04-27

### Added

- LICENSE and contribution documentation (open-source hygiene pass).
- Credential-leak seed rules — initial deny rules for `.env*`, key files, and AWS / SSH / secret-manager credential paths.
- Per-session `--add-dir` capture — extra directories passed at `claude` launch are included in rule scope.

### Changed

- Review and observer CLIs ported from Bash to Python console scripts.

### Fixed

- Namespace collision with Claude Code's built-in `/permissions` — the plugin command is now `/nephoscope:permissions`.

## [0.1.0] — 2026-04-23

### Added

- Session recorder hook — captures pre/post tool calls and session-start events into SQLite.
- Permission learner — accumulates asks, generates candidates, drives the interactive review flow.
- Settings mirror — writes approved/rejected rules from the DB into `~/.claude/settings.json` and project `.claude/settings.local.json`.
- Bash command canonicalization — verb, subcommand, sorted flags, path.
