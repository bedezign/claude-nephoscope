# Observability — Future Ideas Backlog

Speculative items captured during design discussions. Not commitments. Each entry: brief context + the trigger that would make it worth doing.

## Permission popup detection (yes vs yes-to-all)

**What:** Distinguish a one-shot user "yes" from "yes, always" on permission prompts.

**Why hard:** PostToolUse fires identically for both. The only signal is the side effect — `permissions.allow` in `settings.json` gets a new entry on yes-to-all.

**Possible approaches:**
- Snapshot `settings.json` before PostToolUse, diff after, attribute the new allow entry to the just-completed tool_use_id.
- Wait for richer hook payload data — Claude Code may include something like `permission_decision_source` in PostToolUse for prompt-resolved calls. We'd see it once we have real `payload_json` from a session that triggered prompts.

**Trigger to act:** Inspect a real `tool_extras.payload` for a permission-prompted call once Phase 4 is live. If the payload reveals nothing useful, fall back to the settings-diff approach.

## Resolved-model attribution for Agent calls

**What:** When the Agent/Task tool is invoked without an explicit `model:` field, capture which model actually ran (from agent .md frontmatter or session inheritance).

**Why deferred:** Resolving the default at recorder time means reading `~/.claude/agents/<subagent_type>.md` per call — too expensive on the hot path.

**Approach:** Lazy backfill consumer (similar to permission learner). Reads Agent rows, parses frontmatter once per `subagent_type`, caches the default, writes a `tool_extras` entry with `name='resolved_model'`.

**Trigger to act:** When you want to query "which model is doing what work" or attribute cost.

## Compression of tool_extras

**What:** gzip the `value` column in `tool_extras` to shrink storage.

**Why deferred:** Trades SQL inspectability and grep-ability for ~80% storage savings. The sidecar pattern already enables surgical `DELETE` for space reclamation; that's usually enough.

**Trigger to act:** Disk pressure becomes real. Implement as one-shot maintenance pass (read row → compress → write back → set `compressed=1` flag). Sidecar makes this mechanical to retrofit.

## Migrate historical data from tools.db

**What:** Pull the old continuous-learning-v2 `~/.cache/claude/observations/tools.db` into the new `~/.cache/claude/observability/observations.db`.

**Why deferred:** New DB is already accumulating fresh data. Historical may not be worth importing.

**Trigger to act:** Specific need to query across both eras (e.g. "show me my tool usage patterns over the last 6 months" before the new DB has 6 months of history).

## "Find all rm -rf actions" query convenience

**What:** A command/skill that wraps the M2M (`tool_call_shapes`) into ergonomic queries: "show me every tool call that included shape X", "which sessions touched verb Y", "diff command shape distribution between two date ranges".

**Why deferred:** Foundation is in place (the M2M junction). Building the query surface only makes sense once you actually want these views.

**Trigger to act:** First time you find yourself writing the JOIN by hand.

## Settings.json change tracking

**What:** Hook to capture changes to `~/.claude/settings.json` (when, what changed, who/what triggered it). Useful for debugging why a permission was added, why a hook stopped firing, etc.

**Why deferred:** No SettingsChange hook exists; would need either a file watcher or a periodic snapshot+diff cron.

**Trigger to act:** First time settings drift bites you in a way that took >10 minutes to debug.

## Per-session aggregate view

**What:** A dashboard-style query: per session, total tool calls, breakdown by tool/verb, total duration (sum of `completed_ts - ts` per row), error rate.

**Why deferred:** All data is there; just needs the SQL view + a render.

**Trigger to act:** Genuinely curious about session-level patterns or cost attribution.

## Recorder error log

**What:** Today the recorder swallows malformed input silently and prints unhandled exceptions to stderr (the harness surfaces them, but they're noisy). A dedicated `recorder.log` file would let us see drops/errors without harness noise.

**Why deferred:** Hasn't bitten us yet. Silent drop is the right behavior most of the time.

**Trigger to act:** Suspect we're missing rows and need to diagnose.

---

## How to use this file

- Add new items as bullets under existing sections, or create new sections.
- Each entry: **What** + **Why deferred** + **Trigger to act**.
- When promoting an item to active work, move it into the plan file (`dot_claude/.claude/plans/observability-module.md`) under a new Phase section, then delete from this file.
- This file is for *speculation and capture*, not commitments.
