# nephoscope documentation

These pages walk you from install to daily use in order. Start at the top and work down — each page links forward to the next one. If you already know what nephoscope does and just want a command, jump to [recipes](recipes.md) or [reference](reference.md).

- **[Getting started](getting-started.md)** — prerequisites, the three install paths, first-run bootstrap, how to verify the install, opting out, uninstalling, and writing your first rule.
- **[How it works](how-it-works.md)** — what nephoscope actually does for you, the key concepts (tool call, ask, candidate, rule, tier, placeholders), the end-to-end flow, and where the data lives.
- **[Daily use](daily-use.md)** — reading the `status` dashboard line by line, the `scan → propose → review` loop in practice, writing a rule by hand, undoing a rule, and troubleshooting common issues.
- **[Recipes](recipes.md)** — copy-pasteable commands for eight common situations, each with a problem statement and a short explanation.
- **[Reference](reference.md)** — environment variables, placeholder table, every `/nephoscope:permissions` subcommand in one table, plugin file layout, and data file locations.
- **[Contributing](contributing.md)** — architecture, data flow, mirror pipeline, reconcile engine, review CLI, fixture round-trip, and the test suites. Read this if you want to change the plugin, not just use it.

See also the exhaustive subcommand documentation in [`../commands/permissions.md`](../commands/permissions.md) — every flag, every option.
